"""Production single-worm paths: SAM2 silhouettes, body motion and pillar continuity.

This module has no validation-data paths. Coordinates are analysis-crop pixels;
orientation estimates remain separate from measured geometry and human truth.
"""
import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt, map_coordinates

from body_sequence import select_body_sequence, BodySequenceBreak, enclosed_holes
from framewise_pose import observed_pose
from centerline_topology import prepare_topology, junction_candidates, hidden_loop_candidates, skeleton_graph_candidates
from centerline_correspondence import overlap_link
from body_geometry import local_field, candidate_score
from elastic_curve import static_pillars, deformable_fit
from head_recovery import appearance_vote, pose_parity, tracking_indices
from marked_end_recovery import fingerprint, recover_marked_end
from head_evidence import normalize_anchors
from short_turns import recover_flanked_candidates
from blurred_tips import locate_blurred_tips

VERSION = 'sam2-body-sequence-v12-crossing-elastic'
MIN_LENGTH_FRACTION = .85

# Include the measurement geometry called by observed_pose, anchor normalization,
# and image appearance, even though those dependencies are imported indirectly.
SIGNATURE_FILES = ('body_sequence_solver.py', 'body_sequence.py', 'extract_body_sequence.py',
                   'centerline_topology.py', 'centerline_correspondence.py', 'elastic_curve.py',
                   'framewise_pose.py', 'body_geometry.py',
                   'motion.py', 'head_recovery.py', 'head_appearance.py', 'head_tail.py',
                   'analyze_phenotypes.py', 'body_frame_cache.py', 'skeleton_geometry.py',
                   'head_evidence.py', 'tracking_data.py', 'image_io.py', 'serialization.py',
                   'review_intervals.py', 'marked_end_recovery.py', 'terminal_overlap.py', 'short_turns.py', 'blurred_tips.py')


def signature():
    import hashlib
    from pathlib import Path
    digest=hashlib.sha256()
    for name in SIGNATURE_FILES:
        content = Path(__file__).with_name(name).read_bytes()
        digest.update(name.encode('utf-8') + b'\0')
        digest.update(len(content).to_bytes(8, 'big'))
        digest.update(content)
    return digest.hexdigest()


def availability():
    """Check the current pipeline without loading models or any point tracker."""
    from importlib.util import find_spec
    from resource_paths import sam2_checkpoint
    from sam2_models import model_spec
    missing=[name for name in ('cv2','numpy','scipy','skimage','torch','sam2') if find_spec(name) is None]
    checkpoint=sam2_checkpoint(model_spec('large')['checkpoint'])
    if not checkpoint.is_file():missing.append('SAM2 Large 权重')
    return dict(available=not missing,detail='缺少：'+'、'.join(missing) if missing else '分析准备已完成',method='body_sequence')

def visible_pillars(gray, masks, radius, *, holes=None):
    """Locate static circular pillars near the contact holes in the raw images."""
    guesses=[]
    if holes is None:
        holes = (enclosed_holes(mask, mask) for mask in masks)
    for frame_holes in holes:
        for area, center in frame_holes:
            if area < radius**2:
                continue
            if not any(np.linalg.norm(center-old)<2*radius for old in guesses):
                guesses.append(center)
    result=[]
    for guess in guesses:
        lo=np.maximum(np.floor(guess-5*radius).astype(int),0)
        hi=np.minimum(np.ceil(guess+5*radius).astype(int),gray[0].shape[::-1])
        patches=[im[lo[1]:hi[1],lo[0]:hi[0]] for im in gray]
        background=np.median(np.stack(patches),axis=0).astype('uint8')
        circles=cv2.HoughCircles(background,cv2.HOUGH_GRADIENT,1,2*radius,param1=80,param2=18,
                                 minRadius=int(1.2*radius),maxRadius=int(4*radius))
        if circles is None:
            continue
        locations=circles[0,:,:2]+lo
        nearest=int(np.linalg.norm(locations-guess,axis=1).argmin())
        if np.linalg.norm(locations[nearest]-guess)<1.5*radius:
            result.append(locations[nearest])
    return result


def _keep_candidates(candidates, mask, field, origin, reference, radius, device='cpu', relax_coiled=False):
    min_inside = .90 if relax_coiled else .97
    min_len = (.72 * reference - 2 * radius) if relax_coiled else (MIN_LENGTH_FRACTION * reference - 2 * radius)
    max_len = (1.18 * reference + 2 * radius) if relax_coiled else (1.15 * reference + 2 * radius)
    out = []
    for c in candidates:
        candidate_score(c, mask, field, origin, reference, radius, None, device=device)
        if (c['inside_fraction'] < min_inside or c['reconstruction_iou'] < .55 or c['local_hairpin']
                or not min_len <= c['length_px'] <= max_len):
            continue
        if any(min(np.linalg.norm(c['points'] - v['points'], axis=1).mean(),
                   np.linalg.norm(c['points'][::-1] - v['points'], axis=1).mean()) < 1.5 for v in out):
            continue
        out.append(c)
    return out


def candidates_for(mask,reference,observed,device='cpu'):
    if not mask.any():return [],1.
    field,origin=local_field(mask)
    radius=max(1.,float(np.median(field[field>0]))*1.5)
    def keep(candidates, relax_coiled=False):
        return _keep_candidates(candidates, mask, field, origin, reference, radius, device=device, relax_coiled=relax_coiled)
    if observed is not None:
        kept=keep([dict(points=observed,name='clear_observation',connection_cost=0.,endpoint_observed=[True,True])])
        if kept:return kept,radius
    topology = prepare_topology(mask, prune=True)
    if topology is None:
        return [], radius
    raw = []
    for zone in (.7, 1., 1.25):
        choices, _, _ = junction_candidates(topology, zone)
        for candidate in choices:
            candidate.update(name=f'zone{zone}_'+candidate['name'], endpoint_observed=[True, True])
        raw.extend(choices)
    raw.extend(hidden_loop_candidates(topology, reference))
    kept = keep(raw)
    if kept:
        return kept, radius
    graph_raw = skeleton_graph_candidates(topology, reference)
    kept = keep(graph_raw, relax_coiled=True)
    if kept:
        return kept, radius
    for scale in (.5, .35):
        small = cv2.resize(mask.astype('uint8'), None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST) > 0
        scaled_topo = prepare_topology(small, prune=True)
        if scaled_topo is None:
            continue
        choices, _, _ = junction_candidates(scaled_topo, 1.25)
        for candidate in choices:
            candidate['endpoint_observed'] = [True, True]
        choices.extend(hidden_loop_candidates(scaled_topo, reference*scale))
        for candidate in choices:
            candidate['points'] = (candidate['points']+.5)/scale-.5
            candidate['name'] = f'scale{scale}_'+candidate['name']
        kept = keep(choices)
        if kept:
            return kept, radius
        scaled_graph = skeleton_graph_candidates(scaled_topo, reference * scale)
        for candidate in scaled_graph:
            candidate['points'] = (candidate['points']+.5)/scale-.5
            candidate['name'] = f'scale{scale}_'+candidate['name']
        kept = keep(scaled_graph, relax_coiled=True)
        if kept:
            return kept, radius
    return [], radius


def recover_short_observations(frames, observed, masks, reference, fps, device='cpu'):
    """Reconsider a short projected length only between two measured, separated poses."""
    reliable = [i for i, choices in enumerate(frames)
                if choices and choices[0]['name'] == 'clear_observation']
    for left, right in zip(reliable, reliable[1:]):
        span = right - left
        if span not in (2, 3) or span / fps > .25:
            continue
        gap = range(left + 1, right)
        if any(frames[i] or observed[i] is None for i in gap):
            continue
        a, b = observed[left], observed[right]
        flip = pose_parity(a, b)
        if flip is None:
            continue
        if flip:
            b = b[::-1]
        length = min(frames[i][0]['length_px'] for i in (left, right))
        if any(np.linalg.norm(p[0] - p[-1]) < .45 * length for p in (a, b)):
            continue
        for i in gap:
            points = observed[i]
            guide = a + (b - a) * ((i - left) / span)
            flip = pose_parity(guide, points)
            if flip is None:
                continue
            delta = np.linalg.norm((points[::-1] if flip else points) - guide, axis=1)
            if delta.mean() > .05 * length or max(delta[0], delta[-1]) > .08 * length:
                continue
            field, origin = local_field(masks[i])
            radius = max(1., float(np.median(field[field > 0])) * 1.5)
            candidate = dict(points=points, name='short_observation', connection_cost=0.,
                             endpoint_observed=[True, True])
            candidate_score(candidate, masks[i], field, origin, reference, radius, None, device=device)
            if (.65 * reference <= candidate['length_px'] < MIN_LENGTH_FRACTION * reference - 2 * radius
                    and candidate['length_px'] >= .85 * length
                    and np.linalg.norm(points[0] - points[-1]) >= .45 * length
                    and candidate['reconstruction_iou'] >= .85 and candidate['inside_fraction'] >= .99
                    and not candidate['local_hairpin']):
                frames[i].append(candidate)


def taper_seed(mask,line):
    """Optional shape-only initialization; never claim anatomical confirmation."""
    radii=map_coordinates(distance_transform_edt(mask),line.T[::-1],order=1,mode='constant')
    side=taper_side(radii)
    return None if side is None else line[0 if side==0 else -1].copy()


def taper_side(radii):
    ratios=[]
    for r in (radii,radii[::-1]):
        if np.median(r[20:31])<=0:return None
        ratios.append(float(np.median(r[2:13])/np.median(r[20:31])))
    if abs(ratios[0]-ratios[1])/max(sum(ratios),1e-9)<.15:return None
    return int(np.argmax(ratios))


def _biophysical_orientation(lines, masks, clear_frames, votes=None):
    """Combine anterior taper, forward locomotion bias, and lateral tip foraging on long tracks."""
    if len(clear_frames) < 15:
        return None, {}
    if votes is None:
        votes = [[], []]
        for i in clear_frames:
            field, origin = local_field(masks[i])
            radii = map_coordinates(field, (lines[i] - origin).T[::-1], order=1, mode='constant')
            s = taper_side(radii)
            if s is not None:
                votes[s].append(i)
    n_t = len(votes[0]) + len(votes[1])
    t_score = (len(votes[0]) - len(votes[1])) / n_t if n_t else 0.0
    t_vote = (0 if t_score > 0 else 1) if (n_t >= 5 and abs(t_score) >= 0.25) else None

    projs, sw0, sw1 = [], [], []
    for i, j in zip(clear_frames, clear_frames[1:]):
        if j != i + 1:
            continue
        v = lines[j].mean(0) - lines[i].mean(0)
        d = lines[i, 0] - lines[i, 25]
        dn = float(np.linalg.norm(d))
        if dn > 1e-6:
            projs.append(float(np.dot(v, d / dn)))
        mid_v = lines[j, 50] - lines[i, 50]
        t0 = lines[i, 0] - lines[i, 15]
        n0_len = float(np.linalg.norm(t0))
        t1 = lines[i, -1] - lines[i, -16]
        n1_len = float(np.linalg.norm(t1))
        if n0_len > 1e-6 and n1_len > 1e-6:
            u0, u1 = t0 / n0_len, t1 / n1_len
            sw0.append(abs(float(np.dot((lines[j, 0] - lines[i, 0]) - mid_v, [-u0[1], u0[0]]))))
            sw1.append(abs(float(np.dot((lines[j, -1] - lines[i, -1]) - mid_v, [-u1[1], u1[0]]))))

    projs_arr = np.asarray(projs, float)
    active = projs_arr[np.abs(projs_arr) > 1.5]
    m_score = float(np.mean(np.sign(active))) if len(active) else 0.0
    m_vote = (0 if m_score > 0 else 1) if (len(active) >= 15 and abs(m_score) >= 0.15) else None

    s0 = float(np.median(sw0)) if sw0 else 0.0
    s1 = float(np.median(sw1)) if sw1 else 0.0
    f_score = (s0 - s1) / max(s0 + s1, 1e-6)
    f_vote = (0 if f_score > 0 else 1) if (len(sw0) >= 15 and max(s0, s1) >= 3.0 and abs(f_score) >= 0.15) else None

    decisive = [v for v in (t_vote, m_vote, f_vote) if v is not None]
    side = decisive[0] if (len(decisive) >= 2 and len(set(decisive)) == 1) else None
    return side, dict(
        taper=round(t_score, 3), taper_n=n_t,
        loco=round(m_score, 3), loco_n=int(len(active)),
        forage=round(f_score, 3), decisive_votes=len(decisive),
    )


def _verify_auto_seed_orientation(lines, facts, masks, guards):
    """Correct single-frame auto_taper_seed inversions when >=2 trajectory cues unanimously disagree."""
    audit = []
    comps = sorted({
        tuple(f['component']) for f in facts
        if f and f['head_known'] and not f.get('manual_lineage') and not f.get('anchor_conflict')
        and f.get('head_source') == 'auto_taper_seed'
    })
    for lo, hi in comps:
        span = range(lo, hi + 1)
        if guards and any(
            not guards[i]['identity_valid'] or not guards[i]['mask_valid']
            or guards[i].get('source_frame_invalid') or guards[i]['identity_segment'] != guards[lo]['identity_segment']
            for i in span
        ):
            continue
        clear_frames = [
            i for i in span
            if facts[i]['candidate'] == 'clear_observation' and facts[i]['head_observed'] and facts[i]['tail_observed']
        ]
        side, evidence = _biophysical_orientation(lines, masks, clear_frames)
        if side == 1:
            _orient_component(lines, facts, lo, hi, True, 'biophysical_multi_cue_consensus')
            audit.append(dict(component=[lo, hi], flipped=True, **evidence))
        elif side == 0:
            audit.append(dict(component=[lo, hi], flipped=False, **evidence))
    return audit


def reseed_clear_components(lines,facts,masks,components,guards):
    """Orient a still-unassigned continuous track from multiple measured taper or biophysical cues."""
    audit=[]
    for lo,hi in components:
        span=range(lo,hi+1)
        if hi-lo<2 or any(facts[i]['head_known'] or facts[i]['anchor_conflict'] for i in span):
            continue
        if guards and any(not guards[i]['identity_valid'] or not guards[i]['mask_valid']
                or guards[i].get('source_frame_invalid') or guards[i]['identity_segment']!=guards[lo]['identity_segment']
                for i in span):
            continue
        clear_frames = [
            i for i in span
            if facts[i]['candidate']=='clear_observation' and facts[i]['head_observed'] and facts[i]['tail_observed']
        ]
        if len(clear_frames) < 3:
            continue
        votes=[[],[]]
        for i in clear_frames:
            field,origin=local_field(masks[i])
            radii=map_coordinates(field,(lines[i]-origin).T[::-1],order=1,mode='constant')
            side=taper_side(radii)
            if side is not None:
                votes[side].append(i)
        side=int(len(votes[1])>len(votes[0]))
        support=votes[side]
        other=votes[1-side]
        # A neighbouring pair plus a later or earlier observation must agree; long
        # tracks tolerate rare single-frame taper noise when overwhelming consensus holds.
        has_pair=any(b==a+1 for a,b in zip(support,support[1:]))
        strict_small=len(support)>=3 and not other
        overwhelming_long=len(support)>=6 and len(support)>=5*len(other)
        accepted=has_pair and (strict_small or overwhelming_long)
        source='local_taper_consensus'
        bio_evidence={}
        if not accepted and len(clear_frames) >= 15:
            bio_side, bio_evidence = _biophysical_orientation(lines, masks, clear_frames, votes=votes)
            if bio_side is not None:
                side = bio_side
                accepted = True
                source = 'biophysical_multi_cue_consensus'
        audit.append(dict(component=[lo,hi],votes=votes,accepted=accepted,source=source,biophysical=bio_evidence))
        if accepted:
            if side:
                lines[lo:hi+1]=lines[lo:hi+1,::-1].copy()
                for i in span:
                    facts[i]['head_observed'],facts[i]['tail_observed']=facts[i]['tail_observed'],facts[i]['head_observed']
            for i in span:
                facts[i].update(head_known=True,head_source=source,manual_lineage=False)
    return audit


def flow_supports_order(gray, masks, start, stop, earlier, later, indices):
    """Check a proposed orientation using mask-supported motion on both terminal quarters."""
    from motion import lk_step
    points = earlier[indices].copy()
    supported = np.ones(len(points), dtype=bool)
    step = 1 if stop > start else -1
    for frame in range(start, stop, step):
        points, reliable, _ = lk_step(gray[frame], gray[frame+step], points)
        inside = map_coordinates(masks[frame+step].astype(np.uint8), points.T[::-1], order=0, mode='constant')
        supported &= reliable & (inside > 0)
        if np.count_nonzero(supported) < 3:
            return False
    scale = min(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in (earlier, later))
    same = np.linalg.norm(points - later[indices], axis=1)
    opposite = np.linalg.norm(points - later[::-1][indices], axis=1)
    matches = supported & (same <= .12 * scale) & (same + .15 * scale < opposite)
    contradicts = supported & (opposite <= .12 * scale) & (opposite + .15 * scale < same)
    return (not contradicts.any() and np.count_nonzero(matches) >= 3
            and matches[indices <= 25].any() and matches[indices >= 75].any())


def recover_native_observations(frames, observed, masks, gray, reference, fps, excluded, point_count, device='cpu'):
    """Retry short missing paths at native resolution, verified from both sides.

    Only the current silhouette supplies recovered geometry. Neighbours and
    optical flow verify it; they never interpolate a replacement measurement.
    Each missing frame is attempted once. Existing candidates remain untouched.
    """
    anchors = [i for i,c in enumerate(frames) if c and c[0]['name'] == 'clear_observation']
    indices = tracking_indices(point_count)
    attempts = []
    for left, right in zip(anchors, anchors[1:]):
        if right-left < 2 or (right-left)/fps > .25 or any(i in excluded for i in range(left, right+1)):
            continue
        for i in range(left+1, right):
            if frames[i]:
                continue
            attempt = dict(frame=i, anchors=[left,right], accepted=False, reason='native_pose_missing')
            attempts.append(attempt)
            p, reason = observed_pose(masks[i], max_size=None)
            if p is None:
                attempt['reason'] = reason
                continue
            choices, _ = candidates_for(masks[i], reference, p, device=device)
            candidate = next((c for c in choices if c['name']=='clear_observation'), None)
            if candidate is None:
                attempt['reason'] = 'native_geometry_rejected'
                continue
            for anchor in (left,right):
                flip = pose_parity(observed[anchor],p,indices)
                if flip is None or not flow_supports_order(gray,masks,anchor,i,observed[anchor],
                        p[::-1] if flip else p,indices):
                    attempt['reason'] = 'two_sided_image_support_insufficient'
                    break
            else:
                candidate.update(name='native_observation_recovery',recovery_anchors=[left,right])
                frames[i] = [candidate]
                observed[i] = p
                attempt.update(accepted=True,reason='native_geometry_and_two_sided_flow')
    return attempts


def _fit_elastic_candidates(mask, neighbor_bases, reference, radius, pillars, device='cpu'):
    """Fit and validate elastic coil candidates seeded from adjacent solved frames."""
    field, origin = local_field(mask)
    cand_list = []
    for base in neighbor_bases:
        fit = deformable_fit(
            mask, base['points'], reference, radius,
            pillars=pillars, neighbor_points=base['points'], device=device, steps=95,
        )
        if fit is None:
            continue
        end_a_clear = float(np.linalg.norm(fit[50:] - fit[0], axis=1).min()) > 1.5 * radius
        end_b_clear = float(np.linalg.norm(fit[:51] - fit[-1], axis=1).min()) > 1.5 * radius
        cand_list.append(dict(
            points=fit,
            name='elastic_contact_fit',
            connection_cost=0.15,
            endpoint_observed=[bool(end_a_clear), bool(end_b_clear)],
        ))
    return _keep_candidates(cand_list, mask, field, origin, reference, radius, device=device, relax_coiled=True)


def recover_elastic_contacts(frames, masks, reference, radii, excluded, pillars, device='cpu'):
    """Propagate arc-length and pillar-constrained elastic curves into coiled contact gaps."""
    from collections import deque

    valid_areas = [int(masks[i].sum()) for i, c in enumerate(frames) if c and c[0]['name'] == 'clear_observation']
    if not valid_areas:
        valid_areas = [int(masks[i].sum()) for i, m in enumerate(masks) if i not in excluded and m.any()]
    if not valid_areas:
        return []
    ref_area = float(np.median(valid_areas))
    med_radius = float(np.median(radii)) if radii else 8.0
    n = len(masks)
    h, w = masks[0].shape if n else (0, 0)

    def valid_silhouette(idx):
        if idx in excluded or not masks[idx].any():
            return False
        mask_u8 = masks[idx].astype(np.uint8, copy=False)
        area = int(mask_u8.sum())
        if area < 0.55 * ref_area:
            return False
        x, y, bw, bh = cv2.boundingRect(mask_u8)
        if x <= 0 or y <= 0 or x + bw >= w or y + bh >= h:
            return False
        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask_u8, connectivity=8)
        if n_labels <= 1:
            return False
        largest = int(stats[1:, cv2.CC_STAT_AREA].max())
        return largest >= 0.55 * ref_area and largest >= 0.85 * area

    attempts = []
    # Phase 1: augment heuristic non-clear boundary frames bordering a clear observation
    # so uncoiling transitions retain a temporally smooth candidate.
    for target in range(n):
        if not frames[target] or any(c['name'] == 'clear_observation' for c in frames[target]):
            continue
        if not valid_silhouette(target):
            continue
        clear_nbs = [
            frames[nb][0] for nb in (target - 1, target + 1)
            if 0 <= nb < n and frames[nb] and frames[nb][0]['name'] == 'clear_observation'
        ]
        if not clear_nbs:
            continue
        r_t = float(radii[target]) if radii[target] > 1.0 else med_radius
        kept = _fit_elastic_candidates(masks[target], clear_nbs, reference, r_t, pillars, device=device)
        added = 0
        for cand in kept:
            if any(min(np.linalg.norm(cand['points'] - v['points'], axis=1).mean(),
                       np.linalg.norm(cand['points'][::-1] - v['points'], axis=1).mean()) < 1.5
                   for v in frames[target]):
                continue
            frames[target].insert(0, cand)
            added += 1
        if len(frames[target]) > 6:
            frames[target] = frames[target][:6]
        if added:
            attempts.append(dict(frame=target, source='clear_boundary', accepted=True, candidates=added))

    # Phase 2: bidirectional wave propagation into empty coiled frames.
    frontier = deque(i for i in range(n) if frames[i])
    queued = set(frontier)
    while frontier:
        src = frontier.popleft()
        for target in (src - 1, src + 1):
            if not (0 <= target < n) or frames[target] or not valid_silhouette(target):
                continue
            r_t = float(radii[target]) if radii[target] > 1.0 else med_radius
            bases = [frames[nb][0] for nb in (target - 1, target + 1) if 0 <= nb < n and frames[nb]]
            kept = _fit_elastic_candidates(masks[target], bases, reference, r_t, pillars, device=device)
            attempts.append(dict(frame=target, source=src, accepted=bool(kept), candidates=len(kept)))
            if kept:
                frames[target] = kept[:4]
                if target not in queued:
                    queued.add(target)
                    frontier.append(target)
    return attempts


def _same_identity_span(facts_lo, a, b, excluded, guards):
    """Ensure no excluded or guard-invalid frame separates a reference frame from a target span."""
    lo, hi = min(a, b), max(a, b)
    if any(i in excluded for i in range(lo, hi + 1)):
        return False
    if guards is None:
        return True
    seg = guards[facts_lo]['identity_segment']
    return all(
        guards[i]['identity_valid'] and guards[i]['mask_valid']
        and not guards[i].get('source_frame_invalid') and guards[i]['identity_segment'] == seg
        for i in range(lo, hi + 1)
    )


def _orient_component(output, facts, lo, hi, flip, source, **extra):
    for i in range(lo, hi + 1):
        if flip:
            output[i] = output[i, ::-1]
            facts[i]['head_observed'], facts[i]['tail_observed'] = facts[i]['tail_observed'], facts[i]['head_observed']
        facts[i].update(head_known=True, head_source=source, manual_lineage=False, **extra)


def _propagate_short_gap_links(output, facts, gray, masks, components, excluded, guards, fps, point_count):
    """Propagate head orientation across <=0.5s gaps via pose parity, flow, or body overlap."""
    indices = tracking_indices(point_count)
    max_gap = int(np.ceil(.5 * fps))
    changed = True
    while changed:
        changed = False
        for lo, hi in components:
            if facts[lo]['head_known'] or facts[lo]['anchor_conflict']:
                continue
            prev_ref = next((i for i in range(lo - 1, max(-1, lo - max_gap - 1), -1) if facts[i]), None)
            next_ref = next((i for i in range(hi + 1, min(len(facts), hi + max_gap + 1)) if facts[i]), None)
            candidates = []
            if prev_ref is not None and facts[prev_ref]['head_known']:
                candidates.append((prev_ref, lo, range(lo, min(hi + 1, lo + 3))))
            if next_ref is not None and facts[next_ref]['head_known']:
                candidates.append((next_ref, hi, range(hi, max(lo - 1, hi - 3), -1)))
            for ref, adj, sample_span in candidates:
                if not _same_identity_span(lo, ref, adj, excluded, guards):
                    continue
                votes = [pose_parity(output[ref], output[i], indices) for i in sample_span]
                useful = [v for v in votes if v is not None]
                if len(set(useful)) == 1:
                    motion_checked = False
                    if len(useful) == 1:
                        if (votes[0] is None or not facts[ref].get('manual_lineage')
                                or not all(facts[i]['head_observed'] and facts[i]['tail_observed'] for i in (ref, adj))
                                or not flow_supports_order(gray, masks, ref, adj, output[ref],
                                                           output[adj][::-1] if votes[0] else output[adj], indices)):
                            useful = []
                        else:
                            motion_checked = True
                    if useful:
                        extra = dict(preceding_reference=ref, body_votes=len(useful))
                        if motion_checked:
                            extra['flow_order_supported'] = True
                        _orient_component(output, facts, lo, hi, useful[0] == 1, 'short_gap_body_candidate', **extra)
                        changed = True
                        break
                if all(facts[i]['head_observed'] and facts[i]['tail_observed'] for i in (ref, adj)):
                    match = overlap_link(output[ref], output[adj])
                    if (match is not None and match['overlap'] >= 0.55
                            and match['margin'] >= 0.25 and match['rms_bl'] <= 0.05):
                        _orient_component(output, facts, lo, hi, bool(match['flip']),
                                          'short_gap_body_candidate', preceding_reference=ref, body_votes=1)
                        changed = True
                        break


def _recover_appearance_components(output, facts, masks, gray, marks, components, excluded, guards):
    """Orient remaining components iteratively using nearby appearance references."""
    n = len(output)
    fp_cache = {}

    def feat(i):
        if i not in fp_cache:
            fp_cache[i] = fingerprint(gray[i], masks[i], output[i])
        return fp_cache[i]

    seed = next((a['frame'] for a in marks if facts[a['frame']] and facts[a['frame']]['head_known']), None)
    if seed is None:
        return None
    refs = [i for i in range(max(0, seed - 3), min(n, seed + 4))
            if facts[i] and facts[i]['head_known'] and _same_identity_span(seed, seed, i, excluded, guards)]
    global_tmpl = np.median([feat(i) for i in refs], axis=0) if refs else None

    for _ in range(4):
        progressed = False
        known_clear = [
            i for i, f in enumerate(facts)
            if f and f['head_known'] and f['candidate'] == 'clear_observation'
            and f['head_observed'] and f['tail_observed']
        ]
        for lo, hi in components:
            if facts[lo]['head_known'] or facts[lo]['anchor_conflict']:
                continue
            mid = 0.5 * (lo + hi)
            local_pool = sorted(known_clear, key=lambda i: abs(i - mid))
            local_refs = []
            for idx in local_pool:
                span_lo, span_hi = min(idx, lo), max(idx, hi)
                if _same_identity_span(lo, span_lo, span_hi, excluded, guards):
                    local_refs.append(idx)
                    if len(local_refs) >= 8:
                        break
            seed_ok = _same_identity_span(lo, min(seed, lo), max(seed, hi), excluded, guards)
            candidates = []
            if local_refs:
                tmpl = np.median([feat(i) for i in local_refs], axis=0)
                if not (global_tmpl is not None and seed_ok and appearance_vote(tmpl, global_tmpl) < -0.35):
                    candidates.append(tmpl)
            if global_tmpl is not None and seed_ok:
                candidates.append(global_tmpl)
            if not candidates:
                continue

            sample = [
                i for i in range(lo, hi + 1)
                if facts[i]['candidate'] == 'clear_observation'
                and facts[i]['head_observed'] and facts[i]['tail_observed']
            ]
            if len(sample) < 3:
                sample = [i for i in range(lo, hi + 1) if facts[i]['head_observed'] and facts[i]['tail_observed']]
            if len(sample) < 3:
                continue
            sample_feats = [feat(i) for i in sample]
            for tmpl in candidates:
                votes = [appearance_vote(tmpl, sf) for sf in sample_feats]
                useful = np.array([v for v in votes if abs(v) >= .3])
                if len(useful) < 3 or abs(useful.mean()) < .45 or np.mean(np.sign(useful) == np.sign(useful.mean())) < .75:
                    continue
                _orient_component(
                    output, facts, lo, hi, useful.mean() < 0, 'multi_frame_appearance_candidate',
                    appearance_support=len(useful), appearance_score=float(abs(useful.mean())),
                )
                progressed = True
                break
        if not progressed:
            break
    return seed


def solve(masks,gray,fps,manual=None,point_count=13,source_invalid=(),auto_seed=True,progress=None,guards=None,device='cpu'):
    """Return 101-point geometry, per-frame endpoint evidence and sequence audit.

    Manual points are crop-local. Explicitly excluded source frames break both
    motion paths and recovery evidence. A missing candidate stays missing.
    """
    tracking_indices(point_count)
    n=len(masks)
    if n!=len(gray) or not np.isfinite(fps) or fps<=0:raise ValueError('Invalid body-sequence input')
    excluded=set(source_invalid)
    output=np.full((n,101,2),np.nan)
    facts=[None]*n;windows=[]
    observed=[]
    for i,mask in enumerate(masks):
        observed.append(None if i in excluded else observed_pose(mask)[0])
        if progress:progress(i,n,'读取虫体中线',.15*i/max(n,1))
    lengths=[np.linalg.norm(np.diff(p,axis=0),axis=1).sum() for p in observed if p is not None]
    if not lengths:return output,facts,dict(version=VERSION,windows=[],reason='no_length_reference')
    reference=float(np.median(lengths))
    frames=[];radii=[]
    for i,(mask,p) in enumerate(zip(masks,observed)):
        cs,radius=([],1.) if i in excluded else candidates_for(mask,reference,p,device=device)
        frames.append(cs);radii.append(radius)
        if progress:progress(i+1,n,'生成完整虫体与交叉支路候选',.15+.4*(i+1)/max(n,1))
    recover_short_observations(frames, observed, masks, reference, fps, device=device)
    recovery = recover_native_observations(frames,observed,masks,gray,reference,fps,excluded,point_count,device=device)
    pillars_static = static_pillars(gray, float(np.median(radii)) if radii else 10.)
    elastic_recovery = recover_elastic_contacts(frames, masks, reference, radii, excluded, pillars_static, device=device)
    marks=[dict(a) for a in normalize_anchors(manual,n)]
    for a in marks:a['human']=True
    if not marks and auto_seed:
        for i,p in enumerate(observed):
            if p is None or not frames[i]:continue
            point=taper_seed(masks[i],p)
            if point is not None:
                marks=[dict(frame=i,point=point.tolist(),human=False)]
                break
    idx=np.array([i for i,c in enumerate(frames) if c],int)
    queue=[list(a) for a in np.split(idx,np.where(np.diff(idx)>1)[0]+1) if len(a)]
    # Candidates and samples are fixed now. Splitting a window or verifying a
    # second mark reuses the same image motion, independent of radius and marks.
    flow_cache={}
    # Hole geometry depends only on the silhouette. A split still uses its own
    # radius threshold and background images, including for singleton window audits.
    holes = [enclosed_holes(mask, mask) if choices else () for mask, choices in zip(masks, frames)]
    while queue:
        group=queue.pop(0);lo,hi=int(group[0]),int(group[-1])
        local=[a for a in marks if lo<=a['frame']<=hi]
        linked=bool(local);conflict=False
        radius=float(np.median(radii[lo:hi+1]))
        pillars = visible_pillars(gray[lo:hi+1], masks[lo:hi+1], radius, holes=holes[lo:hi+1])
        if len(group)==1:
            chosen=[dict(points=frames[lo][0]['points'].copy(),candidate=0,reverse=False)]
            evidence=dict(links=[],total_cost=None)
            if local:
                p=chosen[0]['points']
                distances=np.linalg.norm(p[[0,-1]]-np.array([a['point'] for a in local])[:,None,:],axis=2)
                sides=distances.argmin(axis=1)
                if ((distances.min(axis=1)>.2*reference).any() or (np.ptp(distances,axis=1)<.05*reference).any()
                        or (sides!=sides[0]).any()):
                    linked=False;conflict=True
                elif sides[0]:chosen[0].update(points=p[::-1],reverse=True)
        else:
            try:
                if len(local)>1:
                    # Check marks against an independently oriented body path.
                    # A contradictory mark must not manufacture an endpoint swap.
                    unmarked,_=select_body_sequence(frames[lo:hi+1],gray[lo:hi+1],masks[lo:hi+1],radius,
                        point_count=point_count,pillar_centers=pillars,device=device,flow_cache=flow_cache,frame_offset=lo)
                    votes=[]
                    for a in local:
                        p=unmarked[a['frame']-lo]['points']
                        d=np.linalg.norm(p[[0,-1]]-a['point'],axis=1)
                        votes.append(int(d.argmin()) if d.min()<=.2*reference and abs(d[0]-d[1])>=.05*reference else None)
                    if None in votes or len(set(votes))!=1:
                        raise ValueError('Conflicting head marks on a continuous body path')
                chosen,evidence=select_body_sequence(frames[lo:hi+1],gray[lo:hi+1],masks[lo:hi+1],radius,
                    point_count=point_count,pillar_centers=pillars,
                    anchors=[(a['frame']-lo,a['point']) for a in local],device=device,flow_cache=flow_cache,frame_offset=lo)
            except BodySequenceBreak as exc:
                cut=lo+exc.index
                queue[:0]=[list(range(lo,cut)),list(range(cut,hi+1))]
                windows.append(dict(start=lo,end=hi,rejected_at=cut,reason='body_correspondence_break'))
                continue
            except ValueError as exc:
                if not local:raise
                linked=False;conflict=True
                chosen,evidence=select_body_sequence(frames[lo:hi+1],gray[lo:hi+1],masks[lo:hi+1],radius,
                    point_count=point_count,pillar_centers=pillars,device=device,flow_cache=flow_cache,frame_offset=lo)
                evidence['head_anchor_rejected']=str(exc)
        for state,i in zip(chosen,group):
            c=frames[i][state['candidate']]
            obs=c['endpoint_observed'][::-1] if state['reverse'] else c['endpoint_observed']
            output[i]=state['points']
            facts[i]=dict(frame=int(i),head_known=linked,
                head_source=('initial_mark' if local[0]['human'] else 'auto_taper_seed') if linked else 'unresolved',
                manual_lineage=bool(linked and any(a['human'] for a in local)),anchor_conflict=conflict,
                anchor_frames=[a['frame'] for a in local],head_observed=bool(obs[0]),tail_observed=bool(obs[1]),
                candidate=c['name'],length_px=c['length_px'],inside=c['inside_fraction'],iou=c['reconstruction_iou'],
                candidate_count=len(frames[i]),component=[lo,hi],recovery_anchors=c.get('recovery_anchors',[]))
        windows.append(dict(start=lo,end=hi,head_linked_to_seed=linked,
            pillar_centers_crop_xy=[p.tolist() for p in pillars],**evidence))
        if progress:progress(hi+1,n,'结合前后帧选择中线和绕柱顺序',.55+.4*(hi+1)/max(n,1))
    for window in windows:
        if 'rejected_at' in window and facts[window['rejected_at']]:
            facts[window['rejected_at']]['correspondence_break_before']=True
    head_links = recover_marked_end(output,facts,masks,gray,fps,marks,excluded,guards)
    bio_checks = _verify_auto_seed_orientation(output, facts, masks, guards) if auto_seed else []
    components=sorted({tuple(f['component']) for f in facts if f and not f['head_known'] and not f['anchor_conflict']})
    _propagate_short_gap_links(output, facts, gray, masks, components, excluded, guards, fps, point_count)
    # A frozen seed template supplies provisional recovery, not a new human mark.
    seed = _recover_appearance_components(output, facts, masks, gray, marks, components, excluded, guards)
    candidate_links=[] if seed is None else recover_flanked_candidates(
        output,facts,fps,seed,excluded,guards,lambda i:fingerprint(gray[i],masks[i],output[i]))
    local_seeds=reseed_clear_components(output,facts,masks,components,guards) if auto_seed else []
    _propagate_short_gap_links(output, facts, gray, masks, components, excluded, guards, fps, point_count)
    if seed is not None:
        locate_blurred_tips(output,facts,gray,masks,guards,excluded)
    return output,facts,dict(version=VERSION,reference_length_px=reference,tracking_point_count=point_count,
                            anchors=marks,windows=windows,recovery=recovery,elastic_recovery=elastic_recovery,
                            biophysical_checks=bio_checks,head_reacquisition=head_links,
                            flanked_candidates=candidate_links,local_reseeding=local_seeds)


def head_results(quality):
    """Adapt saved orientation once; downstream head heuristics must not flip it."""
    results=[]
    for q in quality:
        f=q.get('body_sequence')
        known=bool(f and f['head_known'])
        observed=bool(known and f['head_observed'] and f['tail_observed'])
        confirmed=bool(observed and f.get('manual_lineage'))
        results.append(dict(head_endpoint=0 if known else None,
            head_status=('recovered_from_user_point' if f['head_source']=='recovered_marked_end' else 'confirmed_by_user_point')
                        if confirmed else 'unconfirmed_temporal_candidate' if known else 'unconfirmed_A_B_endpoints',
            head_identity_confirmed=confirmed,head_evidence_score=None,
            head_reason=f['head_source'] if f else 'no_supported_body_path',head_segment=f['component'][0] if known else -1,
            head_anchor_count=len(f.get('anchor_frames',[])) if f else 0,
            head_anchor_conflict=bool(f and f.get('anchor_conflict')),head_shape_votes=0,head_motion_votes=0,
            head_link_boundary=f.get('head_link_boundary',[]) if f else [],
            head_appearance_votes=f.get('head_appearance_support',f.get('appearance_support',0)) if f else 0,
            head_input_flipped=False,body_sequence_head_known=known,body_sequence_version=VERSION,
            body_sequence_segment=f['component'][0] if f else -1,
            body_sequence_break_before=bool(f and f.get('correspondence_break_before')),
            body_recovery_source=f['candidate'] if f and f.get('recovery_anchors') else '',
            body_recovery_anchors=f.get('recovery_anchors',[]) if f else [],
            head_recovery_points=13,head_section_status='body_sequence_estimate' if known and not observed else '',
            head_display_estimated_end=0 if known and not f['head_observed'] else None,
            tail_display_estimated_end=1 if known and not f['tail_observed'] else None))
        if known:
            for side,estimate in f.get('blur_tip_estimates',{}).items():
                x,y=estimate['xy']
                results[-1][f'display_blur_{side}_x_px']=x
                results[-1][f'display_blur_{side}_y_px']=y
                results[-1][f'display_blur_{side}_method']=estimate.get('method','midpoint')
    return results

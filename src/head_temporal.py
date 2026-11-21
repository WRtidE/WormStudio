"""Resolve automatic orientation once per image-supported temporal block.

Adapted from Tierpsy/WormPose's separation of posture continuity and head choice.
Automatic seed orientation is provisional, never anatomical ground truth.
"""
import numpy as np

MAX_INTERVAL_S = .25
BLOCKED_REASONS = {'shape_motion_conflict', 'conflicting_shape_votes',
                   'appearance_motion_conflict'}


def geometry_link(a, b):
    """Return b's reversal and auditable costs, or None if correspondence is unclear."""
    if not np.isfinite(a).all() or not np.isfinite(b).all(): return None
    length = min(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in (a,b))
    if length <= 0: return None
    costs = [float(np.sqrt(np.mean(np.sum((a-q)**2, axis=1)))/length) for q in (b,b[::-1])]
    flip = int(costs[1] < costs[0]); q = b[::-1] if flip else b
    span = max(1, round((len(a)-1)*.05))
    u, v = a[span:]-a[:-span], q[span:]-q[:-span]
    if min(np.linalg.norm(u,axis=1).min(),np.linalg.norm(v,axis=1).min()) < 1e-6: return None
    delta = np.arctan2(u[:,1],u[:,0])-np.arctan2(v[:,1],v[:,0])
    angle = float(np.mean(np.abs(np.arctan2(np.sin(delta),np.cos(delta)))))
    center = np.linalg.norm(a.mean(0)-q.mean(0))/length
    end_step = np.linalg.norm(a[[0,-1]]-q[[0,-1]],axis=1).max()/length
    if (min(costs)>.25 or abs(costs[0]-costs[1])<.15 or center>.3
            or end_step>.5 or angle>np.pi/6): return None
    return dict(flip=flip,rms_bl=min(costs),angle_deg=float(np.degrees(angle)))


def visible_tip_link(a, b, visible_a, visible_b, cfg):
    """Match a visible tip and its adjacent body when the other tip enters or leaves contact."""
    if visible_a.all() and visible_b.all():
        return None
    length = min(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in (a, b))
    if not np.isfinite(length) or length <= 0:
        return None
    span = max(2, round(.1 * (len(a) - 1)) + 1)
    arcs_a = np.stack((a[:span], a[::-1][:span]))
    arcs_b = np.stack((b[:span], b[::-1][:span]))
    distances = np.linalg.norm(arcs_a[:, None] - arcs_b[None, :], axis=-1) / length
    tips = distances[:, :, 0]
    arcs = np.sqrt(np.mean(distances**2, axis=2))
    margin = cfg['min_assignment_margin_bl']
    for i, j in np.argwhere(visible_a[:, None] & visible_b[None, :]):
        # Require a unique match in both directions, including against the estimated
        # opposite ends. A tip passing close to the junction can otherwise switch IDs.
        if any(cost[i, j] > cfg['max_step_bl'] or
               cost[i, j] + margin >= min(cost[1-i, j], cost[i, 1-j]) for cost in (tips, arcs)):
            continue
        flip = int(i != j)
        aligned = b[::-1] if flip else b
        rms = float(np.sqrt(np.mean(np.sum((a - aligned)**2, axis=1))) / length)
        if rms <= .25:
            return dict(flip=flip, rms_bl=rms, source='visible_tip')
    return None


def stabilize(results, poses, identities, fps, mode):
    """Keep manual/metric segments intact; link only reliable automatic segments."""
    for r in results:
        r.update(head_local_endpoint=r['head_endpoint'], head_local_reason=r['head_reason'],
                 head_temporal_block=-1,head_temporal_seed_frame=None,
                 head_temporal_conflict=False,head_temporal_changed=False,
                 head_temporal_link_rms_bl=None,head_temporal_gap_s=None)
    if mode!='twardis': return
    if not np.isfinite(fps) or fps<=0: raise ValueError('头尾时序关联需要有效帧率')
    groups=[]
    for i,r in enumerate(results):
        if r['head_segment']<0: continue
        if not groups or results[groups[-1][-1]]['head_segment']!=r['head_segment']: groups.append([])
        groups[-1].append(i)
    def blocked(g):
        return any(results[i]['head_anchor_count'] or results[i]['head_reason'] in BLOCKED_REASONS for i in g)
    def tracked(i): return poses[i,::-1] if results[i]['head_input_flipped'] else poses[i]
    blocks=[]
    for g in groups:
        if blocks:
            previous,parity,_=blocks[-1][-1];i,j=previous[-1],g[0]
            # A transient identity boundary also prevents linking back to an earlier ID.
            same_identity=all(identities[k]==identities[i] for k in range(i,j+1))
            if same_identity and (j-i)/fps<=MAX_INTERVAL_S and not blocked(previous) and not blocked(g):
                link=geometry_link(tracked(i),tracked(j))
                if link is not None:
                    link['gap_s']=(j-i)/fps
                    blocks[-1].append((g,parity^link['flip'],link));continue
        blocks.append([(g,0,None)])
    for block_id,block in enumerate(blocks):
        if any(blocked(g) for g,_,_ in block):continue
        seeds=[]
        for g,parity,_ in block:
            r=results[g[0]]
            if r['head_endpoint'] is not None and r['head_evidence_score']>=.08:
                score=r['head_evidence_score']*min(len(g)/fps,1.)
                seeds.append((score,-g[0],g,parity))
        if not seeds:continue
        _,_,seed,seed_parity=max(seeds,key=lambda x:(x[0],x[1]))
        sr=results[seed[0]]
        block_head=sr['head_endpoint'] ^ int(sr['head_input_flipped']) ^ seed_parity
        for g,parity,link in block:
            for i in g:
                r=results[i];end=block_head ^ parity ^ int(r['head_input_flipped'])
                old=r['head_endpoint'];changed=old!=end
                r.update(head_temporal_block=block_id,head_temporal_seed_frame=seed[0],
                         head_temporal_conflict=old is not None and changed,
                         head_temporal_changed=changed,
                         head_temporal_link_rms_bl=None if link is None else link['rms_bl'],
                         head_temporal_gap_s=None if link is None else link['gap_s'])
                if len(block)>1 and changed:
                    r.update(head_endpoint=end,head_status='unconfirmed_temporal_candidate',
                             head_reason='temporal_orientation_conflict' if old is not None else 'temporal_orientation_reference',
                             head_evidence_score=sr['head_evidence_score'])


def apply_contact_directions(heads, rows, fit_quality, mode):
    """Map existing anatomical evidence onto a two-boundary A/B correspondence."""
    if mode == 'manual_only' or not fit_quality:
        return
    # Do not use a direction inferred in this pass as evidence for another event.
    original = [r.copy() for r in heads]
    proposals = {}
    events = {tuple(q['contact_bridge']['anchors']) for q in fit_quality if q.get('contact_bridge')}
    for left, right in sorted(events):
        bridges = [q.get('contact_bridge') for q in fit_quality[left+1:right]]
        if not bridges or not all(b and b['anchors'] == [left, right] for b in bridges):
            continue
        span = rows[left:right+1]
        if (not all(r['source_valid'] and r['identity_valid'] and r['mask_valid'] for r in span)
                or len({r['identity_segment'] for r in span}) != 1):
            continue
        conflicts = BLOCKED_REASONS | {'manual_anchor_unreliable', 'initial_taper_conflict', 'conflicting_motion_votes'}
        if any(r['head_anchor_conflict'] or r['head_reason'] in conflicts
               or (r['head_anchor_count'] and not r['head_identity_confirmed'])
               for r in original[left:right+1]):
            continue
        # All contact curves use the left boundary's stored vertex order.
        parity = bridges[0]['right_reversed']
        left_head, right_head = original[left]['head_endpoint'], original[right]['head_endpoint']
        evidence = {end for end in (left_head, None if right_head is None else right_head ^ parity)
                    if end is not None}
        if len(evidence) != 1:
            continue
        end = evidence.pop()
        seed = left if left_head is not None else right

        def assign(i, endpoint):
            proposals.setdefault(i, []).append((endpoint, seed))

        for i, bridge in enumerate(bridges, left+1):
            if bridge['tip_agreement'] and bridge['energy_gap'] is not None and bridge['energy_gap'] >= 1.:
                assign(i, end)
        if not all(i in proposals for i in range(left+1, right)):
            continue
        # Carry the candidate into the immediately adjacent clear segment. Stop
        # at the next ordinary tracking break or independent orientation decision.
        boundary, step, endpoint = (right, 1, end ^ parity) if right_head is None else (left, -1, end)
        if original[boundary]['head_endpoint'] is not None or original[boundary]['head_segment'] < 0:
            continue
        segment = original[boundary]['head_segment']
        for i in range(boundary, len(rows) if step == 1 else -1, step):
            r = original[i]
            if r['head_segment'] != segment or r['head_endpoint'] is not None or r['head_anchor_count']:
                break
            assign(i, endpoint ^ int(original[boundary]['head_input_flipped']) ^ int(r['head_input_flipped']))
    for i, votes in proposals.items():
        if len({endpoint for endpoint, seed in votes}) == 1:
            endpoint, seed = votes[0]
            heads[i].update(head_endpoint=endpoint, head_status='unconfirmed_temporal_candidate',
                            head_identity_confirmed=False, head_reason='two_sided_contact_orientation',
                            head_temporal_seed_frame=seed, head_evidence_score=original[seed]['head_evidence_score'])


def review_terminal_link(a, b):
    """Accept stored correspondence only when both ends have unique reciprocal matches."""
    ref = min(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in (a, b))
    if ref <= 0 or np.linalg.norm(a - b, axis=1).mean() > .18 * ref:
        return False
    distances = np.linalg.norm(a[[0, -1], None] - b[None, [0, -1]], axis=2)
    direct = np.diag(distances)
    return bool(direct.max() <= .25 * ref and
                np.all(direct + .05 * ref < distances[[0, 1], [1, 0]]) and
                np.all(direct + .05 * ref < distances[[1, 0], [0, 1]]))


def orient_loop_reviews(rows, heads, quality, display, worm):
    """Carry confirmed boundary identity across unambiguous links for display only."""
    from skeleton_geometry import sampled
    intervals = {tuple(q['review_anchors']) for q in quality if q.get('review_anchors')}
    for left, right in sorted(intervals):
        span = rows[left:right + 1]
        if not all(r['source_valid'] and r['mask_valid'] and r['identity_valid']
                   and r['identity_segment'] == span[0]['identity_segment'] for r in span):
            continue
        if any(h.get('head_anchor_conflict') for h in heads[left:right + 1]):
            continue
        if not all(r['display_source'] == 'diagnostic_candidate' for r in span[1:-1]):
            continue
        if any((worm, i) not in display for i in range(left, right + 1)):
            continue
        curves = [sampled(display[worm, i]) for i in range(left, right + 1)]
        if any(p is None for p in curves):
            continue
        flip = int(quality[left + 1]['review_right_reversed'])
        if flip:
            curves[-1] = curves[-1][::-1]
        links = [review_terminal_link(a, b) for a, b in zip(curves, curves[1:])]
        votes = {}
        for boundary, direction in ((left, 1), (right, -1)):
            h = heads[boundary]
            if not h['head_identity_confirmed'] or h['head_endpoint'] is None:
                continue
            end = h['head_endpoint'] ^ (flip if boundary == right else 0)
            for i in range(boundary + direction, right if direction == 1 else left, direction):
                edge = min(i, i - direction) - left
                if not links[edge]:
                    break
                votes.setdefault(i, []).append((end, boundary))
        for i, evidence in votes.items():
            # Leave the frames adjacent to a correspondence break anonymous.
            if not all(links[i-left-1:i-left+1]) or len({v[0] for v in evidence}) != 1:
                continue
            end, seed = evidence[0]
            heads[i].update(head_endpoint=end, head_reason='loop_review_temporal_endpoint',
                            head_status='unconfirmed_temporal_candidate',
                            head_temporal_seed_frame=seed, head_identity_confirmed=False)

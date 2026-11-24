"""Production single-worm paths: SAM2 silhouettes, body motion and pillar continuity.

This module has no validation-data paths. Coordinates are analysis-crop pixels;
orientation estimates remain separate from measured geometry and human truth.
"""
import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt, map_coordinates

from body_sequence import select_body_sequence, BodySequenceBreak, persistent_loop_centers
from framewise_pose import observed_pose
from centerline_topology import topology_candidates, hidden_tip_candidates
from body_geometry import local_field, candidate_score
from head_recovery import appearance_vote, pose_parity, tracking_indices
from marked_end_recovery import fingerprint, recover_marked_end
from head_evidence import normalize_anchors
from short_turns import recover_flanked_candidates
from blurred_tips import locate_blurred_tips

VERSION = 'sam2-body-sequence-v11-local-reseed'
MIN_LENGTH_FRACTION = .85

# Include the measurement geometry called by observed_pose, anchor normalization,
# and image appearance, even though those dependencies are imported indirectly.
SIGNATURE_FILES = ('body_sequence_solver.py', 'body_sequence.py', 'extract_body_sequence.py',
                   'centerline_topology.py', 'framewise_pose.py', 'body_geometry.py',
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

def visible_pillars(gray,masks,radius):
    """Locate static circular pillars near the contact holes in the raw images."""
    guesses=[]
    for mask in masks:
        for center in persistent_loop_centers(mask,mask,radius):
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


def candidates_for(mask,reference,observed,device='cpu'):
    if not mask.any():return [],1.
    field,origin=local_field(mask)
    radius=max(1.,float(np.median(field[field>0]))*1.5)
    def keep(candidates):
        out=[]
        for c in candidates:
            candidate_score(c,mask,field,origin,reference,radius,None,device=device)
            if (c['inside_fraction']<.97 or c['reconstruction_iou']<.55 or c['local_hairpin']
                    or not MIN_LENGTH_FRACTION*reference-2*radius <= c['length_px'] <= 1.15*reference+2*radius):continue
            if any(min(np.linalg.norm(c['points']-v['points'],axis=1).mean(),
                       np.linalg.norm(c['points'][::-1]-v['points'],axis=1).mean())<1.5 for v in out):continue
            out.append(c)
        return out
    if observed is not None:
        kept=keep([dict(points=observed,name='clear_observation',connection_cost=0.,endpoint_observed=[True,True])])
        if kept:return kept,radius
    raw=[]
    for zone in (.7,1.,1.25):
        cs,_,_=topology_candidates(mask,prune=True,contact_radius_fraction=zone)
        for c in cs:c.update(name=f'zone{zone}_'+c['name'],endpoint_observed=[True,True])
        raw.extend(cs)
    raw+=hidden_tip_candidates(mask,reference)
    if not raw:
        for scale in (.5,.35):
            small=cv2.resize(mask.astype('uint8'),None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)>0
            cs,_,_=topology_candidates(small,prune=True)
            for c in cs:c['endpoint_observed']=[True,True]
            cs+=hidden_tip_candidates(small,reference*scale)
            for c in cs:
                c['points']=(c['points']+.5)/scale-.5;c['name']=f'scale{scale}_'+c['name']
            raw.extend(cs)
    return keep(raw),radius


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


def reseed_clear_components(lines,facts,masks,components,guards):
    """Orient a still-unassigned continuous track from multiple measured taper observations."""
    audit=[]
    for lo,hi in components:
        span=range(lo,hi+1)
        if hi-lo<2 or any(facts[i]['head_known'] or facts[i]['anchor_conflict']
                or facts[i]['candidate']!='clear_observation'
                or not (facts[i]['head_observed'] and facts[i]['tail_observed']) for i in span):
            continue
        if guards and any(not guards[i]['identity_valid'] or not guards[i]['mask_valid']
                or guards[i].get('source_frame_invalid') or guards[i]['identity_segment']!=guards[lo]['identity_segment']
                for i in span):
            continue
        votes=[[],[]]
        for i in span:
            field,origin=local_field(masks[i])
            radii=map_coordinates(field,(lines[i]-origin).T[::-1],order=1,mode='constant')
            side=taper_side(radii)
            if side is not None:
                votes[side].append(i)
        side=int(len(votes[1])>len(votes[0]))
        support=votes[side]
        # A neighbouring pair plus a later or earlier observation must agree.
        accepted=len(support)>=3 and not votes[1-side] and any(b==a+1 for a,b in zip(support,support[1:]))
        audit.append(dict(component=[lo,hi],votes=votes,accepted=accepted))
        if accepted:
            if side:
                lines[lo:hi+1]=lines[lo:hi+1,::-1].copy()
            for i in span:
                facts[i].update(head_known=True,head_source='local_taper_consensus',manual_lineage=False)
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
    while queue:
        group=queue.pop(0);lo,hi=int(group[0]),int(group[-1])
        local=[a for a in marks if lo<=a['frame']<=hi]
        linked=bool(local);conflict=False
        radius=float(np.median(radii[lo:hi+1]))
        pillars=visible_pillars(gray[lo:hi+1],masks[lo:hi+1],radius)
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
    components=sorted({tuple(f['component']) for f in facts if f and not f['head_known'] and not f['anchor_conflict']})
    for lo,hi in components:
        previous=next((i for i in range(lo-1,max(-1,lo-int(np.ceil(.5*fps))-1),-1) if facts[i]),None)
        if previous is None or not facts[previous]['head_known']:continue
        if any(i in excluded for i in range(previous,lo+1)):continue
        indices = tracking_indices(point_count)
        votes=[pose_parity(output[previous],output[i],indices) for i in range(lo,min(hi+1,lo+3))]
        useful=[v for v in votes if v is not None]
        if len(set(useful)) != 1:
            continue
        motion_checked = False
        if len(useful) == 1:
            if (votes[0] is None or not facts[previous].get('manual_lineage')
                    or not all(facts[i]['head_observed'] and facts[i]['tail_observed'] for i in (previous, lo))
                    or not flow_supports_order(gray, masks, previous, lo, output[previous],
                                               output[lo][::-1] if votes[0] else output[lo], indices)):
                continue
            motion_checked = True
        for i in range(lo,hi+1):
            if useful[0]==1:
                output[i]=output[i,::-1]
                facts[i]['head_observed'],facts[i]['tail_observed']=facts[i]['tail_observed'],facts[i]['head_observed']
            facts[i].update(head_known=True,head_source='short_gap_body_candidate',manual_lineage=False,
                            preceding_reference=previous,body_votes=len(useful))
            if motion_checked:
                facts[i]['flow_order_supported'] = True
    # A frozen seed template supplies provisional recovery, not a new human mark.
    seed=next((a['frame'] for a in marks if facts[a['frame']] and facts[a['frame']]['head_known']),None)
    refs=[] if seed is None else [i for i in range(max(0,seed-3),min(n,seed+4))
                                 if facts[i] and facts[i]['head_known'] and not any(j in excluded for j in range(min(seed,i),max(seed,i)+1))]
    template=np.median([fingerprint(gray[i],masks[i],output[i]) for i in refs],axis=0) if refs else None
    if template is not None:
        for lo,hi in components:
            if facts[lo]['head_known'] or any(i in excluded for i in range(min(seed,lo),max(seed,hi)+1)):continue
            votes=[appearance_vote(template,fingerprint(gray[i],masks[i],output[i]))
                   for i in range(lo,hi+1) if facts[i]['candidate']=='clear_observation']
            useful=np.array([v for v in votes if abs(v)>=.3])
            if len(useful)<3 or abs(useful.mean())<.45 or np.mean(np.sign(useful)==np.sign(useful.mean()))<.75:continue
            for i in range(lo,hi+1):
                if useful.mean()<0:
                    output[i]=output[i,::-1]
                    facts[i]['head_observed'],facts[i]['tail_observed']=facts[i]['tail_observed'],facts[i]['head_observed']
                facts[i].update(head_known=True,head_source='multi_frame_appearance_candidate',manual_lineage=False,
                    appearance_support=len(useful),appearance_score=float(abs(useful.mean())))
    candidate_links=[] if seed is None else recover_flanked_candidates(
        output,facts,fps,seed,excluded,guards,lambda i:fingerprint(gray[i],masks[i],output[i]))
    local_seeds=reseed_clear_components(output,facts,masks,components,guards) if auto_seed else []
    if seed is not None:
        locate_blurred_tips(output,facts,gray,masks,guards,excluded)
    return output,facts,dict(version=VERSION,reference_length_px=reference,tracking_point_count=point_count,
                            anchors=marks,windows=windows,recovery=recovery,head_reacquisition=head_links,
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

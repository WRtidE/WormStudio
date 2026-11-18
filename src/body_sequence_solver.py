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
from head_recovery import end_contrast, appearance_vote, pose_parity
from head_appearance import sample_profile
from head_tail import normalize_anchors

VERSION = 'sam2-body-sequence-v1'

# Include the measurement geometry called by observed_pose, anchor normalization,
# and image appearance, even though those dependencies are imported indirectly.
SIGNATURE_FILES = ('body_sequence_solver.py', 'body_sequence.py', 'extract_body_sequence.py',
                   'centerline_topology.py', 'framewise_pose.py', 'body_geometry.py',
                   'motion.py', 'head_recovery.py', 'head_appearance.py', 'head_tail.py',
                   'analyze_phenotypes.py', 'body_frame_cache.py')


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


def candidates_for(mask,reference,observed):
    if not mask.any():return [],1.
    field,origin=local_field(mask)
    radius=max(1.,float(np.median(field[field>0]))*1.5)
    def keep(candidates):
        out=[]
        for c in candidates:
            candidate_score(c,mask,field,origin,reference,radius,None)
            if (c['inside_fraction']<.97 or c['reconstruction_iou']<.55 or c['local_hairpin']
                    or abs(c['length_px']-reference)>.15*reference+2*radius):continue
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


def fingerprint(gray,mask,line):
    radius=map_coordinates(distance_transform_edt(mask),line.T[::-1],order=1,mode='constant')
    profile=sample_profile(gray,line,radius)
    return np.stack([end_contrast(radius,True),end_contrast(profile)])


def taper_seed(mask,line):
    """Optional shape-only initialization; never claim anatomical confirmation."""
    radii=map_coordinates(distance_transform_edt(mask),line.T[::-1],order=1,mode='constant')
    ratios=[]
    for r in (radii,radii[::-1]):
        if np.median(r[20:31])<=0:return None
        ratios.append(float(np.median(r[2:13])/np.median(r[20:31])))
    if abs(ratios[0]-ratios[1])/max(sum(ratios),1e-9)<.15:return None
    return line[0 if np.argmax(ratios)==0 else -1].copy()


def solve(masks,gray,fps,manual=None,point_count=13,source_invalid=(),auto_seed=True,progress=None):
    """Return 101-point geometry, per-frame endpoint evidence and sequence audit.

    Manual points are crop-local. Explicitly excluded source frames break both
    motion paths and recovery evidence. A missing candidate stays missing.
    """
    from head_recovery import tracking_indices
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
        cs,radius=([],1.) if i in excluded else candidates_for(mask,reference,p)
        frames.append(cs);radii.append(radius)
        if progress:progress(i+1,n,'生成完整虫体与交叉支路候选',.15+.4*(i+1)/max(n,1))
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
                p=chosen[0]['points'];d=np.linalg.norm(p[[0,-1]]-local[0]['point'],axis=1)
                if d.min()>.2*reference:linked=False
                elif d[1]<d[0]:chosen[0].update(points=p[::-1],reverse=True)
        else:
            try:
                chosen,evidence=select_body_sequence(frames[lo:hi+1],gray[lo:hi+1],masks[lo:hi+1],radius,
                    point_count=point_count,pillar_centers=pillars,
                    anchors=[(a['frame']-lo,a['point']) for a in local])
            except BodySequenceBreak as exc:
                cut=lo+exc.index
                queue[:0]=[list(range(lo,cut)),list(range(cut,hi+1))]
                windows.append(dict(start=lo,end=hi,rejected_at=cut,reason='body_correspondence_break'))
                continue
            except ValueError as exc:
                if not local:raise
                linked=False;conflict=True
                chosen,evidence=select_body_sequence(frames[lo:hi+1],gray[lo:hi+1],masks[lo:hi+1],radius,
                    point_count=point_count,pillar_centers=pillars)
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
                candidate_count=len(frames[i]),component=[lo,hi])
        windows.append(dict(start=lo,end=hi,head_linked_to_seed=linked,
            pillar_centers_crop_xy=[p.tolist() for p in pillars],**evidence))
        if progress:progress(hi+1,n,'结合前后帧选择中线和绕柱顺序',.55+.4*(hi+1)/max(n,1))
    components=sorted({tuple(f['component']) for f in facts if f and not f['head_known'] and not f['anchor_conflict']})
    for lo,hi in components:
        prior=[i for i in range(max(0,lo-int(np.ceil(.5*fps))),lo) if facts[i] and facts[i]['head_known']]
        if not prior:continue
        previous=prior[-1]
        if any(i in excluded for i in range(previous,lo+1)):continue
        votes=[pose_parity(output[previous],output[i],tracking_indices(point_count)) for i in range(lo,min(hi+1,lo+3))]
        useful=[v for v in votes if v is not None]
        if len(useful)<2 or len(set(useful))!=1:continue
        for i in range(lo,hi+1):
            if useful[0]==1:
                output[i]=output[i,::-1]
                facts[i]['head_observed'],facts[i]['tail_observed']=facts[i]['tail_observed'],facts[i]['head_observed']
            facts[i].update(head_known=True,head_source='short_gap_body_candidate',manual_lineage=False,
                            preceding_reference=previous,body_votes=len(useful))
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
    return output,facts,dict(version=VERSION,reference_length_px=reference,tracking_point_count=point_count,
                            anchors=marks,windows=windows)


def head_results(quality):
    """Adapt saved orientation once; downstream head heuristics must not flip it."""
    results=[]
    for q in quality:
        f=q.get('body_sequence')
        known=bool(f and f['head_known'])
        observed=bool(known and f['head_observed'] and f['tail_observed'])
        confirmed=bool(observed and f.get('manual_lineage'))
        results.append(dict(head_endpoint=0 if known else None,
            head_status='confirmed_by_user_point' if confirmed else 'unconfirmed_temporal_candidate' if known else 'unconfirmed_A_B_endpoints',
            head_identity_confirmed=confirmed,head_evidence_score=None,
            head_reason=f['head_source'] if f else 'no_supported_body_path',head_segment=f['component'][0] if known else -1,
            head_anchor_count=len(f.get('anchor_frames',[])) if f else 0,
            head_anchor_conflict=bool(f and f.get('anchor_conflict')),head_shape_votes=0,head_motion_votes=0,
            head_input_flipped=False,body_sequence_head_known=known,body_sequence_version=VERSION,
            head_recovery_points=13,head_section_status='body_sequence_estimate' if known and not observed else '',
            head_display_estimated_end=0 if known and not f['head_observed'] else None,
            tail_display_estimated_end=1 if known and not f['tail_observed'] else None))
    return results

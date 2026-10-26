"""Experimental endpoint identity association. Does not infer anatomical head truth."""
import numpy as np
from scipy import ndimage
from scipy.special import expit

VERSION = 'tap-endpoint-groups-v1-experiment'
POLICY = dict(min_visibility=.8,max_entropy=.65,max_outside_px=3.,
              min_group_points=3,min_group_fraction=.5,max_cost_bl=.06,
              min_margin_bl=.08,min_alignment_frames=5,min_alignment_fraction=.8)


def make_queries(curve, mask):
    """Material-point candidates in two terminal regions; labels 0/1 are seed IDs."""
    curve=np.asarray(curve,float);mask=np.asarray(mask,bool)
    if curve.shape!=(101,2) or not np.isfinite(curve).all() or mask.ndim!=2:
        raise ValueError('Expected finite 101-point curve and 2D mask')
    radius=ndimage.distance_transform_edt(mask)
    points=[];groups=[]
    for group,line in enumerate((curve,curve[::-1])):
        for k in (0,3,6,10,15):
            p=line[k];x,y=np.rint(p).astype(int)
            if not (0<=y<mask.shape[0] and 0<=x<mask.shape[1]) or not mask[y,x]:continue
            tangent=line[min(100,k+2)]-line[max(0,k-2)]
            normal=np.array([-tangent[1],tangent[0]])/max(np.linalg.norm(tangent),1e-9)
            for offset in ((0.,) if k==0 else (0.,-.25,.25)):
                q=p+offset*radius[y,x]*normal;qx,qy=np.rint(q).astype(int)
                if not(0<=qy<mask.shape[0] and 0<=qx<mask.shape[1]) or not mask[qy,qx]:continue
                if any(np.linalg.norm(q-v)<.5 for v in points):continue
                points.append(q);groups.append(group)
    points=np.asarray(points,np.float32).reshape(-1,2);groups=np.asarray(groups,int)
    if any(np.sum(groups==g)<3 for g in (0,1)):
        raise ValueError('Insufficient distinct foreground points at both ends')
    return points,groups


def associate(points,groups,visibility_logits,entropy,curve,mask,policy=None):
    """Assign seed group zero to a current curve end, or abstain with diagnostics."""
    cfg={**POLICY,**(policy or {})};curve=np.asarray(curve,float);points=np.asarray(points,float)
    base=dict(endpoint=None,reason='invalid_curve',good_counts=[0,0],costs=None,margin_bl=None)
    if not np.isfinite(curve).all():return base
    length=float(np.linalg.norm(np.diff(curve,axis=0),axis=1).sum())
    if length<=1:return base
    distance=ndimage.distance_transform_edt(~np.asarray(mask,bool))
    safe=np.nan_to_num(points,nan=-1e6)
    outside=ndimage.map_coordinates(distance,[safe[:,1],safe[:,0]],order=1,mode='constant',cval=1e6)
    good=(np.isfinite(points).all(-1)&(expit(visibility_logits)>=cfg['min_visibility'])
          &(np.mean(entropy,axis=-1)<=cfg['max_entropy'])&(outside<=cfg['max_outside_px']))
    counts=[int(np.sum(good&(groups==g))) for g in (0,1)]
    base.update(good_counts=counts,reason='insufficient_visible_group')
    if any(counts[g]<max(cfg['min_group_points'],int(np.ceil(cfg['min_group_fraction']*np.sum(groups==g)))) for g in (0,1)):
        return base
    ends=(curve[:16],curve[-16:]);cost=np.empty((2,2))
    for g in (0,1):
        for e in (0,1):
            d=np.linalg.norm(points[good&(groups==g),None]-ends[e][None],axis=-1).min(-1)
            cost[g,e]=np.median(d)/length
    options=[float((cost[0,0]+cost[1,1])/2),float((cost[0,1]+cost[1,0])/2)]
    choice=int(options[1]<options[0]);margin=abs(options[0]-options[1])
    base.update(costs=cost.tolist(),margin_bl=margin,reason='ambiguous_end_assignment')
    if margin<cfg['min_margin_bl']:return base
    if max(cost[0,choice],cost[1,1-choice])>cfg['max_cost_bl']:
        base['reason']='tracked_group_off_terminal_region';return base
    base.update(endpoint=choice,reason='supported_end_assignment')
    return base


def reconcile(left,right,eligible,policy=None):
    """Align two independently seeded point-group tracks, without anatomical claims."""
    cfg={**POLICY,**(policy or {})}
    votes=[a['endpoint']^b['endpoint'] for a,b,ok in zip(left,right,eligible)
           if ok and a['endpoint'] is not None and b['endpoint'] is not None]
    counts=[votes.count(0),votes.count(1)];parity=int(counts[1]>counts[0])
    accepted=len(votes)>=cfg['min_alignment_frames'] and max(counts)/max(len(votes),1)>=cfg['min_alignment_fraction']
    alignment=dict(accepted=accepted,parity=parity if accepted else None,votes=counts,
                   fraction=max(counts)/max(len(votes),1))
    rows=[]
    for a,b,ok in zip(left,right,eligible):
        endpoint=None
        if not ok:reason='existing_pose_or_identity_gate'
        elif not accepted:reason='independent_tracks_not_aligned'
        elif a['endpoint'] is None or b['endpoint'] is None:reason='one_or_both_tracks_uncertain'
        elif a['endpoint'] != (b['endpoint']^parity):reason='independent_tracks_disagree'
        else:endpoint=a['endpoint'];reason='bidirectional_group_candidate'
        rows.append(dict(endpoint=endpoint,reason=reason,head_identity_confirmed=False))
    return alignment,rows

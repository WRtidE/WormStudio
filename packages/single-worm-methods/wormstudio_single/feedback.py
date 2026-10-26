"""Bounded single-instance feedback policy. Scores are engineering gates, not probabilities of correctness.

No reference annotations are consumed. A held-out subset of queries never becomes a SAM prompt.
Cycle checks use forward endpoints to seed reverse tracking: they are not independent identity evidence.
"""
from dataclasses import dataclass, asdict
import numpy as np
from scipy import ndimage as ndi
from scipy.special import expit

VERSION='sam2-tap-feedback-v1-experimental'

@dataclass(frozen=True)
class FeedbackPolicy:
    query_count: int=32
    radius: int=8
    min_visible_probability: float=.8
    max_cycle_px: float=2.
    max_agreement_px: float=2.
    max_entropy: float=.7
    min_reliable: int=8
    min_missing: int=3
    min_outside_px: float=1.5
    max_outside_px: float=12.
    min_holdout: int=3
    min_holdout_gain: float=.10
    max_holdout_loss: float=.02
    max_change_fraction: float=.40
    min_area_ratio: float=.60
    max_area_ratio: float=1.60

    def __post_init__(self):
        for k in ('query_count','radius','min_reliable','min_missing','min_holdout'):
            v=getattr(self,k)
            if isinstance(v,bool) or not isinstance(v,int) or v<1:raise ValueError(k+' must be a positive integer')
        if not 12<=self.query_count<=64 or self.radius>30:raise ValueError('query_count12..64 and radius1..30 required')
        for k,v in asdict(self).items():
            if not np.isfinite(v):raise ValueError('Nonfinite policy '+k)
        if not 0<self.min_visible_probability<1 or not 0<self.max_entropy<=1:raise ValueError('Invalid confidence thresholds')
        if not 0<self.min_area_ratio<=1<=self.max_area_ratio:raise ValueError('Invalid area limits')
        if not 0<self.min_outside_px<self.max_outside_px or min(self.max_cycle_px,self.max_agreement_px)<=0:raise ValueError('Invalid distances')
        if not 0<self.min_holdout_gain<=1 or not 0<=self.max_holdout_loss<=1 or not 0<self.max_change_fraction<1:raise ValueError('Invalid acceptance limits')

def sample_queries(mask,count=32,margin=1.):
    mask=np.asarray(mask,bool);distance=ndi.distance_transform_edt(mask)
    y,x=np.where(mask&(distance>=margin))
    if len(x)<count:return np.empty((0,2),np.float32)
    candidates=np.column_stack([x,y]);chosen=[int(np.argmax(distance[y,x]))]
    d=np.sum((candidates-candidates[chosen[0]])**2,axis=1).astype(float)
    for _ in range(1,count):
        k=int(d.argmax());chosen.append(k);d=np.minimum(d,np.sum((candidates-candidates[k])**2,axis=1));d[chosen]=-1
    return candidates[chosen].astype(np.float32)

def distances(mask,points):
    p=np.asarray(points,float);safe=np.nan_to_num(p,nan=-1e6,posinf=-1e6,neginf=-1e6)
    return ndi.map_coordinates(ndi.distance_transform_edt(~np.asarray(mask,bool)),[safe[:,1],safe[:,0]],order=1,mode='constant',cval=1e6)

def reliable_tracks(tracks,shape,policy):
    f=np.asarray(tracks['forward']);b=np.asarray(tracks['backward']);t,q,_=f.shape
    if f.shape!=b.shape or f.shape[-1]!=2:raise ValueError('Track shapes differ')
    h,w=shape
    finite=np.isfinite(f).all(-1)&np.isfinite(b).all(-1)
    in_bounds=(f[...,0]>=0)&(f[...,0]<w)&(f[...,1]>=0)&(f[...,1]<h)&(b[...,0]>=0)&(b[...,0]<w)&(b[...,1]>=0)&(b[...,1]<h)
    good=finite&in_bounds
    for direction in ('forward','backward'):
        good &= np.asarray(tracks[direction+'_visible'],bool)
        logits=np.asarray(tracks[direction+'_visibility_logits']);entropy=np.asarray(tracks[direction+'_entropy'])
        good &= np.isfinite(logits)&np.isfinite(entropy).all(-1)
        good &= (expit(logits)>=policy.min_visible_probability)&(entropy.mean(-1)<=policy.max_entropy)
    cycle=np.linalg.norm(b[0]-np.asarray(tracks['queries']),axis=-1)
    agreement=np.linalg.norm(f-b,axis=-1)
    good &= (cycle[None]<=policy.max_cycle_px)&(agreement<=policy.max_agreement_px)
    return good,dict(cycle_error_px=cycle,agreement_px=agreement)

def plan_feedback(masks,tracks,policy=FeedbackPolicy()):
    masks=np.asarray(masks,bool);t,h,w=masks.shape;good,diag=reliable_tracks(tracks,(h,w),policy)
    q=good.shape[1];heldout=np.arange(q)%3==0;prompt_pool=~heldout;f=tracks['forward']
    diagnostics=[];choices=[]
    # Both endpoint masks are immutable trusted references supplied by the caller.
    for i in range(1,t-1):
        valid=good[i]&prompt_pool;d=distances(masks[i],f[i]);missing=valid&(d>policy.min_outside_px)&(d<=policy.max_outside_px)
        diagnostics.append(dict(frame=i,reliable_prompt_queries=int(valid.sum()),missing_queries=int(missing.sum())))
        if valid.sum()>=policy.min_reliable and missing.sum()>=policy.min_missing:
            points=f[i,valid]
            if np.linalg.norm(np.ptp(points,axis=0))>=6:choices.append((int(missing.sum()),i,missing,valid,d))
    base=dict(version=VERSION,policy=asdict(policy),heldout_query_ids=np.flatnonzero(heldout).tolist(),frames=diagnostics,
              cycle_check='reverse initialized from forward endpoints; consistency only, not independent identity truth')
    if not choices:return dict(base,status='no_trigger',reason='no_frame_with_sufficient_visible_consistent_mask_disagreement')
    _,i,missing,valid,d=max(choices,key=lambda r:(r[0],-r[1]))
    # Two disagreeing points plus two retained interior points, all outside held-out bank.
    outside=np.flatnonzero(missing);outside=outside[np.argsort(-d[outside],kind='stable')][:2]
    inside=np.flatnonzero(valid&(d<=.5))[:2];ids=np.concatenate([outside,inside])
    positives=f[i,ids]
    if len(inside)<2:return dict(base,status='no_trigger',reason='insufficient_retained_interior_prompts')
    # Negatives are distant corners outside ALL original masks and all reliable tracked locations.
    union=masks.any(0);negatives=[]
    for p in np.array([[2,2],[w-3,2],[2,h-3],[w-3,h-3]],float):
        tracked=f[good&prompt_pool[None,:]]
        if distances(union,p[None])[0]>=12 and (not len(tracked) or np.linalg.norm(tracked-p,axis=-1).min()>=12):negatives.append(p)
    negatives=np.asarray(negatives,dtype=np.float32).reshape(-1,2)
    lo=max(1,i-policy.radius);hi=min(t-2,i+policy.radius)
    return dict(base,status='proposed',frame=i,start=lo,stop=hi+1,prompt_query_ids=ids.tolist(),
                points=np.concatenate([positives,negatives]).tolist(),labels=[1]*len(positives)+[0]*len(negatives),
                reason='visible_cycle_consistent_tracks_outside_current_mask')

def judge_proposal(before,candidate,tracks,plan,policy=FeedbackPolicy()):
    before=np.asarray(before,bool);candidate=np.asarray(candidate,bool)
    if candidate.shape!=before.shape:raise ValueError('Candidate mask shape mismatch')
    if plan['status']!='proposed':return dict(accepted=False,reasons=['no_proposal'])
    lo,hi=plan['start'],plan['stop'];good,_=reliable_tracks(tracks,before.shape[1:],policy)
    heldout=np.arange(good.shape[1])%3==0;rows=[];reasons=[];old_hits=[];new_hits=[];losses=[]
    if not np.array_equal(before[:lo],candidate[:lo]) or not np.array_equal(before[hi:],candidate[hi:]):reasons.append('changed_outside_budget')
    reference_area=float(np.median([before[0].sum(),before[-1].sum()]));changed=[]
    for i in range(lo,hi):
        a,b=before[i],candidate[i];delta=int((a^b).sum())
        if not delta:continue
        changed.append(i);ratio=b.sum()/max(reference_area,1);fraction=delta/max(int((a|b).sum()),1)
        if not policy.min_area_ratio<=ratio<=policy.max_area_ratio:reasons.append('area_guard')
        if fraction>policy.max_change_fraction:reasons.append('change_budget')
        _,components=ndi.label(b,np.ones((3,3)))
        _,old_components=ndi.label(a,np.ones((3,3)))
        if components>max(1,old_components):reasons.append('new_disconnected_component')
        ids=np.flatnonzero(good[i]&heldout)
        if len(ids)<policy.min_holdout:reasons.append('insufficient_holdout_tracks');continue
        points=tracks['forward'][i,ids];old=distances(a,points)<=.5;new=distances(b,points)<=.5
        if float((old&~new).mean())>policy.max_holdout_loss:reasons.append('frame_lost_supported_region')
        old_hits.extend(old.tolist());new_hits.extend(new.tolist());losses.extend((old&~new).tolist())
        rows.append(dict(frame=i,changed_pixels=delta,area_ratio=float(ratio),change_fraction=float(fraction),heldout_queries=len(ids),old_coverage=float(old.mean()),new_coverage=float(new.mean())))
    if not changed:reasons.append('no_mask_change')
    gain=float(np.mean(new_hits)-np.mean(old_hits)) if old_hits else 0.
    loss=float(np.mean(losses)) if losses else 0.
    if gain<policy.min_holdout_gain:reasons.append('insufficient_holdout_gain')
    if loss>policy.max_holdout_loss:reasons.append('lost_supported_region')
    i=plan['frame'];points=np.asarray(plan['points']);labels=np.asarray(plan['labels']);inside=distances(candidate[i],points)<=.5
    if not inside[labels==1].all() or inside[labels==0].any():reasons.append('prompt_violation')
    return dict(accepted=not reasons,reasons=sorted(set(reasons)),changed_frames=changed,heldout_gain=gain,heldout_loss=loss,frames=rows,
                scope='internal consistency acceptance, not ground-truth accuracy or identity certification')

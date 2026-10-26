"""Image corroboration for local feedback. Photometric classes are NOT identity labels."""
from dataclasses import dataclass,asdict
import numpy as np
from scipy import ndimage as ndi
import cv2
from .feedback import FeedbackPolicy,distances
from .feedback_local import LocalPolicy,local_plan,local_judge

VERSION='sam2-tap-image-feedback-v3-experimental'

@dataclass(frozen=True)
class ImagePolicy:
    min_contrast: float=8.
    min_contrast_noise_ratio: float=3.
    foreground_score: float=.65
    background_score: float=.35
    min_path_score: float=.50
    max_added_background_fraction: float=.10
    max_added_unknown_fraction: float=.25

    def __post_init__(self):
        if not np.isfinite(list(asdict(self).values())).all():raise ValueError('Finite image thresholds required')
        if self.min_contrast<=0 or self.min_contrast_noise_ratio<=0:raise ValueError('Positive contrast required')
        if not 0<self.background_score<self.min_path_score<self.foreground_score<1:raise ValueError('Ordered score gates required')
        if not 0<=self.max_added_background_fraction<1 or not 0<=self.max_added_unknown_fraction<1:raise ValueError('Invalid addition budgets')

def image_field(frame,mask,policy=ImagePolicy()):
    """Robust, polarity-independent foreground/background contrast from original mask.

    Low contrast and complex multimodal backgrounds abstain. The field is an
    intensity descriptor; same-looking objects and body-part identity remain unresolved.
    """
    mask=np.asarray(mask,bool);gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY).astype(float) if frame.ndim==3 else np.asarray(frame,float)
    smooth=ndi.median_filter(gray,size=3)
    inside=ndi.distance_transform_edt(mask);outside=ndi.distance_transform_edt(~mask)
    radius=max(1.,min(5.,float(np.median(inside[mask])))) if mask.any() else 1.
    core=mask&(inside>=max(1.5,radius));ring=(outside>=radius+1)&(outside<=radius+5)
    if core.sum()<20 or ring.sum()<20:return dict(valid=False,reason='insufficient_core_or_ring',score=np.full(mask.shape,np.nan))
    fg=float(np.median(smooth[core]));bg=float(np.median(smooth[ring]));noise=max(1.,1.4826*float(np.median(abs(smooth[ring]-bg))))
    contrast=abs(fg-bg);valid=contrast>=policy.min_contrast and contrast/noise>=policy.min_contrast_noise_ratio
    score=(smooth-bg)/(fg-bg) if valid else np.full(mask.shape,np.nan)
    return dict(valid=bool(valid),reason='contrast_available' if valid else 'ambiguous_contrast',foreground=fg,background=bg,noise=noise,contrast=contrast,score=score,core=core)

def point_evidence(field,mask,points,policy=ImagePolicy()):
    points=np.asarray(points,float);rows=[];supported=np.zeros(len(points),bool)
    if not field['valid']:
        return supported,[dict(label='unknown_contrast',score=None,path_min=None) for _ in points]
    score=field['score'];h,w=mask.shape;core=field['core'];_,nearest=ndi.distance_transform_edt(~core,return_indices=True)
    for j,(x,y) in enumerate(points):
        if not np.isfinite([x,y]).all() or not (0<=x<w and 0<=y<h):rows.append(dict(label='invalid',score=None,path_min=None));continue
        ix,iy=int(round(x)),int(round(y));ix=min(ix,w-1);iy=min(iy,h-1);cy,cx=nearest[:,iy,ix]
        length=max(3,int(np.ceil(np.hypot(x-cx,y-cy)*2))+1);xx=np.linspace(cx,x,length);yy=np.linspace(cy,y,length)
        values=ndi.map_coordinates(score,[yy,xx],order=1,mode='nearest');value=float(values[-1]);minimum=float(values.min())
        # Foreground-background transition on the route indicates crossing an image edge.
        edge=bool(np.any(values<=policy.background_score) and values[0]>=policy.foreground_score)
        if value<=policy.background_score:label='background_drift'
        elif edge:label='edge_crossing_or_other_object'
        elif value>=policy.foreground_score and minimum>=policy.min_path_score:label='foreground_supported';supported[j]=True
        else:label='ambiguous_boundary'
        rows.append(dict(label=label,score=value,path_min=minimum,edge_barrier=edge))
    return supported,rows

def image_plan(frames,masks,tracks,policy=FeedbackPolicy(),local=LocalPolicy(),image_policy=ImagePolicy()):
    """Only outside points require additional image support; no thresholds loosened."""
    filtered={k:v.copy() for k,v in tracks.items()};records=[]
    # Held-out point values never enter prompt construction or its global statistics.
    prompt_ids=np.flatnonzero(np.arange(len(tracks['queries']))%3!=0)
    for i,(frame,mask) in enumerate(zip(frames,masks)):
        field=image_field(frame,mask,image_policy);p=tracks['forward'][i,prompt_ids];support,rows=point_evidence(field,mask,p,image_policy)
        outside=distances(mask,p)>policy.min_outside_px
        filtered['forward_visible'][i,prompt_ids] &= ~outside|support
        records.append(dict(frame=i,field={k:v for k,v in field.items() if k not in ('score','core')},points=[dict(query=int(j),outside=bool(o),**r) for j,o,r in zip(prompt_ids,outside,rows)]))
    plan=local_plan(masks,filtered,policy,local);plan['image_policy']=asdict(image_policy);plan['image_evidence']=records
    if plan['status']=='proposed':
        # Freeze the modification interval using prompt-pool evidence only. No
        # candidate or held-out gain is used to select which frames may change.
        supported={r['frame'] for r in plan['frames'] if r['missing_queries']>=policy.min_missing and r['reliable_prompt_queries']>=policy.min_reliable}
        i=plan['frame'];lo,hi=i,i+1;old_lo,old_hi=plan['start'],plan['stop']
        while lo>old_lo and lo-1 in supported:lo-=1
        while hi<old_hi and hi in supported:hi+=1
        plan['propagation_budget_before_image']=[old_lo,old_hi]
        plan['start'],plan['stop']=lo,hi
        plan['modification_budget']='contiguous image-supported prompt evidence around trigger, frozen before segmentation'
    plan['version']=VERSION
    return plan

def image_judge(frames,before,candidate,tracks,plan,policy=FeedbackPolicy(),local=LocalPolicy(),image_policy=ImagePolicy()):
    # Keep ALL original local acceptance rules. Image evidence can veto, not excuse loss.
    decision=local_judge(before,candidate,tracks,plan,policy,local);rows=[];reasons=list(decision['reasons'])
    for i in range(plan['start'],plan['stop']):
        added=candidate[i]&~before[i]
        if not added.any():continue
        field=image_field(frames[i],before[i],image_policy)
        if not field['valid']:reasons.append('added_region_unknown_contrast');rows.append(dict(frame=i,added=int(added.sum()),valid=False));continue
        score=field['score'][added];background=float((score<=image_policy.background_score).mean());unknown=float(((score>image_policy.background_score)&(score<image_policy.foreground_score)).mean())
        if background>image_policy.max_added_background_fraction:reasons.append('added_region_background')
        if unknown>image_policy.max_added_unknown_fraction:reasons.append('added_region_ambiguous')
        rows.append(dict(frame=i,added=int(added.sum()),valid=True,background_fraction=background,ambiguous_fraction=unknown))
    decision.update(accepted=not reasons,reasons=sorted(set(reasons)),image_added_regions=rows)
    return decision

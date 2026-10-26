"""Local event proposal generation. Frozen v1 and original v4 remain separate."""
import gc
from dataclasses import dataclass,asdict,replace
import numpy as np
from scipy import ndimage as ndi
from .feedback import FeedbackPolicy,sample_queries,distances,reliable_tracks,plan_feedback,judge_proposal

VERSION='sam2-tap-local-feedback-v2-experimental'

@dataclass(frozen=True)
class LocalPolicy:
    window: int=17
    stride: int=8
    max_attempts: int=3
    persistence: int=2
    mass_radius_px: float=2.
    min_local_mass: float=.25
    min_mass_enrichment: float=3.

    def __post_init__(self):
        for k in ('window','stride','max_attempts','persistence'):
            v=getattr(self,k)
            if isinstance(v,bool) or not isinstance(v,int) or v<1:raise ValueError('Positive integer required: '+k)
        if self.window<5 or self.window>65 or self.window%2!=1:raise ValueError('Odd window5..65 required')
        if self.stride>=self.window or self.max_attempts>8:raise ValueError('Invalid stride/attempt budget')
        if self.persistence>self.window-2:raise ValueError('Persistence exceeds interior window')
        if not np.isfinite([self.mass_radius_px,self.min_local_mass,self.min_mass_enrichment]).all():raise ValueError('Finite mass gates required')
        if not 0<self.mass_radius_px<=4 or not 0<self.min_local_mass<1 or self.min_mass_enrichment<=1:raise ValueError('Invalid mass gates')

def windows(n,policy=LocalPolicy()):
    if n<3:raise ValueError('At least3 frames required')
    starts=list(range(0,max(1,n-policy.window+1),policy.stride))
    last=max(0,n-policy.window)
    if starts[-1]!=last:starts.append(last)
    return [(s,min(n,s+policy.window)) for s in starts]

def track_window(model,frames,masks,policy=FeedbackPolicy()):
    from .inference import track
    queries=sample_queries(masks[0],policy.query_count)
    if not len(queries):return None
    f,fv,fe=track(model,frames,queries,with_evidence=True)
    # Track points outside the current final mask too: mask membership is precisely
    # what feedback evaluates. Visibility, bounds and return cycle still gate them.
    h,w=masks.shape[1:];end=f[-1]
    eligible=np.isfinite(end).all(-1)&fv[-1]&(end[:,0]>=0)&(end[:,0]<w)&(end[:,1]>=0)&(end[:,1]<h)
    ids=np.flatnonzero(eligible);b=np.full_like(f,np.nan);bv=np.zeros_like(fv);be={k:np.full_like(v,np.nan) for k,v in fe.items()}
    if len(ids):
        rb,rv,re=track(model,frames[::-1],end[ids],with_evidence=True);b[:,ids]=rb[::-1];bv[:,ids]=rv[::-1]
        for k,v in re.items():be[k][:,ids]=v[::-1]
    out=dict(queries=queries,forward=f,backward=b,forward_visible=fv,backward_visible=bv)
    out.update({'forward_'+k:v for k,v in fe.items()});out.update({'backward_'+k:v for k,v in be.items()})
    return out

def concentrated_tracks(tracks,shape,local=LocalPolicy()):
    """Measure each marginal's actual mass near its decoded point, not global entropy.

    A uniform or distant multimodal head fails even when a cycle closes. This is
    engineering support, not calibrated probability of point identity.
    """
    filtered={k:v.copy() for k,v in tracks.items()};diagnostic={}
    for direction in ('forward','backward'):
        probs=tracks[direction+'_coordinate_probabilities'];points=tracks[direction]
        mass=[];enrichment=[]
        for axis,extent in enumerate((shape[1],shape[0])):
            bins=(np.arange(probs.shape[-1])+.5)*extent/probs.shape[-1]
            near=abs(bins[None,None,:]-points[...,axis,None])<=local.mass_radius_px
            m=np.sum(np.where(near,probs[...,axis,:],0.),-1)
            uniform=near.mean(-1)
            mass.append(m);enrichment.append(m/np.maximum(uniform,1e-12))
        mass=np.stack(mass,-1);enrichment=np.stack(enrichment,-1)
        valid=np.isfinite(mass).all(-1)&np.isfinite(enrichment).all(-1)&(mass.min(-1)>=local.min_local_mass)&(enrichment.min(-1)>=local.min_mass_enrichment)
        filtered[direction+'_visible'] &= valid
        diagnostic[direction+'_mass']=mass;diagnostic[direction+'_concentrated']=valid
    return filtered,diagnostic

def local_plan(masks,tracks,policy=FeedbackPolicy(),local=LocalPolicy()):
    """Require the same missing point in adjacent frames before proposing a repair."""
    filtered,diag=concentrated_tracks(tracks,masks.shape[1:],local)
    effective=replace(policy,max_entropy=1.)
    good,_=reliable_tracks(filtered,masks.shape[1:],effective)
    d=np.array([distances(m,p) for m,p in zip(masks,tracks['forward'])])
    missing=good&(d>policy.min_outside_px)&(d<=policy.max_outside_px)
    persistent=np.zeros_like(missing)
    for i in range(len(masks)-local.persistence+1):
        run=missing[i:i+local.persistence].all(0)
        persistent[i:i+local.persistence] |= run
    # Suppress isolated outside points, retaining reliable inside points.
    filtered['forward_visible'] &= ~missing|persistent
    plan=plan_feedback(masks,filtered,effective)
    plan['persistence_frames']=local.persistence
    plan['local_policy']=asdict(local)
    plan['confidence_rule']='mass within2px on each marginal plus enrichment over uniform; visibility/cycle/agreement retained'
    plan['max_reliable_prompt_queries']=int(good[:,np.arange(good.shape[1])%3!=0].sum(1).max())
    return plan

def local_judge(before,candidate,tracks,plan,policy=FeedbackPolicy(),local=LocalPolicy()):
    filtered,_=concentrated_tracks(tracks,before.shape[1:],local)
    return judge_proposal(before,candidate,filtered,plan,replace(policy,max_entropy=1.))

def select_attempts(records,local=LocalPolicy()):
    """Freeze disjoint proposals using prompt evidence only, before seeing candidates."""
    proposed=[r for r in records if r['plan']['status']=='proposed']
    proposed.sort(key=lambda r:(-max(x['missing_queries'] for x in r['plan']['frames']),r['start']))
    chosen=[]
    for r in proposed:
        if any(max(r['start'],x['start'])<min(r['stop'],x['stop']) for x in chosen):continue
        chosen.append(r)
        if len(chosen)>=local.max_attempts:break
    return chosen

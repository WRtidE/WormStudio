"""Image-constrained temporal selection, with explicit missing-pose output.

Clear two-tip masks supply their own medial path. Point tracks orient that path
and resolve contact alternatives; they cannot replace a clear path with drift.
Every displayed curve must pass the same hard geometry checks before selection.
"""
import numpy as np
from scipy import ndimage as ndi
from .quality import INDICES,full_line,curve_measures
from .geometry import topology_candidates,resample
from .skeleton import extract_niu,valid_line

VERSION='image-constrained-temporal-v3-contact-events'

def feasible_quality(p,mask,reference):
    p=np.asarray(p,float)
    if p.shape!=(101,2) or not np.isfinite(p).all():return dict(finite=False,geometric_pass=False)
    if np.linalg.norm(np.diff(p,axis=0),axis=1).sum()<1:
        return dict(finite=True,geometric_pass=False,length_px=0.,rejection_reason='degenerate_curve')
    qa=curve_measures(p,mask,reference)
    if not qa.get('finite'):return qa
    # Test the segments, not just the 101 vertices: a shortcut may cross a
    # background gap even if its endpoints are inside the worm.
    length=qa['length_px'];dense=resample(p,max(101,int(np.ceil(2*length))+1))
    outside=ndi.distance_transform_edt(~mask)
    distance=ndi.map_coordinates(outside,[dense[:,1],dense[:,0]],order=1,mode='constant',cval=1e6)
    tip_distance=ndi.map_coordinates(outside,[p[[0,-1],1],p[[0,-1],0]],order=1,mode='constant',cval=1e6)
    inside=ndi.map_coordinates(mask.astype(float),[dense[:,1],dense[:,0]],order=0,mode='constant',cval=0)
    qa.update(dense_inside_fraction=float(inside.mean()),max_outside_distance_px=float(distance.max()),
        max_tip_outside_distance_px=float(tip_distance.max()))
    qa['geometric_pass']=bool(qa['geometric_pass'] and qa['dense_inside_fraction']>=.97
        and qa['max_outside_distance_px']<=1.5 and qa['max_tip_outside_distance_px']<=1.5)
    return qa

def frame_candidates(mask,reference,observations,mask_valid=True):
    """Reject inadmissible hypotheses BEFORE they enter the temporal solver."""
    if not mask_valid or not mask.any():return [],dict(reason='invalid_mask',rejected=0)
    niu=extract_niu(mask);states=[];rejected=0
    if niu['topology']=='simple' and valid_line(niu['ordered']):
        # A unique observable body path is stronger than generic point tracks.
        source=[dict(points=niu['ordered'],name='robust_medial',origin='image')]
    else:
        graph,_,_=topology_candidates(mask)
        source=[dict(points=c['points'],name=c['name'],origin='image') for c in graph]
        for name,points,visible in observations:
            if np.isfinite(points).all() and np.asarray(visible).sum()>=4:
                source.append(dict(points=full_line(points),name=name,origin='motion'))
    for c in source:
        quality=feasible_quality(c['points'],mask,reference)
        if not quality['geometric_pass']:
            rejected+=1;continue
        for reverse in (False,True):
            states.append(dict(c,points=c['points'][::-1].copy() if reverse else c['points'].copy(),quality=quality))
    return states,dict(reason='' if states else 'no_image_supported_centerline',
        robust_topology=niu['topology'],rejected=rejected)

def select_segment(candidates,prior,scale):
    if len(candidates)==1:return [int(np.argmin([x['unary'] for x in candidates[0]]))]
    def transition(a,b,i):
        step=b['points']-a['points'];expected=full_line(prior[i]-prior[i-1])
        return .15*float(np.minimum(np.sum((step-expected)**2,axis=1)/scale**2,25).mean())
    cost=np.array([[a['unary']+b['unary']+transition(a,b,1) for b in candidates[1]] for a in candidates[0]])
    pointers=[]
    for i in range(2,len(candidates)):
        updated=np.full((len(candidates[i-1]),len(candidates[i])),np.inf);ptr=np.zeros(updated.shape,int)
        for j,bstate in enumerate(candidates[i-1]):
            for k,cstate in enumerate(candidates[i]):
                values=[]
                for h,astate in enumerate(candidates[i-2]):
                    acc=cstate['points']-2*bstate['points']+astate['points']
                    penalty=.2*float(np.minimum(np.sum(acc**2,axis=1)/scale**2,25).mean())
                    values.append(cost[h,j]+penalty)
                ptr[j,k]=np.argmin(values);updated[j,k]=min(values)+cstate['unary']+transition(bstate,cstate,i)
        pointers.append(ptr);cost=updated
    j,k=np.unravel_index(cost.argmin(),cost.shape);chosen=[int(k),int(j)]
    for ptr in pointers[::-1]:chosen.append(int(ptr[chosen[-1],chosen[-2]]))
    return chosen[::-1]

def fit(d,z):
    ref=float(d['reference_length']);scale=max(5,ref*.03)
    f,b=np.asarray(z['forward']),np.asarray(z['backward']);fv,bv=z['forward_visible'],z['backward_visible'];t=len(f)
    has_end=np.isfinite(d['last']).all() and np.isfinite(b).all()
    if has_end:
        w=np.linspace(0,1,t)[:,None,None];linear=(1-w)*d['first'][None,INDICES]+w*d['last'][None,INDICES]
        both=fv&bv&(np.linalg.norm(f-b,axis=2)<scale)
        prior=np.where(both[...,None],(1-w)*f+w*b,linear)
    else:prior=np.where(np.isfinite(f),f,d['first'][None,INDICES])
    candidates=[];diagnostics=[]
    for i,mask in enumerate(d['masks']):
        observations=[('motion_forward',f[i],fv[i]),('motion_backward',b[i],bv[i])]
        states,info=frame_candidates(mask,ref,observations,bool(d['mask_valid'][i]))
        anchor=d['first'] if i==0 else (d['last'] if i==t-1 and has_end else None)
        if anchor is not None and d['mask_valid'][i]:
            aq=feasible_quality(anchor,mask,ref)
            if aq['geometric_pass']:states=[dict(points=anchor.copy(),name='first_anchor' if i==0 else 'last_anchor',origin='anchor',quality=aq)]
        for c in states:
            p=c['points'];qa=c['quality'];distances=[]
            for _,obs,visible in observations:
                use=np.asarray(visible)&np.isfinite(obs).all(1)
                if use.sum()>=4:
                    err=np.linalg.norm(p[INDICES][use]-obs[use],axis=1)/scale
                    distances.append(float(np.minimum(err**2,25).mean()))
            c['unary']=12*(1-qa['inside_fraction'])+8*qa['length_relative_deviation']+3*(1-qa['skeleton_coverage'])+(min(distances) if distances else 0.)
        candidates.append(states);diagnostics.append(info)
    lines=np.full((t,101,2),np.nan)
    quality=[dict(frame=int(d['frame_ids'][i]),candidate='none',status='UNAVAILABLE',
        mask_valid=bool(d['mask_valid'][i]),finite=False,geometric_pass=False,
        abstention_reason=info['reason'],rejected_candidates=info['rejected']) for i,info in enumerate(diagnostics)]
    available=np.flatnonzero([bool(c) for c in candidates])
    for ids in np.split(available,np.where(np.diff(available)>1)[0]+1):
        if not len(ids):continue
        chosen=select_segment([candidates[i] for i in ids],prior[ids],scale)
        for i,k in zip(ids,chosen):
            c=candidates[i][k];lines[i]=c['points']
            quality[i]=dict(frame=int(d['frame_ids'][i]),candidate=c['name'],origin=c['origin'],status='UNCONFIRMED',
                mask_valid=bool(d['mask_valid'][i]),rejected_candidates=diagnostics[i]['rejected'],**c['quality'])
    # A missing contact-frame candidate should not split an otherwise
    # bracketed event into unrelated pieces. Restore REVIEW hypotheses using
    # the entire window, while retaining all hard geometry checks.
    if has_end and not np.isfinite(lines).all() and np.asarray(d['mask_valid']).all():
        from .contact import recover
        restored,record=recover(d)
        if restored is not None:
            for i,points in enumerate(restored):
                if diagnostics[i].get('robust_topology')=='simple':continue
                lines[i]=points;quality[i]=dict(record['quality'][i],
                    event_anchors=record['anchors'],event_cost=record['cost'])
    return lines,quality

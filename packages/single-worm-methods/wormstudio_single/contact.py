"""Whole-event self-contact hypotheses with observed and hidden endpoint provenance.

This module restores review candidates only. Geometry feasibility does not
confirm which material segment lies above the other in a monocular image.
"""
import numpy as np,cv2
from scipy import ndimage as ndi
from skimage.morphology import skeletonize
from .fusion import feasible_quality
from .geometry import (topology_candidates,hidden_tip_candidates,
    longest_chain,smooth_chain,extend_tip,connector,resample)
from .skeleton import extract_niu,valid_line

VERSION='whole-contact-event-v1'

def short_tip_candidates(mask):
    """Try smaller contact zones so a short visible endpoint is not erased."""
    yy,xx=np.nonzero(mask)
    if not len(xx):return []
    x0=max(0,int(xx.min())-4);y0=max(0,int(yy.min())-4)
    local=mask[y0:yy.max()+5,x0:xx.max()+5];sk=skeletonize(local)
    degree=ndi.convolve(sk.astype(int),np.ones((3,3)),mode='constant')-sk
    ends=np.argwhere(sk&(degree==1));branches=sk&(degree>2)
    if len(ends)!=2 or not branches.any():return []
    dt=ndi.distance_transform_edt(local);radius=float(np.median(dt[sk]))
    labels,n=ndi.label(branches,np.ones((3,3)))
    centers=np.array([np.argwhere(labels==i).mean(0)[::-1] for i in range(1,n+1)])
    if n>4 or np.ptp(centers,axis=0).max()>max(8*radius,30):return []
    result=[]
    for fraction in [.35,.55,.75]:
        r=max(2,round(radius*fraction));zone=np.zeros_like(local,np.uint8)
        for c in centers:cv2.circle(zone,tuple(np.rint(c).astype(int)),r,1,-1)
        for c in centers[1:]:cv2.line(zone,tuple(np.rint(centers[0]).astype(int)),tuple(np.rint(c).astype(int)),1,2*r)
        lab,count=ndi.label(sk&~zone.astype(bool),np.ones((3,3)));dz=ndi.distance_transform_edt(~zone.astype(bool));tails=[];loops=[]
        for k in range(1,count+1):
            part=lab==k
            if part.sum()<3:continue
            try:p,coverage=longest_chain(part)
            except ValueError:continue
            if coverage<.9:continue
            p=smooth_chain(p);idx=np.rint(p[[0,-1]]).astype(int);contact=dz[idx[:,1],idx[:,0]]<2.5
            if contact.all():loops.append(p)
            elif contact.sum()==1:tails.append(p if contact[-1] else p[::-1])
        if len(tails)!=2 or len(loops)!=1:continue
        a=extend_tip(tails[0],local);b=extend_tip(tails[1],local)[::-1]
        for reverse in [False,True]:
            mid=loops[0][::-1] if reverse else loops[0]
            bridge1,cost1=connector(a,mid,radius);bridge2,cost2=connector(mid,b,radius)
            p=np.vstack([a,bridge1[1:-1],mid,bridge2[1:-1],b])
            result.append(dict(points=resample(p)+[x0,y0],name=f'short_tip_{fraction}_{int(reverse)}',
                endpoint_observed=[True,True],connection_cost=cost1+cost2))
    return result

def candidate_states(mask,reference,mask_valid=True):
    if not mask_valid:return []
    niu=extract_niu(mask)
    if niu['topology']=='simple' and valid_line(niu['ordered']):
        source=[dict(points=niu['ordered'],name='robust_medial',endpoint_observed=[True,True],connection_cost=0.)]
    else:
        graph,_,_=topology_candidates(mask)
        source=[dict(c,endpoint_observed=[True,True]) for c in graph]
        short=short_tip_candidates(mask);source.extend(short)
        # Do not replace visible tips with a length-invented tip when an
        # image-supported two-tip hypothesis is already available.
        if not any(feasible_quality(c['points'],mask,reference)['geometric_pass'] for c in source):
            source.extend(hidden_tip_candidates(mask,reference))
    states=[]
    for c in source:
        q=feasible_quality(c['points'],mask,reference)
        if not q['geometric_pass']:continue
        for reverse in [False,True]:
            p=c['points'][::-1].copy() if reverse else c['points'].copy()
            observed=c['endpoint_observed'][::-1] if reverse else list(c['endpoint_observed'])
            # Avoid duplicate state paths from contact-zone radius variants.
            if any(np.linalg.norm(s['points']-p,axis=1).mean()<.5 for s in states):continue
            # Hidden endpoints are weakly observed: prevent the optimizer
            # shortening the animal merely to make endpoint motion cheaper.
            unary=(q['length_relative_deviation']/.03)**2+4*(1-q['skeleton_coverage'])+12*(1-q['dense_inside_fraction'])
            states.append(dict(c,points=p,endpoint_observed=observed,quality=q,unary=unary))
    return states

def _transition(a,b,scale):
    delta=b-a
    body=np.mean(np.sum(delta**2,axis=-1),axis=-1)
    tips=np.mean(np.sum(delta[..., [0,-1],:]**2,axis=-1),axis=-1)
    return (body+1.5*tips)/scale**2

def select_event(states,reference):
    """Second-order DP over body correspondence, endpoints and acceleration."""
    n=len(states);scale=max(5,.08*reference)
    if n==1:return [int(np.argmin([c['unary'] for c in states[0]]))],0.
    points=[np.array([c['points'] for c in choices]) for choices in states]
    unary=[np.array([c['unary'] for c in choices]) for choices in states]
    transitions=[_transition(points[i][:,None],points[i+1][None],scale) for i in range(n-1)]
    cost=unary[0][:,None]+unary[1][None]+transitions[0];pointers=[]
    for i in range(2,n):
        acc=points[i][None,None]-2*points[i-1][None,:,None]+points[i-2][:,None,None]
        penalty=.6*_transition(np.zeros_like(acc),acc,scale)
        scores=cost[:,:,None]+penalty
        ptr=scores.argmin(0);cost=scores.min(0)+transitions[i-1]+unary[i][None]
        pointers.append(ptr)
    j,k=np.unravel_index(cost.argmin(),cost.shape);selected=[int(k),int(j)];best=float(cost[j,k])
    for ptr in pointers[::-1]:selected.append(int(ptr[selected[-1],selected[-2]]))
    return selected[::-1],best

def recover(d):
    """Requires clear poses on BOTH sides; never confirms inferred endpoints."""
    masks=d['masks'];valid=d['mask_valid'];reference=d['reference_length'];n=len(masks)
    if not(np.isfinite(d['first']).all() and np.isfinite(d['last']).all()):
        return None,dict(status='missing_two_sided_anchors')
    if not np.asarray(valid).all():return None,dict(status='invalid_mask_in_event')
    states=[candidate_states(mask,reference,bool(v)) for mask,v in zip(masks,valid)]
    for i,p in [(0,d['first']),(-1,d['last'])]:
        q=feasible_quality(p,masks[i],reference)
        if not q['geometric_pass']:return None,dict(status='invalid_anchor_geometry')
        # Last A/B ordering is solved from the trajectory, not assumed to be
        # the order returned independently by the skeleton extractor.
        states[i]=[dict(points=p.copy(),name='anchor',endpoint_observed=[True,True],quality=q,unary=0.)]
        if i==-1:states[i].append(dict(states[i][0],points=p[::-1].copy()))
    if any(not s for s in states):return None,dict(status='no_feasible_candidate',missing=[int(d['frame_ids'][i]) for i,s in enumerate(states) if not s])
    chosen,cost=select_event(states,reference);selected=[s[k] for s,k in zip(states,chosen)]
    curves=np.array([s['points'] for s in selected])
    qa=[dict(frame=int(d['frame_ids'][i]),candidate=s['name'],endpoint_observed=s['endpoint_observed'],
        status='TEMPORAL_RECONSTRUCTION_REVIEW',origin='whole_event_temporal',candidate_count=len(states[i]),**s['quality']) for i,s in enumerate(selected)]
    # Release each anchor separately while retaining all image frames. This
    # checks anchor sensitivity; it is NOT independent one-sided inference.
    forward_states=[list(s) for s in states];forward_states[-1]=candidate_states(masks[-1],reference)
    backward_states=[list(s) for s in states];backward_states[0]=candidate_states(masks[0],reference)
    left,_=select_event(forward_states,reference);right,_=select_event(backward_states,reference)
    for i,q in enumerate(qa):
        a=forward_states[i][left[i]]['points'];b=backward_states[i][right[i]]['points']
        q['anchor_release_shape_agreement_px']=min(float(np.linalg.norm(a-b,axis=1).mean()),float(np.linalg.norm(a[::-1]-b,axis=1).mean()))
    return curves,dict(status='review_candidates_recovered',cost=cost,quality=qa,
        anchors=[int(d['frame_ids'][0]),int(d['frame_ids'][-1])],inferred_endpoint_frames=[q['frame'] for q in qa if not all(q['endpoint_observed'])])

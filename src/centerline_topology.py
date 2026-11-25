"""Geometry checks and explicit self-contact branch candidates.

Crossings in the 2D projection are allowed. Candidate confidence is an
operational quality check, not a biological ground-truth probability.
"""
import cv2
import numpy as np
from scipy import ndimage as ndi
from typing import NamedTuple
from skeleton_geometry import resample as sample_arc, skeleton_path
from scipy.signal import savgol_filter
from skimage.morphology import skeletonize


def resample(p, count=101):
    return sample_arc(p, count, tolerance=1e-7, error='Degenerate path')


def geometry_quality(p, device='cpu'):
    p=resample(np.asarray(p,float))
    v=np.diff(p,axis=0);theta=np.arctan2(v[:,1],v[:,0])
    turn=(np.diff(theta)+np.pi)%(2*np.pi)-np.pi
    sums=np.convolve(turn,np.ones(5),mode='valid')
    k=int(np.argmax(np.abs(sums)))
    peak=float(np.degrees(abs(sums[k])))
    # Check non-neighbouring segments together, preserving row-major pair order.
    xp=np
    if device=='cuda':
        import torch
        xp=torch
        p=torch.as_tensor(p,device=device)
        v=torch.as_tensor(v,device=device)
    a,b=v[:,None],v[None,:]
    c=p[None,:-1]-p[:-1,None]
    cross=a[...,0]*b[...,1]-a[...,1]*b[...,0]
    valid=abs(cross)>1e-7
    den=xp.where(valid,cross,1.)
    t=(c[...,0]*b[...,1]-c[...,1]*b[...,0])/den
    u=(c[...,0]*a[...,1]-c[...,1]*a[...,0])/den
    hit=xp.triu(valid&(t>0)&(t<1)&(u>0)&(u<1),3)
    intersections=xp.argwhere(hit).tolist()
    return dict(max_turn_5pct_deg=peak,max_turn_start_fraction=k/100,
                local_hairpin=peak>150,projection_crossings=len(intersections),
                crossing_segments=intersections)


def longest_chain(sk):
    path, coverage, _ = skeleton_path(sk, no_ends='No open chain', disconnected='Disconnected')
    return path, coverage


def smooth_chain(p):
    if len(p)>=11:
        smooth=savgol_filter(p,11,2,axis=0);smooth[0]=p[0];smooth[-1]=p[-1];return smooth
    return p


def extend_tip(p,mask):
    p=p.copy();v=p[0]-p[min(12,len(p)-1)];v/=max(np.linalg.norm(v),1e-8)
    tip=p[0].copy()
    for distance in np.arange(.5,40,.5):
        q=p[0]+distance*v;x,y=np.rint(q).astype(int)
        if not(0<=y<mask.shape[0] and 0<=x<mask.shape[1] and mask[y,x]):break
        tip=q
    return np.vstack([tip,p])


def connector(a,b,radius):
    start=a[-1];end=b[0];distance=np.linalg.norm(end-start)
    back=min(len(a)-1,max(4,int(radius)))
    fore=min(len(b)-1,max(4,int(radius)))
    u=start-a[-1-back];v=b[fore]-end
    u/=max(np.linalg.norm(u),1e-8);v/=max(np.linalg.norm(v),1e-8)
    # A cubic bridge preserves incoming/outgoing tangents across the merged zone.
    ctrl=np.array([start,start+u*distance/3,end-v*distance/3,end])
    t=np.linspace(0,1,max(8,int(distance*2)))[:,None]
    bridge=(1-t)**3*ctrl[0]+3*(1-t)**2*t*ctrl[1]+3*(1-t)*t**2*ctrl[2]+t**3*ctrl[3]
    straight=(end-start)/max(distance,1e-8)
    penalty=(1-np.dot(u,straight))+(1-np.dot(v,straight))
    return bridge,float(penalty)


def prune_short_spurs(sk,radius):
    sk=sk.copy()
    for _ in range(4):
        degree=ndi.convolve(sk.astype(int),np.ones((3,3)),mode='constant')-sk
        remove=[]
        for start in np.argwhere(sk&(degree==1)):
            point=tuple(start);route=[point];previous=None;length=0
            while length<1.6*radius:
                y,x=point
                neighbors=[(y+dy,x+dx) for dy in (-1,0,1) for dx in (-1,0,1)
                           if (dy or dx) and 0<=y+dy<sk.shape[0] and 0<=x+dx<sk.shape[1]
                           and sk[y+dy,x+dx] and (y+dy,x+dx)!=previous]
                if len(neighbors)!=1:
                    if len(neighbors)>1:remove.extend(route[:-1])
                    break
                nxt=neighbors[0];length+=np.linalg.norm(np.array(nxt)-point)
                previous,point=point,nxt;route.append(point)
        if not remove:break
        for y,x in remove:sk[y,x]=False
        sk=skeletonize(sk)
    return sk


class MaskTopology(NamedTuple):
    local: np.ndarray
    skeleton: np.ndarray
    degree: np.ndarray
    radius: float
    origin: np.ndarray


def prepare_topology(mask, prune):
    ys,xs=np.nonzero(mask)
    if len(xs)<30:return None
    x0=max(0,xs.min()-4);y0=max(0,ys.min()-4)
    local=mask[y0:ys.max()+5,x0:xs.max()+5]
    sk=skeletonize(local);dt=ndi.distance_transform_edt(local)
    if prune:sk=prune_short_spurs(sk,float(np.median(dt[sk])))
    degree=ndi.convolve(sk.astype(int),np.ones((3,3)),mode='constant')-sk
    return MaskTopology(local, sk, degree, float(np.median(dt[sk])), np.array([x0, y0]))


def topology_candidates(mask, prune=False, contact_radius_fraction=1.25):
    topology = prepare_topology(mask, prune)
    if topology is None:
        return [], np.zeros_like(mask), dict(kind='empty')
    candidates, zone, info = junction_candidates(topology, contact_radius_fraction)
    fullzone = np.zeros_like(mask)
    x, y = topology.origin
    fullzone[y:y+zone.shape[0], x:x+zone.shape[1]] = zone > 0
    return candidates, fullzone, info


def junction_candidates(topology, contact_radius_fraction):
    local, sk, degree, radius, offset = topology
    endpoints = np.argwhere(sk & (degree == 1))
    branches = sk & (degree > 2)
    info=dict(kind='simple',skeleton_endpoints=len(endpoints),radius_px=radius)
    zone=np.zeros_like(local,np.uint8);candidates=[]
    if not branches.any() and len(endpoints)==2:
        p,coverage=longest_chain(sk)
        p=extend_tip(extend_tip(smooth_chain(p),local)[::-1],local)[::-1]
        candidates.append(dict(points=resample(p)+offset,name='native_medial',connection_cost=0.0))
    else:
        info['kind']='unresolved_contact'
        labels,n=ndi.label(branches,np.ones((3,3)))
        centers=np.array([np.argwhere(labels==i).mean(axis=0)[::-1] for i in range(1,n+1)])
        # Only resolve one compact contact region with two observable body ends.
        # Hidden tips and multiple separate contacts remain uncertain.
        if len(endpoints)==2 and 1<=len(centers)<=4 and np.ptp(centers,axis=0).max()<max(8*radius,30):
            r=max(6,int(round(contact_radius_fraction*radius)))
            for c in centers:cv2.circle(zone,tuple(np.rint(c).astype(int)),r,1,-1)
            for c in centers[1:]:cv2.line(zone,tuple(np.rint(centers[0]).astype(int)),tuple(np.rint(c).astype(int)),1,2*r)
            rest=sk&~zone.astype(bool);lab,num=ndi.label(rest,np.ones((3,3)))
            dz=ndi.distance_transform_edt(~zone.astype(bool));tails=[];loops=[]
            for i in range(1,num+1):
                part=lab==i
                if part.sum()<max(10,radius):continue
                try:p,coverage=longest_chain(part)
                except ValueError:continue
                if coverage<.9:continue
                p=smooth_chain(p);idx=np.rint(p[[0,-1]]).astype(int)
                contact=dz[idx[:,1],idx[:,0]]<3
                if contact.all():loops.append(p)
                elif contact.sum()==1:
                    tails.append(p if contact[-1] else p[::-1])
            if len(tails)==2 and len(loops)==1:
                a=extend_tip(tails[0],local);b=extend_tip(tails[1],local)[::-1]
                for reverse in (False,True):
                    middle=loops[0][::-1] if reverse else loops[0]
                    bridge1,cost1=connector(a,middle,radius);bridge2,cost2=connector(middle,b,radius)
                    p=np.vstack([a,bridge1[1:-1],middle,bridge2[1:-1],b])
                    candidates.append(dict(points=resample(p)+offset,name=f'junction_pairing_{int(reverse)}',connection_cost=cost1+cost2))
                info['kind']='paired_contact'
    return candidates, zone, info


def hidden_tip_candidates(mask,length_ref):
    """Single visible tip plus a loop: follow both loop orders to a length prior.

    These are temporal hypotheses, never sufficient on their own to assert the
    location of an occluded endpoint. Keep the unobserved endpoint provenance.
    """
    topology = prepare_topology(mask, prune=True)
    return hidden_loop_candidates(topology, length_ref) if topology is not None else []


def hidden_loop_candidates(topology, length_ref):
    local, sk, degree, radius, (x0, y0) = topology
    branches=sk&(degree>2);ends=sk&(degree==1)
    if ends.sum()!=1 or not branches.any():return []
    labels,n=ndi.label(branches,np.ones((3,3)))
    centers=np.array([np.argwhere(labels==i).mean(0)[::-1] for i in range(1,n+1)])
    if len(centers)>3 or np.ptp(centers,axis=0).max()>8*radius:return []
    zone=np.zeros_like(local,np.uint8);r=max(5,round(radius*1.1))
    for c in centers:cv2.circle(zone,tuple(np.rint(c).astype(int)),r,1,-1)
    for c in centers[1:]:cv2.line(zone,tuple(np.rint(centers[0]).astype(int)),tuple(np.rint(c).astype(int)),1,2*r)
    labels,n=ndi.label(sk&~zone.astype(bool),np.ones((3,3)));dz=ndi.distance_transform_edt(~zone.astype(bool))
    tails=[];loops=[]
    for i in range(1,n+1):
        part=labels==i
        if part.sum()<max(10,radius):continue
        try:p,coverage=longest_chain(part)
        except ValueError:continue
        if coverage<.9:continue
        p=smooth_chain(p);idx=np.rint(p[[0,-1]]).astype(int);contact=dz[idx[:,1],idx[:,0]]<3
        if contact.all():loops.append(p)
        elif contact.sum()==1:tails.append(p if contact[-1] else p[::-1])
    if len(tails)!=1 or len(loops)!=1:return []
    a=extend_tip(tails[0],local);out=[]
    for reverse in (False,True):
        middle=loops[0][::-1] if reverse else loops[0]
        bridge,cost=connector(a,middle,radius)
        p=np.vstack([a,bridge[1:-1],middle])
        # Complete the hidden arc instead of extrapolating a tangent out of the
        # worm. The endpoint may advance over the already visible body arc.
        closing,_=connector(middle,middle,radius)
        extended=np.vstack([p,closing[1:-1],middle]);s=np.r_[0,np.cumsum(np.linalg.norm(np.diff(extended,axis=0),axis=1))]
        for fraction in (.94,1.,1.06):
            length=min(length_ref*fraction,s[-1]);query=np.linspace(0,length,101)
            q=np.column_stack([np.interp(query,s,extended[:,k]) for k in (0,1)])+[x0,y0]
            out.append(dict(points=q,name=f'hidden_loop_{int(reverse)}_{fraction}',connection_cost=cost,
                            endpoint_observed=[True,False]))
    return out

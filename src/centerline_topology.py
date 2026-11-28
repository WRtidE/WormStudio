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
    if device in ('cuda', 'mps'):
        import torch
        xp=torch
        dtype = torch.float32 if device == 'mps' else torch.float64
        p=torch.as_tensor(p,device=device,dtype=dtype)
        v=torch.as_tensor(v,device=device,dtype=dtype)
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


def _tangent(pts, at_start=True):
    if len(pts) < 2:
        return np.array([1.0, 0.0])
    step = min(len(pts) - 1, 5)
    v = (pts[step] - pts[0]) if at_start else (pts[-1] - pts[-1 - step])
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 1e-8 else np.array([1.0, 0.0])


def skeleton_graph_candidates(topology, length_ref, max_candidates=8):
    """Enumerate valid centerline paths across multi-branch/spur/multi-loop skeletons."""
    local, sk, degree, radius, offset = topology
    if not np.isfinite(length_ref) or length_ref <= 0 or not sk.any():
        return []
    comp_labels, n_comp = ndi.label(sk, np.ones((3, 3), dtype=int))
    if n_comp > 1:
        sizes = np.bincount(comp_labels.ravel())
        sizes[0] = 0
        main_id = int(sizes.argmax())
        if sizes[main_id] < 0.70 * sk.sum():
            return []
        sk = comp_labels == main_id
        degree = ndi.convolve(sk.astype(int), np.ones((3, 3), dtype=int), mode='constant') - sk

    br_labels, n_br = ndi.label(sk & (degree > 2), np.ones((3, 3), dtype=int))
    end_yx = np.argwhere(sk & (degree == 1))
    n_end = len(end_yx)
    if n_end == 0 or (n_br == 0 and n_end == 2):
        return []

    node_id = np.full(sk.shape, -1, dtype=int)
    for b in range(1, n_br + 1):
        node_id[br_labels == b] = b - 1
    for idx, (ey, ex) in enumerate(end_yx):
        node_id[ey, ex] = n_br + idx

    h, w = sk.shape
    visited_chain = np.zeros(sk.shape, dtype=bool)
    raw_edges = []
    seen_direct = set()

    def neighbors8(y, x):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if not (dy or dx):
                    continue
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and sk[ny, nx]:
                    yield ny, nx

    for y, x in np.argwhere(node_id >= 0):
        u = int(node_id[y, x])
        for ny, nx in neighbors8(y, x):
            nv = int(node_id[ny, nx])
            if nv >= 0:
                if nv != u:
                    pair = (min(u, nv), max(u, nv))
                    if pair not in seen_direct:
                        seen_direct.add(pair)
                        pts = np.array([[x, y], [nx, ny]], dtype=float)
                        raw_edges.append((u, nv, pts))
                continue
            if visited_chain[ny, nx]:
                continue
            chain = [(x, y), (nx, ny)]
            visited_chain[ny, nx] = True
            py, px = y, x
            cy, cx = ny, nx
            end_node = None
            while True:
                nexts = [(ay, ax) for ay, ax in neighbors8(cy, cx)
                         if (ay, ax) != (py, px) and (node_id[ay, ax] >= 0 or not visited_chain[ay, ax])]
                # Prefer continuing along degree-2 chain before jumping back to node u.
                next_nodes = [(ay, ax) for ay, ax in nexts if node_id[ay, ax] >= 0]
                next_chain = [(ay, ax) for ay, ax in nexts if node_id[ay, ax] < 0]
                if next_chain:
                    ay, ax = next_chain[0]
                    visited_chain[ay, ax] = True
                    chain.append((ax, ay))
                    py, px = cy, cx
                    cy, cx = ay, ax
                elif next_nodes:
                    # Avoid immediate 1-step bounce back to the exact starting pixel if another node exists.
                    chosen = next((p for p in next_nodes if p != (y, x)), next_nodes[0])
                    ay, ax = chosen
                    end_node = int(node_id[ay, ax])
                    chain.append((ax, ay))
                    break
                else:
                    break
            if end_node is not None:
                pts = np.asarray(chain, dtype=float)
                if u != end_node or len(pts) >= max(8, int(round(radius))):
                    raw_edges.append((u, end_node, pts))

    if not raw_edges:
        return []

    adj = {i: [] for i in range(n_br + n_end)}
    edge_lengths = []
    for e_idx, (u, v, pts) in enumerate(raw_edges):
        arc_len = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
        edge_lengths.append(arc_len)
        d_out = _tangent(pts, at_start=True)
        d_in = _tangent(pts, at_start=False)
        adj[u].append((e_idx, v, pts, arc_len, d_out, d_in))
        adj[v].append((e_idx, u, pts[::-1].copy(), arc_len, -d_in, -d_out))

    major_len = sum(l for l in edge_lengths if l >= 1.5 * radius) or sum(edge_lengths)
    min_len = 0.72 * length_ref - 2.0 * radius
    max_len = 1.18 * length_ref + 2.0 * radius
    bridge_limit = 4.5 * radius

    proposals = []

    def search(u, prev_e, prev_dir_in, cum_len, turn_pen, used, seq_ids, seq_pts):
        if len(proposals) >= 80 or len(seq_ids) > 18 or cum_len > max_len:
            return
        if seq_ids and u >= n_br:
            if cum_len >= min_len:
                covered = sum(edge_lengths[e] for e in set(seq_ids) if edge_lengths[e] >= 1.5 * radius)
                cov = covered / max(major_len, 1.0)
                if cov >= 0.65:
                    score = turn_pen + 3.0 * (1.0 - min(1.0, cov)) + 1.5 * abs(cum_len / length_ref - 1.0)
                    proposals.append((score, turn_pen, [p for p in seq_pts]))
            return
        for e_idx, v, pts, e_len, d_out, d_in in adj[u]:
            count = used.get(e_idx, 0)
            is_bridge = (u < n_br and v < n_br and u != v and e_len <= bridge_limit)
            if count >= (2 if is_bridge and e_idx != prev_e else 1):
                continue
            step_pen = 0.0
            if prev_dir_in is not None:
                dot = float(np.dot(prev_dir_in, d_out))
                if dot < -0.55:
                    continue
                step_pen = 1.0 - dot
            used[e_idx] = count + 1
            seq_ids.append(e_idx)
            seq_pts.append(pts)
            search(v, e_idx, d_in, cum_len + e_len, turn_pen + step_pen, used, seq_ids, seq_pts)
            seq_ids.pop()
            seq_pts.pop()
            if count == 0:
                del used[e_idx]
            else:
                used[e_idx] = count

    for start in range(n_br, n_br + n_end):
        search(start, -1, None, 0.0, 0.0, {}, [], [])

    proposals.sort(key=lambda item: item[0])
    out = []
    for _, turn_pen, parts in proposals:
        joined = np.vstack([parts[0], *[p[1:] for p in parts[1:]]])
        try:
            joined = smooth_chain(joined)
            joined = extend_tip(extend_tip(joined, local)[::-1], local)[::-1]
            line = resample(joined) + offset
        except ValueError:
            continue
        if any(min(np.linalg.norm(line - v['points'], axis=1).mean(),
                   np.linalg.norm(line[::-1] - v['points'], axis=1).mean()) < 2.5 for v in out):
            continue
        out.append(dict(points=line, name='skeleton_graph_path', connection_cost=float(turn_pen),
                        endpoint_observed=[True, True]))
        if len(out) >= max_candidates:
            break
    return out

"""Choose a whole sequence of body paths and end orders using image motion."""
import numpy as np
from scipy.ndimage import map_coordinates
from scipy.ndimage import binary_fill_holes,label

from head_recovery import tracking_indices, pose_parity
from motion import lk_step


def huber_squared(distance):
    return np.where(distance<=2,distance**2,4*distance-4)


def flow_matches(state, target, direction, indices, length):
    """Require image agreement on both terminal quarters, not just the midbody."""
    use = state['good_f' if direction == 'forward' else 'good_b']
    predicted = state[direction]
    error = np.linalg.norm(predicted - target, axis=1)
    opposite = np.linalg.norm(predicted - target[::-1], axis=1)
    matches = use & (error <= .12*length) & (error + .15*length < opposite)
    contradicts = use & (opposite <= .12*length) & (opposite + .15*length < error)
    return (not contradicts.any() and matches.sum() >= 3
            and matches[indices <= 25].any() and matches[indices >= 75].any())

def persistent_loop_centers(a,b,radius):
    """Find enclosed background shared by two silhouettes around the same pillar."""
    holes_a=binary_fill_holes(a)&~a
    holes_b=binary_fill_holes(b)&~b
    shared,count=label(holes_a&holes_b)
    if not count:
        return []
    sizes=np.bincount(shared.ravel())
    centers=[]
    for index in np.flatnonzero(sizes>=max(50,radius**2)):
        if index==0:
            continue
        y,x=np.nonzero(shared==index)
        centers.append(np.array([x.mean(),y.mean()]))
    return centers


def loop_transition(left,right,center):
    """A body cannot gain a full turn around an enclosed pillar in one frame."""
    a=np.array([s['points'] for s in left])-center
    b=np.array([s['points'] for s in right])-center
    angles_a=np.unwrap(np.arctan2(a[:,:,1],a[:,:,0]),axis=1)
    angles_b=np.unwrap(np.arctan2(b[:,:,1],b[:,:,0]),axis=1)
    turn_a=angles_a[:,-1]-angles_a[:,0]
    turn_b=angles_b[:,-1]-angles_b[:,0]
    delta=angles_b[None,:,[0,-1]]-angles_a[:,None,[0,-1]]
    delta=np.arctan2(np.sin(delta),np.cos(delta))
    residual=turn_b[None,:]-turn_a[:,None]-(delta[:,:,1]-delta[:,:,0])
    looped=(np.abs(turn_a[:,None])>np.pi)&(np.abs(turn_b[None,:])>np.pi)
    return looped&(np.abs(residual)>np.pi),turn_a,turn_b


def select_body_sequence(frames,gray,masks,radius,point_count=13,pillar_centers=(),anchor=None,anchors=()):
    """Second-order path search. Both end orders remain states until backtracking.

    The first candidate's order supplies the initial marked side. Later clear
    frames constrain geometry and measured motion without assigning a new head.
    """
    indices=tracking_indices(point_count)
    if len(frames)<2 or len(frames)!=len(gray) or any(not c for c in frames):
        raise ValueError('Body sequence requires an observed candidate in every frame')
    states=[]
    for i,candidates in enumerate(frames):
        state=[]
        for k,c in enumerate(candidates):
            p=c['points'][indices]
            forward=backward=good_f=good_b=None
            if i+1<len(frames):
                forward,good_f,_=lk_step(gray[i],gray[i+1],p)
                good_f &= map_coordinates(masks[i+1].astype('uint8'),forward.T[::-1],order=0,mode='constant')>0
            if i>0:
                backward,good_b,_=lk_step(gray[i],gray[i-1],p)
                good_b &= map_coordinates(masks[i-1].astype('uint8'),backward.T[::-1],order=0,mode='constant')>0
            for reverse in (False,True):
                order=slice(None,None,-1) if reverse else slice(None)
                state.append(dict(candidate=k,reverse=reverse,points=c['points'][order].copy(),
                    sampled=p[order],unary=.4*c['unary'],
                    forward=None if forward is None else forward[order],
                    backward=None if backward is None else backward[order],
                    good_f=None if good_f is None else good_f[order],
                    good_b=None if good_b is None else good_b[order]))
        states.append(state)
    geometry=[np.array([s['sampled'] for s in group]) for group in states]
    transitions=[];loop_checks=[]
    for i,(left,right) in enumerate(zip(states,states[1:])):
        a,b=geometry[i],geometry[i+1]
        displacement=np.linalg.norm(a[:,None]-b[None,:],axis=-1)/radius
        cost=.05*huber_squared(displacement).mean(-1)+.15*huber_squared(displacement[:,:,[0,-1]]).mean(-1)
        # Flow supplies observed movement of each body section in BOTH images.
        # Invalid flow contributes no vote, instead of becoming zero movement.
        for j,s in enumerate(left):
            use=s['good_f']
            if use.sum()>=4:
                error=np.linalg.norm(b[:,use]-s['forward'][use],axis=-1)/radius
                cost[j]+=1.2*huber_squared(error).mean(-1)
        for k,s in enumerate(right):
            use=s['good_b']
            if use.sum()>=4:
                error=np.linalg.norm(a[:,use]-s['backward'][use],axis=-1)/radius
                cost[:,k]+=1.2*huber_squared(error).mean(-1)
        # A finite cost is not proof of correspondence. A weak image may use
        # unambiguous geometry; a large motion needs support in both images.
        for j, earlier in enumerate(left):
            for k, later in enumerate(right):
                if pose_parity(earlier['points'], later['points'], indices) == 0:
                    continue
                length = min(np.linalg.norm(np.diff(s['points'], axis=0), axis=1).sum()
                             for s in (earlier, later))
                if not (flow_matches(earlier, later['sampled'], 'forward', indices, length)
                        and flow_matches(later, earlier['sampled'], 'backward', indices, length)):
                    cost[j,k] = np.inf
        checks=[]
        centers=list(pillar_centers) or persistent_loop_centers(masks[i],masks[i+1],radius)
        for center in centers:
            blocked,turn_a,turn_b=loop_transition(left,right,center)
            cost[blocked]=np.inf
            checks.append(dict(center=center,blocked=int(blocked.sum()),left_turn=turn_a,right_turn=turn_b))
        loop_checks.append(checks)
        transitions.append(cost)
    unary=[np.array([s['unary'] for s in group]) for group in states]
    marks=list(anchors)
    if anchor is not None:marks.insert(0,anchor)
    if not marks:
        unary[0][1::2]=np.inf
    for frame,point in marks:
        for j,state in enumerate(states[frame]):
            points=state['points']
            distance=np.linalg.norm(points[[0,-1]]-np.asarray(point),axis=1)
            length=np.linalg.norm(np.diff(points,axis=0),axis=1).sum()
            if distance[0]>.2*length or distance[1]-distance[0]<.05*length:
                unary[frame][j]=np.inf
        if not np.isfinite(unary[frame]).any():
            raise ValueError('Initial head mark does not match a body endpoint')
    cost=unary[0][:,None]+unary[1][None,:]+transitions[0]
    if not np.isfinite(cost).any():
        raise BodySequenceBreak(1)
    pointers=[]
    for i in range(2,len(states)):
        a,b,c=geometry[i-2],geometry[i-1],geometry[i]
        acceleration=np.linalg.norm(c[None,None]-2*b[None,:,None]+a[:,None,None],axis=-1)/radius
        penalty=.08*huber_squared(acceleration).mean(-1)+.3*huber_squared(acceleration[:,:,:,[0,-1]]).mean(-1)
        combined=cost[:,:,None]+penalty
        pointers.append(combined.argmin(0))
        cost=combined.min(0)+transitions[i-1]+unary[i][None,:]
        if not np.isfinite(cost).any():
            raise BodySequenceBreak(i)
    final_costs=[float(cost[:,np.arange(cost.shape[1])%2==reverse].min()) for reverse in (0,1)]
    a,b=np.unravel_index(cost.argmin(),cost.shape)
    if not np.isfinite(cost[a,b]):
        raise BodySequenceBreak(len(states)-1)
    chosen=[int(b),int(a)]
    for ptr in pointers[::-1]:chosen.append(int(ptr[chosen[-1],chosen[-2]]))
    chosen=chosen[::-1]
    selected=[states[i][j] for i,j in enumerate(chosen)]
    links=[]
    for i,(a,b) in enumerate(zip(selected,selected[1:]),1):
        loops=[dict(center_crop_px=q['center'].tolist(),rejected_pairs=q['blocked'],
                    before_turn_pi=float(q['left_turn'][chosen[i-1]]/np.pi),
                    after_turn_pi=float(q['right_turn'][chosen[i]]/np.pi)) for q in loop_checks[i-1]]
        links.append(dict(index=i,forward_points=int(a['good_f'].sum()),
            backward_points=int(b['good_b'].sum()),
            head_step_px=float(np.linalg.norm(b['points'][0]-a['points'][0])),
            tail_step_px=float(np.linalg.norm(b['points'][-1]-a['points'][-1])),loops=loops))
    return selected,dict(total_cost=float(cost.min()),final_order_costs=final_costs,links=links)


class BodySequenceBreak(ValueError):
    def __init__(self,index):
        self.index=index
        super().__init__(f'No continuous body correspondence at sequence index {index}')

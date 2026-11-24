"""Candidate endpoint order through isolated, measured poses at a fast turn."""
from itertools import groupby, product

import numpy as np
from head_recovery import appearance_vote


def bracketed_order(lines, frames, source_flip, length):
    """Compare every order of one or two isolated frames and the following track.

    The last two observations belong to one continuous track and share an order.
    Compare the actual terminal tenths in both directions around each middle frame.
    """
    curves = lines[frames]
    terminals = np.stack((curves[:,:11], curves[:,-1:-12:-1]), axis=1)
    scores = []
    for sides in product((0,1), repeat=len(frames)-2):
        order = (source_flip,*sides,sides[-1])
        track = np.stack([ends[::-1] if flip else ends for ends,flip in zip(terminals,order)])
        predicted = (track[:-2]+track[2:])*.5
        residual = np.linalg.norm(track[1:-1]-predicted,axis=-1).mean(axis=-1)/length
        steps = np.linalg.norm(np.diff(track,axis=0),axis=-1).mean(axis=-1)/length
        scores.append((float(residual.mean()),sides,float(residual.max()),float(steps.max())))
    scores.sort()
    cost,sides,worst,step = scores[0]
    margin = scores[1][0]-cost
    if margin < .05 or worst > .18 or step > .45:
        return None
    return sides,dict(trajectory_frames=frames,max_prediction_error_bl=worst,
                      order_margin_bl=margin,max_step_bl=step)


def flanked_order(lines, facts, middle):
    """Compare both observed terminal tenths with their positions between oriented flanks."""
    before,after=middle[0]-1,middle[-1]+1
    curves=lines[[before,*middle,after]]
    ends=np.stack((curves[:,:11],curves[:,-1:-12:-1]),axis=1)
    weight=(np.arange(1,len(middle)+1)/(after-before))[:,None,None,None]
    expected=(1-weight)*ends[0]+weight*ends[-1]
    observed=ends[1:-1]
    length=min(facts[i]['length_px'] for i in [before,*middle,after])
    errors=np.stack([np.linalg.norm(expected-tips,axis=-1).mean(axis=-1)/length
                     for tips in (observed,observed[:,::-1])],axis=1)
    paired_cost=errors.mean(axis=2)
    sides=paired_cost.argmin(axis=1)
    index=np.arange(len(middle))
    best,other=errors[index,sides],errors[index,1-sides]
    if np.any(best>=other) or np.max(best)>.18 or np.any(np.mean(other-best,axis=1)<.05):
        return None
    return sides,errors


REFERENCE_SOURCES = ('initial_mark','auto_taper_seed','recovered_marked_end','multi_frame_appearance_candidate')


def visible_end_order(a, b, observed_a, observed_b, length):
    """Match only observed terminal tenths. Return relative order or abstain."""
    if not all(observed_b):
        if not all(observed_a):
            return None
        a,b,observed_a = b,a,observed_b
    if not any(observed_a):
        return None
    ends_a=np.stack((a[:11],a[-1:-12:-1]))[list(observed_a)]
    ends_b=np.stack((b[:11],b[-1:-12:-1]))
    orders=np.stack((ends_b,ends_b[::-1]))[:,list(observed_a)]
    travel=np.linalg.norm(ends_a-orders,axis=-1).mean(axis=-1)/length
    winner=int(travel.mean(axis=1).argmin())
    advantage=travel[1-winner]-travel[winner]
    if len(ends_a)==2 and np.all(advantage>0) and advantage.mean()>=.09 and travel[winner].max()<=.45:
        return winner
    # Close tips can exchange nearest neighbours while their adjacent shafts
    # still point in distinct directions. Every visible shaft must agree.
    tangent=ends_a[:,-3:].mean(axis=1)-ends_a[:,:3].mean(axis=1)
    targets=orders[:,:,-3:].mean(axis=2)-orders[:,:,:3].mean(axis=2)
    norms=np.linalg.norm(tangent,axis=-1)*np.linalg.norm(targets,axis=-1)
    if np.any(norms<=0):
        return None
    cosine=(tangent*targets).sum(axis=-1)/norms
    winner=int(cosine.mean(axis=1).argmax())
    if (cosine[winner].min()>=.87 and np.min(cosine[winner]-cosine[1-winner])>=.45
            and travel[winner].max()<=.18 and np.max(travel[winner]-travel[1-winner])<=.075):
        return winner
    return None


def bridge_visible_turn(lines, facts, edges, left, right):
    """Check an isolated ambiguous link against the observed motion on each side."""
    missing=[i for i in range(left,right) if edges[i] is None]
    if len(missing)!=1 or missing[0] in (left,right-1):
        return None
    i=missing[0]
    frames=list(range(i-1,i+3))
    if not all(facts[j]['candidate']=='clear_observation' and facts[j]['head_observed']
               and facts[j]['tail_observed'] for j in frames):
        return None
    # Align each outer observation to its already matched neighbour.
    curves=lines[frames]
    if edges[i-1]:
        curves[0]=curves[0,::-1]
    if edges[i+1]:
        curves[3]=curves[3,::-1]
    length=min(facts[j]['length_px'] for j in frames)
    forward=bracketed_order(curves,[0,1,2,3],0,length)
    backward=bracketed_order(curves,[3,2,1,0],0,length)
    if forward is None or backward is None or forward[0][0]!=0 or forward[0]!=backward[0]:
        return None
    order=forward[0][1]
    edges[i]=order
    return dict(frames=frames,edge=[i,i+1],order=order,
                forward_margin_bl=forward[1]['order_margin_bl'],backward_margin_bl=backward[1]['order_margin_bl'])


def recover_visible_run(lines, facts, middle, first, last, fps, feature):
    """Track from each original flank independently and verify each component against both images."""
    left,right=middle[0]-1,middle[-1]+1
    flanks=(facts[left],facts[right])
    if not all(f and f['head_known'] and f['head_source'] in REFERENCE_SOURCES and not f['anchor_conflict'] for f in flanks):
        return []
    if not all(facts[i]['head_observed'] and facts[i]['tail_observed'] and not facts[i]['anchor_conflict'] for i in middle):
        return []
    # The last visible tip and the last clear appearance need not come from the same frame.
    span=int(np.ceil(fps))
    pools=(range(max(first,flanks[0]['component'][0],left-span+1),left+1),
           range(right,min(last,flanks[1]['component'][1],right+span-1)+1))
    refs=[[i for i in pool if facts[i]['head_known'] and facts[i]['head_source'] in REFERENCE_SOURCES
           and facts[i]['candidate']=='clear_observation' and facts[i]['head_observed']
           and facts[i]['tail_observed'] and not facts[i]['anchor_conflict']] for pool in pools]
    refs=[refs[0][-5:],refs[1][:5]]
    if min(map(len,refs))<3:
        return []
    references=[[feature(i) for i in group] for group in refs]
    templates=[np.median(group,axis=0) for group in references]
    if appearance_vote(*templates)<.45:
        return []
    edges={}
    for i in range(left,right):
        a,b=facts[i:i+2]
        if a['component']==b['component']:
            edges[i]=0
        else:
            seen_a=(a['head_observed'],a['tail_observed'])
            seen_b=(b['head_observed'],b['tail_observed'])
            edges[i]=visible_end_order(lines[i],lines[i+1],seen_a,seen_b,min(a['length_px'],b['length_px']))
    bridge=bridge_visible_turn(lines,facts,edges,left,right)
    orders=[]
    for root,step in ((left,1),(right,-1)):
        track={root:0}
        for i in range(root,right if step==1 else left,step):
            edge=edges[min(i,i+step)]
            if edge is None:
                break
            track[i+step]=track[i]^edge
        orders.append(track)

    audit=[]
    for _,frames in groupby(middle,lambda i:facts[i]['component']):
        frames=list(frames)
        lo,hi=frames[0],frames[-1]
        votes=[track[lo] for track in orders if lo in track]
        entry=dict(component=[lo,hi],boundary=[left,right],reference_frames=refs,candidate_only=True,
                   accepted=False,motion_orders=[track.get(lo) for track in orders])
        if bridge is not None:
            entry['trajectory_bridge']=bridge
        audit.append(entry)
        if len(set(votes))!=1:
            entry['reason']='unresolved_visible_end_motion'
            continue
        flip=votes[0]
        # A touching shape can carry the visible tip to the next frame, but
        # only a clear measured shape can receive a new candidate identity.
        if not all(facts[i]['candidate']=='clear_observation' for i in frames):
            entry['reason']='contact_geometry'
            continue
        samples=[feature(i) for i in frames]
        scores=np.array([[appearance_vote(template,sample) for template in templates] for sample in samples])*(1-2*flip)
        entry['appearance_votes']=scores.tolist()
        support=scores>=.45
        corroborated=support.any(axis=0).all()
        if (not corroborated and bridge is not None and len(votes)==2 and len(frames)>=2
                and support.any() and not np.any(scores<=-.45)):
            # A median can lose a visible feature. Require a single real image
            # from the other flank to reproduce the order on two target frames.
            other=int(np.flatnonzero(~support.any(axis=0))[0])
            individual=np.array([[appearance_vote(ref,s) for s in samples]
                                 for ref in references[other]])*(1-2*flip)
            agrees=(individual>=.45)&support[:,1-other]
            witnesses=np.flatnonzero(agrees.sum(axis=1)>=2)
            if len(witnesses) and not np.any(individual<=-.45):
                corroborated=True
                entry['supporting_reference']=refs[other][witnesses[0]]
                entry['supporting_frames']=[frames[j] for j in np.flatnonzero(agrees[witnesses[0]])]
        if not corroborated or np.any(scores<=-.45):
            entry['reason']='insufficient_local_end_appearance'
            continue
        entry.update(accepted=True,reason='visible_end_track_candidate',target_order=flip)
        for i in frames:
            if flip:
                lines[i]=lines[i,::-1]
            facts[i].update(head_known=True,manual_lineage=False,head_source=entry['reason'],
                            head_link_boundary=[left,right],head_appearance_support=int(support.any(axis=1).sum()))
    return audit


def recover_flanked_candidates(lines, facts, fps, seed, excluded, guards, feature):
    """Fill a short visible break using already oriented images on both sides, as candidates."""
    def same_track(i):
        return i not in excluded and (guards is None or (
            guards[i]['mask_valid'] and guards[i]['identity_valid'] and not guards[i].get('source_frame_invalid')
            and guards[i]['identity_segment']==guards[seed]['identity_segment']))

    if not same_track(seed):
        return []
    first=last=seed
    while first>0 and same_track(first-1):
        first-=1
    while last+1<len(facts) and same_track(last+1):
        last+=1

    audit=[]
    for missing,indices in groupby(range(first,last+1),lambda i: bool(facts[i] and not facts[i]['head_known'])):
        if not missing:
            continue
        middle=list(indices)
        lo,hi=middle[0],middle[-1]
        if lo==first or hi==last:
            continue
        if len(middle)>2:
            if (hi-lo+2)/fps<=1.0:
                audit.extend(recover_visible_run(lines,facts,middle,first,last,fps,feature))
            continue
        if (hi-lo+2)/fps>.30:
            continue
        left,right=facts[lo-1],facts[hi+1]
        if not left or not right:
            continue
        # References stay within their own continuous track. New guesses never seed another gap.
        left_refs=list(range(max(first,left['component'][0],lo-5),lo))
        right_refs=list(range(hi+1,min(last,right['component'][1],hi+5)+1))
        if min(len(left_refs),len(right_refs))<3:
            continue
        if not all(facts[i]['head_known'] and facts[i]['head_source'] in REFERENCE_SOURCES
                for i in left_refs+right_refs):
            continue
        if not all(facts[i]['candidate']=='clear_observation' and facts[i]['head_observed']
                   and facts[i]['tail_observed'] and not facts[i]['anchor_conflict']
                   for i in left_refs+middle+right_refs):
            continue
        entry=dict(component=[lo,hi],boundary=[lo-1,hi+1],reference_frames=[left_refs,right_refs],
                   accepted=False,candidate_only=True)
        audit.append(entry)
        match=flanked_order(lines,facts,middle)
        if match is None:
            entry['reason']='ambiguous_flanked_motion'
            continue
        sides,errors=match
        entry.update(target_orders=sides.tolist(),terminal_errors_bl=errors.tolist())
        templates=[np.median([feature(i) for i in refs],axis=0) for refs in (left_refs,right_refs)]
        if appearance_vote(*templates)<.45:
            entry['reason']='flank_appearance_disagrees'
            continue
        samples=[feature(i) for i in middle]
        votes=np.array([[appearance_vote(t,sample) for t in templates] for sample in samples])
        votes*=1-2*sides[:,None]
        entry['appearance_votes']=votes.tolist()
        # A two-frame continuous component may share one clear appearance observation.
        # Each frame still needs its own unambiguous motion and must not contradict either flank.
        support=np.all(votes>=.45,axis=1)
        consistent=True
        for _,group in groupby(range(len(middle)),lambda j:facts[middle[j]]['component']):
            idx=list(group)
            if np.ptp(sides[idx]) or not support[idx].any() or np.any(votes[idx]<=-.45):
                consistent=False
                break
        if not consistent:
            entry['reason']='insufficient_local_end_appearance'
            continue
        entry.update(accepted=True,reason='flanked_motion_and_appearance_candidate',
                     support_frames=[i for i,ok in zip(middle,support) if ok])
        for i,flip in zip(middle,sides):
            if flip:
                lines[i]=lines[i,::-1]
            facts[i].update(head_known=True,manual_lineage=False,head_source=entry['reason'],
                            head_link_boundary=entry['boundary'],head_appearance_support=int(support.sum()))
    return audit

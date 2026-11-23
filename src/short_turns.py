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
        if len(middle)>2 or (hi-lo+2)/fps>.30 or lo==first or hi==last:
            continue
        left,right=facts[lo-1],facts[hi+1]
        if not left or not right:
            continue
        # References stay within their own continuous track. New guesses never seed another gap.
        left_refs=list(range(max(first,left['component'][0],lo-5),lo))
        right_refs=list(range(hi+1,min(last,right['component'][1],hi+5)+1))
        if min(len(left_refs),len(right_refs))<3:
            continue
        if not all(facts[i]['head_known'] and facts[i]['head_source'] in (
                'initial_mark','auto_taper_seed','recovered_marked_end','multi_frame_appearance_candidate')
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

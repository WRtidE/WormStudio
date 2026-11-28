"""Rejoin observed head tracks to an original human mark across a short pose break."""
import numpy as np
from scipy.ndimage import map_coordinates

from body_geometry import local_field
from head_appearance import sample_profile
from head_recovery import appearance_vote, end_contrast
from skeleton_geometry import visible_terminal_branches
from terminal_overlap import TERMINAL_FRACTION, match_shaft
from short_turns import bracketed_order


def fingerprint(gray, mask, line):
    field, origin = local_field(mask)
    local = line - origin
    h, w = field.shape
    x, y = origin
    radius = map_coordinates(field, local.T[::-1], order=1, mode='constant')
    profile = sample_profile(gray[y:y+h, x:x+w], local, radius)
    return np.stack([end_contrast(radius, True), end_contrast(profile)])


def clear_sample(facts, component, limit):
    lo, hi = component
    clear = [i for i in range(lo, hi+1) if facts[i]['candidate'] == 'clear_observation'
             and facts[i]['head_observed'] and facts[i]['tail_observed']]
    if len(clear) <= limit:
        return clear
    return [clear[i] for i in np.linspace(0, len(clear)-1, limit).round().astype(int)]


def match_tip(lines, start, end, source_side, length, gap_tips):
    """Compare one visible terminal tenth with each end of the emerging pose."""
    step = 1 if end > start else -1
    before, after = start-step, end+step
    span = abs(end-start)
    old, current = (lines[i, ::-1] if source_side else lines[i] for i in (before,start))
    head = current[:11]
    velocity = head - old[:11]
    forecast = head + span * velocity
    errors = []
    for flip in (False, True):
        first, second = (lines[i, ::-1] if flip else lines[i] for i in (end,after))
        tip = first[:11]
        back = tip - span * (second[:11]-tip)
        errors.append([np.linalg.norm(forecast-tip, axis=1).mean()/length,
                       np.linalg.norm(back-head, axis=1).mean()/length])
    errors = np.asarray(errors)
    winner = int(errors.mean(axis=1).argmin())
    # Both directions must prefer this end. A common translation or a close
    # pair of stationary tips alone cannot identify the endpoint.
    if errors[winner].max() > .16 or np.min(errors[1-winner]-errors[winner]) < .06:
        return None
    first, second = (lines[i, ::-1] if winner else lines[i] for i in (end,after))
    if np.linalg.norm(first[0]-head[0]) > min(.35*span, .45)*length:
        return None
    evidence = dict(motion_frames=[before,start,end,after],
                    prediction_errors_bl=errors[winner].tolist(), observed_gap_tips=[])
    for i,tips in gap_tips:
        forward = head[0]+abs(i-start)*velocity[0]
        backward = first[0]-abs(end-i)*(second[0]-first[0])
        near = tips[(np.linalg.norm(tips-forward,axis=1) <= .09*length) &
                    (np.linalg.norm(tips-backward,axis=1) <= .09*length)]
        if len(near) != 1:
            return None
        evidence['observed_gap_tips'].append(dict(frame=i,xy=near[0].tolist()))
    return winner, evidence


def motion_match(lines, facts, masks, start, end, source_flip):
    step = 1 if end > start else -1
    before, after = start-step, end+step
    if not all(facts[i]['head_observed'] and facts[i]['tail_observed'] for i in (end,after)):
        return None,dict(reason='ambiguous_endpoint_motion')
    length = min(facts[start]['length_px'], facts[end]['length_px'])
    branches = {i:visible_terminal_branches(masks[i],length,TERMINAL_FRACTION*length)
                for i in range(start+step,end,step)}
    tips = [(i,np.asarray([path[0] for path in paths]).reshape(-1,2)) for i,paths in branches.items()]
    matches = {}
    for side,field in enumerate(('head_observed','tail_observed')):
        if not (facts[before][field] and facts[start][field]):
            continue
        root_end = side ^ source_flip
        name = 'tail' if root_end else 'head'
        motion = match_tip(lines,start,end,side,length,tips)
        shaft = match_shaft(lines,start,end,side,branches,length)
        if not (motion or shaft):
            continue
        if motion and shaft and (motion[0] != shaft[0] or
                motion[1]['observed_gap_tips'] != shaft[1]['observed_gap_tips']):
            return None,dict(reason='motion_and_shaft_disagree',tracked_end=name,
                             motion_side=motion[0],shaft_side=shaft[0],motion=motion[1],shaft_overlap=shaft[1])
        target_side, evidence = motion or (shaft[0],dict(observed_gap_tips=shaft[1]['observed_gap_tips']))
        evidence['method'] = 'motion_and_shaft' if motion and shaft else 'tip_motion' if motion else 'shaft_overlap'
        evidence['target_side'] = target_side
        if shaft:
            evidence['shaft_overlap'] = shaft[1]
        matches[name] = (target_side ^ root_end,evidence)
    if len({flip for flip,_ in matches.values()}) != 1:
        return None,dict(reason='opposing_end_tracks' if matches else 'ambiguous_endpoint_motion',
                         tracked_ends={name:e for name,(_,e) in matches.items()})
    flip = next(iter(matches.values()))[0]
    return flip, dict(tracked_ends={name:e for name,(_,e) in matches.items()})


def turn_window(facts, components, j, step, fps, runs, *, marked_flank=False):
    """Collect adjacent singleton poses and two observations on the far flank."""
    source = components[j-step][1 if step==1 else 0]
    targets = []
    first_frames = []
    while 0 <= j < len(components) and len(targets) < 3:
        lo,hi = components[j]
        first = lo if step==1 else hi
        if (first != (first_frames[-1] if first_frames else source)+step
                or runs[lo] != runs[source] or runs[hi] != runs[source]
                or facts[lo]['anchor_conflict'] or (facts[lo]['anchor_frames'] and not (marked_flank and lo!=hi))):
            return None
        targets.append(j)
        first_frames.append(first)
        if lo!=hi:
            break
        j+=step
    if hi-lo < 2 or abs(first-source)/fps > .30:
        return None
    frames=[source,*first_frames,first+step]
    if not all(facts[i]['candidate']=='clear_observation' and facts[i]['head_observed']
               and facts[i]['tail_observed'] for i in frames):
        return None
    return targets,frames


def bracketed_candidates(lines, facts, components, j, step, source_flip, fps, runs, templates, feature):
    """Candidate only. Require contiguous clear singleton poses and a measured far flank."""
    window=turn_window(facts,components,j,step,fps,runs)
    if window is None:
        return None
    targets,frames=window
    match=bracketed_order(lines,frames,source_flip,min(facts[i]['length_px'] for i in frames))
    if match is None:
        return None
    sides,evidence=match
    support=[]
    for target,flip in zip(targets,sides):
        sampled=clear_sample(facts,components[target],18)
        votes=np.array([[appearance_vote(t,feature(i)) for t in templates] for i in sampled])
        signed=-votes if flip else votes
        agreeing=[i for i,row in zip(sampled,signed) if np.count_nonzero(row >= .45)>=2]
        minimum=1 if components[target][0]==components[target][1] else 3
        if len(agreeing)<max(minimum,.75*len(sampled)) or np.any(signed <= -.55):
            return None
        support.extend(agreeing)
    evidence.update(support_frames=support,boundary=[frames[0],frames[-2]],source_flip=source_flip,
                    target_orders=list(sides),target_components=[components[k] for k in targets])
    return list(zip(targets,sides)),evidence


def recover_between_marks(lines, facts, components, committed, fps, runs, references, feature):
    """Verify isolated poses between tracks independently oriented by different human marks."""
    audit=[]
    flanks=sorted(committed)
    for left,right in zip(flanks,flanks[1:]):
        if right-left not in (2,3):
            continue
        start,end=components[left][1],components[right][0]
        left_marks,right_marks=(set(facts[i]['anchor_frames']) for i in (start,end))
        if (not left_marks.isdisjoint(right_marks) or components[left][0]==start or runs[start]<0
                or runs[start-1]!=runs[start]):
            continue
        window=turn_window(facts,components,left+1,1,fps,runs,marked_flank=True)
        if window is None or window[0][-1]!=right:
            continue
        frames=window[1]
        preceding=facts[start-1]
        if not (preceding['candidate']=='clear_observation' and preceding['head_observed'] and preceding['tail_observed']):
            continue
        middle=frames[1:-2]
        marks=sorted(left_marks | right_marks)
        entry=dict(boundary=[start,end],component=[middle[0],middle[-1]],original_marks=marks,accepted=False)
        audit.append(entry)
        reverse=[end,*reversed(middle),start,start-1]
        length=min(facts[i]['length_px'] for i in [start-1,*frames])
        forward=bracketed_order(lines,frames,0,length)
        backward=bracketed_order(lines,reverse,0,length)
        if forward is None or backward is None:
            entry['reason']='ambiguous_two_sided_turn'
            continue
        order,entry['forward']=forward
        back_order,entry['backward']=backward
        conflict=order[-1]!=0 or back_order[-1]!=0 or order[:-1]!=back_order[:-1][::-1]
        roots=sorted({facts[i]['component'][0] for i in marks})
        if not conflict:
            if any(k not in references for k in roots):
                entry['reason']='insufficient_marked_appearance'
                continue
            entry['reference_frames']=[references[k][0] for k in roots]
            votes=np.array([[[appearance_vote(t,feature(i)) * (1-2*side) for t in references[k][1]]
                             for k in roots] for i,side in zip(middle,order)])
            entry['appearance_votes']=votes.tolist()
            conflict=bool(np.any(votes <= -.55))
            if not conflict and np.any((votes >= .45).sum(axis=2)<2):
                entry['reason']='insufficient_marked_appearance'
                continue
        if conflict:
            entry['reason']='conflicting_two_sided_turn'
            for i in middle:
                facts[i].update(anchor_conflict=True,anchor_frames=marks,head_source=entry['reason'])
            continue
        entry.update(accepted=True,reason='two_sided_marked_turn',target_orders=list(order[:-1]),support_frames=middle)
        for i,side in zip(middle,order):
            if side:
                lines[i]=lines[i,::-1]
            # All endpoints in this window are observed; no endpoint needs inferring.
            facts[i].update(head_known=True,manual_lineage=True,head_source='recovered_marked_end',
                            anchor_frames=marks,head_link_boundary=[start,end],
                            head_appearance_support=len(middle))
    return audit


def recover_marked_end(lines, facts, masks, gray, fps, marks, excluded, guards):
    """Discover links from frozen human-rooted tracks before changing any assignments."""
    components = [tuple(f['component']) for i,f in enumerate(facts) if f and f['component'][0] == i]
    roots = [k for k,(lo,hi) in enumerate(components) if facts[lo]['manual_lineage']]
    if not roots or len(components) < 2:
        return []
    # Each trustworthy identity run has a distinct number, even if a source
    # reuses a segment ID after an invalid frame. A mark on a lost pose is a barrier.
    blocked = excluded | {a['frame'] for a in marks if not facts[a['frame']]}
    runs = []
    run = 0
    for i in range(len(facts)):
        ok = i not in blocked
        if guards is not None:
            g = guards[i]
            ok = ok and g['mask_valid'] and g['identity_valid'] and not g.get('source_frame_invalid')
            if i and g['identity_segment'] != guards[i-1]['identity_segment']:
                run += 1
        if not ok:
            run += 1
        runs.append(run if ok else -1)
    proposals = {}
    provisional = []
    audit = []
    features = {}
    references = {}

    def feature(i):
        if i not in features:
            features[i] = fingerprint(gray[i], masks[i], lines[i])
        return features[i]

    for k in roots:
        lo, hi = components[k]
        if (runs[lo] < 0 or runs[lo] != runs[hi]
                or not all(facts[i]['head_observed'] and facts[i]['tail_observed'] for i in facts[lo]['anchor_frames'])):
            continue
        refs = clear_sample(facts, components[k],60)
        if len(refs) < 3:
            continue
        # Each disjoint reference set spans the marked track's range of postures.
        templates = [np.median([feature(i) for i in refs[offset::3]],axis=0) for offset in range(min(3, len(refs)))]
        references[lo]=(refs,templates)
        for step in (-1,1):
            source_flip = 0
            pending = []
            for j in range(k+step, len(components) if step == 1 else -1, step):
                a,b = components[j-step]
                c,d = components[j]
                start,end = (b,c) if step == 1 else (a,d)
                if (facts[c]['anchor_frames'] or facts[c]['anchor_conflict'] or runs[c] != runs[lo]
                        or runs[d] != runs[lo]):
                    break
                entry = dict(component=[c,d], original_marks=facts[lo]['anchor_frames'],
                             reference_frames=refs, boundary=[start,end], accepted=False)
                audit.append(entry)
                if a == b or c == d or abs(end-start)/fps > .30:
                    entry['reason'] = 'insufficient_temporal_context'
                    if a!=b and c==d and not pending:
                        turn=bracketed_candidates(lines,facts,components,j,step,source_flip,fps,runs,templates,feature)
                        if turn is not None:
                            orders,details=turn
                            entry.update(details,reason='bracketed_turn_pending')
                            provisional.append((j-step,orders,entry))
                    break
                flip,evidence = motion_match(lines,facts,masks,start,end,source_flip)
                entry.update(evidence)
                if flip is None:
                    break
                frames = clear_sample(facts, components[j],18)
                votes = np.array([[appearance_vote(t,feature(i)) for t in templates] for i in frames])
                signed = -votes if flip else votes
                support = [i for i,row in zip(frames,signed) if np.count_nonzero(row >= .45) >= 2]
                entry.update(support_frames=support, sample_frames=frames, reverse=bool(flip))
                # A two-frame passage needs matching motion at BOTH boundaries
                # and a further independently supported block before it can be used.
                passage = (d-c == 1 and len(frames) == 2 and len(support) >= 1
                           and np.all(np.median(signed,axis=1) >= .30))
                strong_appearance = len(support) >= max(3, .75*len(frames))
                reciprocal = False
                if abs(end-start)==1 and not strong_appearance:
                    back, backward = motion_match(lines,facts,masks,end,start,flip)
                    entry['backward_check'] = backward
                    backward_ends = backward.get('tracked_ends',{})
                    common = [name for name,track in evidence['tracked_ends'].items()
                              if name in backward_ends and 'motion_frames' in track
                              and 'motion_frames' in backward_ends[name]]
                    reciprocal = back == source_flip and bool(common)
                    if reciprocal:
                        entry['reciprocal_ends'] = common
                if (not (strong_appearance or passage or reciprocal) or np.any(signed <= -.55)):
                    entry['reason'] = 'appearance_insufficient_or_conflicting'
                    break
                pending.append((j,flip,entry))
                entry['reason'] = 'awaiting_more_clear_frames'
                if len(support) >= 3 or reciprocal:
                    for target,side,link in pending:
                        proposals.setdefault(target,[]).append((side,link))
                    pending.clear()
                source_flip = flip
    committed = {k:0 for k in roots}
    for target, links in proposals.items():
        c,d = components[target]
        anchors = sorted({i for _,link in links for i in link['original_marks']})
        if len({side for side,_ in links}) > 1:
            for _,link in links:
                link['reason'] = 'opposing_marked_tracks'
            for i in range(c,d+1):
                facts[i].update(anchor_conflict=True,anchor_frames=anchors,head_source='opposing_marked_tracks')
            continue
        flip, evidence = links[0]
        committed[target]=flip
        for _,link in links:
            shaft_only = all(e['method']=='shaft_overlap' for e in link['tracked_ends'].values())
            reason = 'marked_end_shaft_and_appearance' if shaft_only else 'marked_end_motion_and_appearance'
            link.update(accepted=True,reason='reciprocal_marked_end' if link.get('reciprocal_ends') else reason)
        for i in range(c,d+1):
            f = facts[i]
            if flip:
                lines[i] = lines[i,::-1]
                f['head_observed'],f['tail_observed'] = f['tail_observed'],f['head_observed']
            f.update(head_known=True, manual_lineage=True, head_source='recovered_marked_end',
                     anchor_frames=anchors, head_link_boundary=evidence['boundary'],
                     head_appearance_support=len(evidence['support_frames']))
    if len(roots)>1:
        audit.extend(recover_between_marks(lines,facts,components,committed,fps,runs,references,feature))
    # A provisional bridge cannot become a new seed or override another marked track.
    votes={}
    for source,orders,entry in provisional:
        if committed.get(source)==entry['source_flip']:
            for k,side in orders:
                votes.setdefault(k,set()).add(side)
    for source,orders,entry in provisional:
        if (committed.get(source)!=entry['source_flip'] or any(votes.get(k)!={side} or
                facts[components[k][0]]['anchor_conflict'] or (k in committed and committed[k]!=side)
                for k,side in orders)):
            entry['reason']='conflicting_bracketed_tracks'
            continue
        entry.update(reason='bracketed_trajectory_and_appearance_candidate',candidate_only=True)
        for k,side in orders:
            lo,hi=components[k]
            if facts[lo]['head_known']:
                continue
            for i in range(lo,hi+1):
                f=facts[i]
                if side:
                    lines[i]=lines[i,::-1]
                    f['head_observed'],f['tail_observed']=f['tail_observed'],f['head_observed']
                f.update(head_known=True,manual_lineage=False,head_source=entry['reason'],
                         head_link_boundary=entry['boundary'],head_appearance_support=len(entry['support_frames']))
    return audit

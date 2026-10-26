"""Conservative single-worm branch recovery with explicit abstention.

Only a single compact contact with two observable tips is resolvable here.
Closed loops, hidden tips and multiple contacts are not guessed. A contact
must have reliable simple poses on both sides and a distinct branch choice.
Scores and thresholds are engineering checks, not calibrated accuracy.
"""
import numpy as np
from .skeleton import extract_niu, line_metrics, valid_line, align
from .geometry import topology_candidates, geometry_quality

VERSION = 'conservative-crossing-v1'
DEFAULTS = dict(max_anchor_seconds=2., min_branch_margin=.75,
    min_one_sided_margin=.25, max_length_deviation=.15,
    max_step_shape_fraction=.15, max_step_endpoint_fraction=.20)


def ordered_error(a, b):
    """Correspondence distance, allowing an arbitrary A/B reversal."""
    return min(float(np.mean(np.linalg.norm(a-b,axis=1))),
               float(np.mean(np.linalg.norm(a[::-1]-b,axis=1))))


def sequence_choice(frames, reference):
    """Viterbi and forward/backward min-margins for DISTINCT path candidates."""
    states=[]
    for candidates in frames:
        states.append([dict(candidate=k,points=c['points'][::-1] if reverse else c['points'],unary=c['unary'])
                       for k,c in enumerate(candidates) for reverse in [False,True]])
    if not states or any(not s for s in states):raise ValueError('Missing candidate in sequence')
    transitions=[]
    for left,right in zip(states,states[1:]):
        a=np.array([s['points'] for s in left]);b=np.array([s['points'] for s in right])
        a=a-a.mean(1,keepdims=True);b=b-b.mean(1,keepdims=True)
        delta=a[:,None]-b[None]
        shape=(delta**2).sum(-1).mean(-1)
        ends=(delta[:,:,[0,-1]]**2).sum(-1).mean(-1)
        transitions.append((shape+.5*ends)/max(.10*reference,1.)**2)
    forward=[np.array([s['unary'] for s in states[0]])];pointers=[]
    forward[0][1::2]+=1000  # A/B order only; not an anatomical head label.
    for i in range(1,len(states)):
        cost=forward[-1][:,None]+transitions[i-1]
        pointers.append(cost.argmin(0))
        forward.append(cost.min(0)+np.array([s['unary'] for s in states[i]]))
    chosen=[int(forward[-1].argmin())]
    for ptr in pointers[::-1]:chosen.append(int(ptr[chosen[-1]]))
    chosen=chosen[::-1]
    backward=[None]*len(states);backward[-1]=np.zeros(len(states[-1]))
    for i in range(len(states)-2,-1,-1):
        unary=np.array([s['unary'] for s in states[i+1]])
        backward[i]=(transitions[i]+unary[None]+backward[i+1][None]).min(1)
    result=[]
    for i,index in enumerate(chosen):
        selected=states[i][index]
        # Comparing crossing counts is insufficient: two incorrect traversal
        # orders can have the same number of projected intersections.
        distinct=[j for j,s in enumerate(states[i]) if s['candidate']!=selected['candidate']
            and ordered_error(s['points'],selected['points'])>max(1.,reference*.01)]
        total=forward[i]+backward[i]
        unary=np.array([s['unary'] for s in states[i]])
        right=unary+backward[i]
        own=[j for j,s in enumerate(states[i]) if s['candidate']==selected['candidate']]
        margin=lambda costs: float(min(costs[j] for j in distinct)-min(costs[j] for j in own)) if distinct else None
        result.append(dict(points=selected['points'].copy(),candidate=selected['candidate'],
            branch_margin=margin(total),left_margin=margin(forward[i]),right_margin=margin(right)))
    return result


def resolve_sequence(masks, mask_valid=None, fps=30., parameters=None):
    cfg=dict(DEFAULTS,**(parameters or {}));count=len(masks)
    if not count or not np.isfinite(fps) or fps<=0:raise ValueError('Invalid frame sequence')
    mask_valid=np.ones(count,bool) if mask_valid is None else np.asarray(mask_valid,bool)
    if mask_valid.shape!=(count,):raise ValueError('Mask validity frame mismatch')
    records=[];simple_lengths=[]
    for frame_index,mask in enumerate(masks):
        mask=np.asarray(mask,bool);niu=extract_niu(mask);candidates=[]
        if niu['topology']=='simple' and valid_line(niu['ordered']):
            candidates=[dict(points=niu['ordered'],name='simple_medial',connection_cost=0.)]
            kind='simple'
        else:
            candidates,_,info=topology_candidates(mask)
            kind=info['kind']
            # Never allow a less conservative pixel graph to override a
            # robust graph's complex/closed topology as a simple body.
            if kind=='simple':kind='unresolved_contact';candidates=[]
        for c in candidates:
            c['metric']=line_metrics(c['points'],mask,niu['raw_xy'],niu['radius'])
        if mask_valid[frame_index] and kind=='simple' and candidates[0]['metric']['geometric_pass']:
            simple_lengths.append(candidates[0]['metric']['length_px'])
        records.append(dict(candidates=candidates,topology=kind,niu=niu))
    reference=float(np.median(simple_lengths)) if simple_lengths else None
    fallback=[c['metric']['length_px'] for r in records for c in r['candidates']]
    score_reference=reference or (float(np.median(fallback)) if fallback else 1.)
    max_gap=max(1,round(cfg['max_anchor_seconds']*fps))
    reliable_anchors=[i for i,r in enumerate(records) if mask_valid[i] and r['topology']=='simple'
        and r['candidates'][0]['metric']['geometric_pass']
        and abs(r['candidates'][0]['metric']['length_px']/score_reference-1)<=cfg['max_length_deviation']]
    brackets={}
    for f,r in enumerate(records):
        if r['topology']!='paired_contact':continue
        left=[a for a in reliable_anchors if a<f];right=[a for a in reliable_anchors if a>f]
        if left and right:
            a,b=left[-1],right[0]
            if b-a<=max_gap and mask_valid[a:b+1].all():brackets[f]=[a,b]
    for r in records:
        for c in r['candidates']:
            m=c['metric']
            c['unary']=float(12*(1-m['inside_fraction'])+4*(1-m['skeleton_coverage'])
                +8*abs(m['length_px']/score_reference-1)+2*c['connection_cost']
                +.5*min((m['max_turn_deg']/90)**2,16))
    output=np.full((count,101,2),np.nan);diagnostic=output.copy();quality=[]
    for f,r in enumerate(records):
        quality.append(dict(frame=f,mask_valid=bool(mask_valid[f]),centerline_valid=False,
            topology=r['topology'],selected_candidate='none',branch_score_margin=None,
            quality_reasons=[],reference_length_px=reference))
    available=np.array([bool(r['candidates']) for r in records]) & mask_valid
    groups=np.split(np.flatnonzero(available),np.where(np.diff(np.flatnonzero(available))>1)[0]+1)
    for group in groups:
        if not len(group):continue
        selected=sequence_choice([records[f]['candidates'] for f in group],score_reference)
        previous=None
        for f,choice in zip(group,selected):
            f=int(f);r=records[f];q=quality[f];c=r['candidates'][choice['candidate']]
            p=choice['points'];m=c['metric'];reasons=[]
            diagnostic[f]=p
            if not m['geometric_pass']:reasons.append('centerline_geometry_review')
            if reference and abs(m['length_px']/reference-1)>cfg['max_length_deviation']:
                reasons.append('length_outlier')
            if r['topology']=='paired_contact':
                bracket=brackets.get(f,[])
                q['temporal_anchors']=bracket
                if reference is None or not bracket or bracket[1]-bracket[0]>max_gap:
                    reasons.append('crossing_without_two_reliable_anchors')
                margins=[choice['branch_margin'],choice['left_margin'],choice['right_margin']]
                if any(v is None for v in margins) or margins[0]<cfg['min_branch_margin'] or min(margins[1:])<cfg['min_one_sided_margin']:
                    reasons.append('ambiguous_branch_pairing')
                q.update(branch_score_margin=choice['branch_margin'],left_branch_margin=choice['left_margin'],right_branch_margin=choice['right_margin'])
            if previous is not None:
                pp=align(p,previous);delta=(pp-pp.mean(0))-(previous-previous.mean(0))
                if np.linalg.norm(delta,axis=1).mean()/score_reference>cfg['max_step_shape_fraction'] or np.linalg.norm(delta[[0,-1]],axis=1).max()/score_reference>cfg['max_step_endpoint_fraction']:
                    reasons.append('centerline_temporal_jump')
            q.update(selected_candidate=c['name'],quality_reasons=reasons,centerline_valid=not reasons,
                inside_fraction=m['inside_fraction'],skeleton_coverage=m['skeleton_coverage'],
                projection_crossings=geometry_quality(p)['projection_crossings'])
            if not reasons:output[f]=p
            previous=p
    for f,q in enumerate(quality):
        if not mask_valid[f]:q['quality_reasons'].append('tracking_contact_or_area_outlier')
        if not records[f]['candidates']:q['quality_reasons'].append('unresolved_self_contact_or_missing_tips')
        if q['quality_reasons']:q['centerline_valid']=False
    return output,quality,diagnostic

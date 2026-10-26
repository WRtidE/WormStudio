"""Image-constrained whole-window inference using full TAPNext++ evidence."""
import numpy as np
from .evidence import prepare, candidate_cost
from .fusion import feasible_quality, frame_candidates
from .contact import candidate_states, _transition
from .skeleton import extract_niu, valid_line

VERSION = 'tapnext-evidence-v4-experimental'


def select_with_margins(states, reference):
    """Exact second-order DP and whole-sequence min-marginal ambiguity gaps.

    A gap is an energy difference, not a calibrated confidence probability.
    """
    n=len(states);scale=max(5.,.08*reference)
    unary=[np.array([s['unary'] for s in choices]) for choices in states]
    if n==1:
        k=int(unary[0].argmin());return [k], [unary[0]-unary[0][k]], float(unary[0][k])
    p=[np.array([s['points'] for s in choices]) for choices in states]
    transition=[_transition(p[i][:,None],p[i+1][None],scale) for i in range(n-1)]
    def acceleration(i):
        a=p[i][None,None]-2*p[i-1][None,:,None]+p[i-2][:,None,None]
        return .6*_transition(np.zeros_like(a),a,scale)
    forward=[unary[0][:,None]+unary[1][None]+transition[0]];pointers=[]
    for i in range(2,n):
        scores=forward[-1][:,:,None]+acceleration(i)
        pointers.append(scores.argmin(0))
        forward.append(scores.min(0)+transition[i-1]+unary[i][None])
    j,k=np.unravel_index(forward[-1].argmin(),forward[-1].shape)
    best=float(forward[-1][j,k]);chosen=[int(k),int(j)]
    for ptr in pointers[::-1]:chosen.append(int(ptr[chosen[-1],chosen[-2]]))
    backward=[None]*(n-1);backward[-1]=np.zeros_like(forward[-1])
    for i in range(n-2,0,-1):
        future=transition[i]+unary[i+1][None]+backward[i]
        backward[i-1]=(acceleration(i+1)+future[None]).min(-1)
    margins=[(forward[0]+backward[0]).min(1)-best]
    margins.extend((a+b).min(0)-best for a,b in zip(forward,backward))
    return chosen[::-1],margins,best


def fit(d,z,motion_weight=2.):
    evidence=prepare(d,z);ref=d['reference_length'];n=len(d['masks'])
    bracketed=bool(np.isfinite(d['last']).all() and np.asarray(d['mask_valid']).all())
    states=[];topologies=[]
    for i,mask in enumerate(d['masks']):
        niu=extract_niu(mask);simple=niu['topology']=='simple' and valid_line(niu['ordered'])
        topologies.append(niu['topology'])
        if not d['mask_valid'][i] or not mask.any(): choices=[]
        elif bracketed:
            # Enumerate self-contact hypotheses even when a shorter path
            # already exists. Missing output is not the sole trigger anymore.
            choices=candidate_states(mask,ref,True)
        else:
            choices,_=frame_candidates(mask,ref,[],True)
            for c in choices:
                q=c['quality'];c.update(endpoint_observed=[True,True],
                    unary=(q['length_relative_deviation']/.03)**2+4*(1-q['skeleton_coverage'])+12*(1-q['dense_inside_fraction']))
        if not simple and d['mask_valid'][i] and mask.any():
            observations=[(f'motion_{direction}',z[direction][i],z[f'{direction}_visible'][i]) for direction in ('forward','backward')]
            motion,_=frame_candidates(mask,ref,observations,True)
            for c in motion:
                if c.get('origin')!='motion':continue
                if any(np.linalg.norm(s['points']-c['points'],axis=1).mean()<.5 for s in choices):continue
                q=c['quality']
                # A generic tracked texture point is not an independently
                # observed anatomical tip. Keep these fallback curves REVIEW.
                c.update(endpoint_observed=[False,False],
                    unary=(q['length_relative_deviation']/.03)**2+4*(1-q['skeleton_coverage'])+12*(1-q['dense_inside_fraction']))
                choices.append(c)
        anchor=d['first'] if i==0 else d['last'] if i==n-1 and bracketed else None
        if anchor is not None and d['mask_valid'][i]:
            q=feasible_quality(anchor,mask,ref)
            if q['geometric_pass']:
                choices=[dict(points=anchor.copy(),name='anchor',endpoint_observed=[True,True],quality=q,unary=0.)]
                if i==n-1 and n>1:choices.append(dict(choices[0],points=anchor[::-1].copy()))
        for c in choices:
            c['tap_cost']=candidate_cost(c['points'],evidence['frames'][i])
            c['unary']+=motion_weight*c['tap_cost']
        states.append(choices)
    curves=np.full((n,101,2),np.nan)
    qa=[dict(frame=int(d['frame_ids'][i]),candidate='none',status='UNAVAILABLE',
             mask_valid=bool(d['mask_valid'][i]),finite=False,geometric_pass=False,
             abstention_reason='invalid_mask' if not d['mask_valid'][i] else 'no_feasible_candidate',
             solver=VERSION,**evidence['frames'][i]['diagnostic']) for i in range(n)]
    available=np.flatnonzero([bool(s) for s in states])
    for ids in np.split(available,np.where(np.diff(available)>1)[0]+1):
        if not len(ids):continue
        chosen,margins,cost=select_with_margins([states[i] for i in ids],ref)
        for i,k,margin in zip(ids,chosen,margins):
            c=states[i][k];curves[i]=c['points']
            alternatives=[j for j in range(len(states[i])) if j!=k and np.linalg.norm(states[i][j]['points']-c['points'],axis=1).mean()>1.]
            other=min(alternatives,key=lambda j:margin[j]) if alternatives else None
            complex_pose=topologies[i]!='simple'
            qa[i]=dict(frame=int(d['frame_ids'][i]),candidate=c['name'],solver=VERSION,
                status='TEMPORAL_RECONSTRUCTION_REVIEW' if complex_pose else 'UNCONFIRMED',
                origin='tap_evidence_event' if complex_pose else 'image',
                mask_valid=bool(d['mask_valid'][i]),endpoint_observed=c.get('endpoint_observed',[True,True]),
                candidate_count=len(states[i]),tap_candidate_cost=c['tap_cost'],event_cost=cost,
                alternative_energy_gap=float(max(0.,margin[other])) if other is not None else None,
                alternative_candidate=states[i][other]['name'] if other is not None else None,
                **c['quality'],**evidence['frames'][i]['diagnostic'])
    return curves,qa

"""Recover a marked end of one worm from image-supported pose and end appearance."""
from functools import lru_cache
import numpy as np

LANDMARKS = np.array([0, .04, .09, .15, .23, .34, .50, .66, .77, .85, .91, .96, 1.])
END_BINS = (.03, .06, .10, .15, .20, .25)
VERSION = 'single-well-sampled-ends-v4'


def tracking_indices(count=13):
    """Symmetric indices into the 101-point centerline, including both ends."""
    if type(count) is not int or not 2 <= count <= 101:
        raise ValueError('中线跟踪点数须为 2 至 101 的整数')
    return _tracking_indices(count)


@lru_cache(maxsize=100)
def _tracking_indices(count):
    pairs = count // 2
    if pairs <= 6:
        positions = np.interp(np.arange(pairs)/(count-1), np.linspace(0,1,13), LANDMARKS)
        left = np.rint(100*positions).astype(int)
    else:
        # Retain the original end landmarks and split the widest remaining gaps.
        knots = [0,4,9,15,23,34,50]
        while len(knots)-1 < pairs:
            i = int(np.argmax(np.diff(knots)))
            knots.insert(i+1, (knots[i]+knots[i+1])//2)
        left = np.array(knots[:-1])
    indices = np.concatenate((left, [50] if count%2 else [], 100-left[::-1])).astype(int)
    indices.setflags(write=False)
    return indices


def pose_parity(a, b, indices=None):
    """Compare the two correspondences after accounting for a shared body translation."""
    length=min(np.linalg.norm(np.diff(p,axis=0),axis=1).sum() for p in (a,b))
    if length <= 0:
        return None
    idx=np.rint((tracking_indices() if indices is None else indices)*(len(a)-1)/100).astype(int)
    left, right = a[idx], b[idx]
    shift=right.mean(0)-left.mean(0)
    if np.linalg.norm(shift) > .55*length:
        return None
    residuals=[np.linalg.norm(q-left-shift,axis=1)/length for q in (right,right[::-1])]
    costs=np.array([np.mean(e) for e in residuals])
    best=int(costs.argmin())
    if costs[best] > .19 or np.ptp(costs)<.09 or max(residuals[best][[0,-1]])>.45:
        return None
    return best


def end_contrast(profile, width=False):
    """A signed spatial fingerprint of the difference between the two terminal quarters."""
    if profile is None or not np.isfinite(profile).all():
        return np.full(len(END_BINS),np.nan)
    u=np.linspace(0,1,len(profile))
    scale=np.median(profile[(u>=.35)&(u<=.65)]) if width else 1.
    if scale<=0:
        return np.full(len(END_BINS),np.nan)
    return np.array([np.median((profile-profile[::-1])[np.abs(u-f)<=max(.022,.51/(len(u)-1))])/scale
                     for f in END_BINS])


def appearance_vote(template, sample):
    """Compare signed width and grayscale differences, omitting indistinguishable ends."""
    scores=[]
    for ref, obs, floor in zip(template, sample, (.12,.32)):
        if not (np.isfinite(ref).all() and np.isfinite(obs).all()):
            continue
        power=np.mean(ref*ref),np.mean(obs*obs)
        if min(power)<floor**2:
            continue
        scores.append(2*np.mean(obs*ref)/sum(power))
    if not scores or (min(scores)<-.45 and max(scores)>.45):
        return 0.
    return float(np.mean(scores))


def recover(results, poses, usable, identities, radii, gray, anchors, cfg, fps):
    """Only apply in a fixed single-target field after a valid manual mark.

    Geometry never links across an identity break. A new block must independently
    match the frozen marked-end appearance on multiple nearby observed frames.
    Recovered assignments remain candidates and cannot grant measurement permissions.
    """
    indices=tracking_indices(cfg.get('tracking_point_count',13))
    usable=np.asarray(usable,bool)
    frames=np.flatnonzero(usable)
    parity=np.zeros(len(poses),int)
    blocks=[]
    for frame in frames:
        link=None
        if blocks:
            prev=blocks[-1][-1]
            if (frame-prev)/fps <= max(.50,1/fps) and np.all(identities[prev:frame+1]==identities[prev]):
                link=pose_parity(poses[prev],poses[frame],indices)
        if link is None:
            blocks.append([])
        else:
            parity[frame]=parity[prev]^link
        blocks[-1].append(frame)

    fingerprints={i:np.stack([end_contrast(radii[i],True) if radii is not None else end_contrast(None),
                              end_contrast(gray[i]) if gray is not None else end_contrast(None)]) for i in frames}
    marks={}
    for anchor in anchors:
        i=anchor['frame']
        if not usable[i]:
            continue
        p=poses[i]
        length=np.linalg.norm(np.diff(p,axis=0),axis=1).sum()
        dist=np.linalg.norm(p[[0,-1]]-anchor['point'],axis=1)
        marks.setdefault(i,[]).append(int(dist.argmin()) if dist.min()<=cfg['manual_max_distance_bl']*length
                  and np.ptp(dist)>=cfg['min_assignment_margin_bl']*length else None)
    seeds={}
    blocked={}
    seed_frames={}
    templates=[]
    ref_frames=set()
    for block_id,block in enumerate(blocks):
        marked=[i for i in block if i in marks]
        if not marked:
            continue
        votes={None if v is None else v^parity[i] for i in marked for v in marks[i]}
        if None in votes:
            blocked[block_id]='manual_point_not_near_endpoint'
            continue
        if len(votes)!=1 or any(results[i].get('head_anchor_conflict') for i in block):
            blocked[block_id]='manual_anchor_conflict'
            continue
        side=votes.pop()
        seeds[block_id]=side
        seed_frames[block_id]=min(marked)
        for i in block:
            if min(abs(i-a) for a in marked)<=fps*.25:
                templates.append(fingerprints[i]*(1-2*(side^parity[i])))
                ref_frames.add(i)
    template=np.full((2,len(END_BINS)),np.nan)
    if templates:
        stack=np.stack(templates)
        for channel in range(2):
            values=stack[:,channel]
            finite=values[np.isfinite(values).all(axis=1)]
            if len(finite):
                template[channel]=np.median(finite,axis=0)
    for block_id,block in enumerate(blocks):
        sequence=np.asarray(block)
        observations=np.array([appearance_vote(template,fingerprints[i])*(1-2*parity[i]) for i in block])
        for j,i in enumerate(block):
            row=results[i]
            was_confirmed=row['head_identity_confirmed']
            old_end=row['head_endpoint']
            is_mark=i in marks
            row.update(head_identity_confirmed=False,head_endpoint=None,head_status='unconfirmed_A_B_endpoints',
                       head_recovery_block=block_id,head_recovery_points=len(indices),head_recovery_support=0,
                       head_temporal_block=block_id,head_temporal_seed_frame=seed_frames.get(block_id),
                       head_temporal_changed=False,head_temporal_conflict=False,
                       head_temporal_link_rms_bl=None,head_temporal_gap_s=None,
                       head_appearance_votes=0,head_appearance_score=0.,
                       head_appearance_reference_frames=len(ref_frames),head_appearance_reference='marked_end',
                       head_reason='marked_end_insufficient',head_evidence_score=0.)
            if block_id in blocked or row.get('head_anchor_conflict'):
                row['head_reason']=blocked.get(block_id,'manual_anchor_conflict')
                row['head_anchor_conflict']=row['head_reason']=='manual_anchor_conflict'
                continue
            nearby=observations[np.abs(sequence-i)<=fps*.5]
            useful=nearby[np.abs(nearby)>=.30]
            score=float(np.mean(useful)) if len(useful) else 0.
            consensus=(len(useful)>=3 and abs(score)>=.45 and
                       np.mean(np.sign(useful)==np.sign(score))>=.75)
            row.update(head_recovery_support=len(useful),head_appearance_votes=len(useful),
                       head_appearance_score=abs(score))
            if block_id in seeds:
                side=seeds[block_id]
                if not is_mark and consensus and score*(1-2*side)<-.55:
                    row['head_reason']='marked_end_evidence_conflict'
                    row['head_temporal_conflict']=True
                    continue
                reason='marked_end_pose_continuity'
            elif consensus:
                side=int(score<0)
                if observations[j]*(1-2*side)<-.55:
                    row['head_reason']='marked_end_evidence_conflict'
                    row['head_temporal_conflict']=True
                    continue
                reason='marked_end_appearance_recovery'
            else:
                continue
            end=int(side^parity[i])
            confirmed=bool(is_mark or (was_confirmed and old_end==end and block_id in seeds))
            row.update(head_endpoint=end,head_identity_confirmed=confirmed,
                       head_status='confirmed_by_user_point' if confirmed else 'unconfirmed_temporal_candidate',
                       head_reason='manual_anchor' if is_mark else reason,
                       head_evidence_score=1. if is_mark else abs(score),
                       head_temporal_changed=old_end!=end)
    return results

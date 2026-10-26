"""Resolve automatic orientation once per image-supported temporal block.

Adapted from Tierpsy/WormPose's separation of posture continuity and head choice.
Automatic seed orientation is provisional, never anatomical ground truth.
"""
import numpy as np

MAX_INTERVAL_S = .25
BLOCKED_REASONS = {'shape_motion_conflict', 'conflicting_shape_votes',
                   'appearance_motion_conflict'}


def geometry_link(a, b):
    """Return b's reversal and auditable costs, or None if correspondence is unclear."""
    if not np.isfinite(a).all() or not np.isfinite(b).all(): return None
    length = min(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in (a,b))
    if length <= 0: return None
    costs = [float(np.sqrt(np.mean(np.sum((a-q)**2, axis=1)))/length) for q in (b,b[::-1])]
    flip = int(costs[1] < costs[0]); q = b[::-1] if flip else b
    span = max(1, round((len(a)-1)*.05))
    u, v = a[span:]-a[:-span], q[span:]-q[:-span]
    if min(np.linalg.norm(u,axis=1).min(),np.linalg.norm(v,axis=1).min()) < 1e-6: return None
    delta = np.arctan2(u[:,1],u[:,0])-np.arctan2(v[:,1],v[:,0])
    angle = float(np.mean(np.abs(np.arctan2(np.sin(delta),np.cos(delta)))))
    center = np.linalg.norm(a.mean(0)-q.mean(0))/length
    end_step = np.linalg.norm(a[[0,-1]]-q[[0,-1]],axis=1).max()/length
    if (min(costs)>.25 or abs(costs[0]-costs[1])<.15 or center>.3
            or end_step>.5 or angle>np.pi/6): return None
    return dict(flip=flip,rms_bl=min(costs),angle_deg=float(np.degrees(angle)))


def stabilize(results, poses, identities, fps, mode):
    """Keep manual/metric segments intact; link only reliable automatic segments."""
    for r in results:
        r.update(head_local_endpoint=r['head_endpoint'], head_local_reason=r['head_reason'],
                 head_temporal_block=-1,head_temporal_seed_frame=None,
                 head_temporal_conflict=False,head_temporal_changed=False,
                 head_temporal_link_rms_bl=None,head_temporal_gap_s=None)
    if mode!='twardis': return
    if not np.isfinite(fps) or fps<=0: raise ValueError('头尾时序关联需要有效帧率')
    groups=[]
    for i,r in enumerate(results):
        if r['head_segment']<0: continue
        if not groups or results[groups[-1][-1]]['head_segment']!=r['head_segment']: groups.append([])
        groups[-1].append(i)
    def blocked(g):
        return any(results[i]['head_anchor_count'] or results[i]['head_reason'] in BLOCKED_REASONS for i in g)
    def tracked(i): return poses[i,::-1] if results[i]['head_input_flipped'] else poses[i]
    blocks=[]
    for g in groups:
        if blocks:
            previous,parity,_=blocks[-1][-1];i,j=previous[-1],g[0]
            # A transient identity boundary also prevents linking back to an earlier ID.
            same_identity=all(identities[k]==identities[i] for k in range(i,j+1))
            if same_identity and (j-i)/fps<=MAX_INTERVAL_S and not blocked(previous) and not blocked(g):
                link=geometry_link(tracked(i),tracked(j))
                if link is not None:
                    link['gap_s']=(j-i)/fps
                    blocks[-1].append((g,parity^link['flip'],link));continue
        blocks.append([(g,0,None)])
    for block_id,block in enumerate(blocks):
        if any(blocked(g) for g,_,_ in block):continue
        seeds=[]
        for g,parity,_ in block:
            r=results[g[0]]
            if r['head_endpoint'] is not None and r['head_evidence_score']>=.08:
                score=r['head_evidence_score']*min(len(g)/fps,1.)
                seeds.append((score,-g[0],g,parity))
        if not seeds:continue
        _,_,seed,seed_parity=max(seeds,key=lambda x:(x[0],x[1]))
        sr=results[seed[0]]
        block_head=sr['head_endpoint'] ^ int(sr['head_input_flipped']) ^ seed_parity
        for g,parity,link in block:
            for i in g:
                r=results[i];end=block_head ^ parity ^ int(r['head_input_flipped'])
                old=r['head_endpoint'];changed=old!=end
                r.update(head_temporal_block=block_id,head_temporal_seed_frame=seed[0],
                         head_temporal_conflict=old is not None and changed,
                         head_temporal_changed=changed,
                         head_temporal_link_rms_bl=None if link is None else link['rms_bl'],
                         head_temporal_gap_s=None if link is None else link['gap_s'])
                if len(block)>1 and changed:
                    r.update(head_endpoint=end,head_status='unconfirmed_temporal_candidate',
                             head_reason='temporal_orientation_conflict' if old is not None else 'temporal_orientation_reference',
                             head_evidence_score=sr['head_evidence_score'])

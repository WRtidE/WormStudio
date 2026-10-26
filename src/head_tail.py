"""Head/tail candidates from body-relative bending, taper and image appearance.

Only endpoint order changes; masks and centerline geometry are never fitted here.
Quality gates and a vote over each continuous track prevent framewise flicker.
No forward-motion assumption is used to flip heads (true reversals are retained).
"""
import numpy as np

VERSION = 'head-tail-v7-initialized-orientation'
DEFAULTS = dict(window_frames=5, min_motion_bl=.005, min_contrast=.15,
                min_vote_fraction=.65, close_endpoints_bl=.10,
                max_step_bl=.25, min_assignment_margin_bl=.02,
                manual_max_distance_bl=.15, min_shape_contrast=.15,
                min_shape_support=.60, min_motion_rad=.04,
                min_appearance_margin=.12, min_appearance_support=.60)

CANDIDATE_STATUSES = ('unconfirmed_twardis_motion_candidate','unconfirmed_taper_candidate',
                      'unconfirmed_relative_motion_candidate','unconfirmed_appearance_candidate','unconfirmed_temporal_candidate')


def relative_endpoint_angles(poses):
    """Terminal tangent angles relative to the middle-body tangent, in radians."""
    n=poses.shape[1]-1
    middle=poses[:,round(.6*n)]-poses[:,round(.4*n)]
    tips=np.stack([poses[:,0]-poses[:,round(.1*n)],poses[:,-1]-poses[:,round(.9*n)]],axis=1)
    base=np.arctan2(middle[:,1],middle[:,0])
    angles=np.arctan2(tips[:,:,1],tips[:,:,0])-base[:,None]
    angles=np.arctan2(np.sin(angles),np.cos(angles))
    bad=(np.linalg.norm(middle,axis=1)<1e-6)|(np.linalg.norm(tips,axis=2).min(axis=1)<1e-6)
    angles[bad]=np.nan
    return angles


def appearance_fusion(results, profiles, identity_segments, mode, cfg):
    """Tierpsy-style profile matching; other segments seed orientation, never self.

    A template preserves a seed's anatomical ambiguity. It is not a pharynx model.
    Use a frozen first-pass snapshot so template decisions cannot reinforce themselves.
    """
    if profiles is None or mode!='twardis':return
    profiles=np.asarray(profiles,float)
    snapshot=[r.copy() for r in results]
    for segment in sorted({r['head_segment'] for r in snapshot if r['head_segment']>=0}):
        idx=[i for i,r in enumerate(snapshot) if r['head_segment']==segment]
        first=snapshot[idx[0]]
        if first['head_anchor_count'] or first['head_reason'] in ('shape_motion_conflict','conflicting_shape_votes'):continue
        ref=[i for i,r in enumerate(snapshot) if r['head_segment']!=segment
             and identity_segments[i]==identity_segments[idx[0]] and r['head_endpoint'] is not None
             and (r['head_identity_confirmed'] or r['head_evidence_score']>=.08)
             and np.isfinite(profiles[i]).all()]
        if len(ref)<5:continue
        template=np.median([profiles[i,::(-1 if snapshot[i]['head_endpoint']==1 else 1)] for i in ref],axis=0)
        scale=np.std(template)
        if scale<.2:continue
        template=(template-np.mean(template))/scale
        votes=[]
        for i in idx:
            v=profiles[i]
            if not np.isfinite(v).all():continue
            a=np.mean((v-template)**2);b=np.mean((v[::-1]-template)**2)
            margin=abs(a-b)/max(a+b,1e-9)
            # Very dissimilar profiles are not useful orientation evidence.
            if margin<cfg['min_appearance_margin'] or min(a,b)>.9:continue
            original_end=int(b<a)
            tracked_end=original_end ^ int(results[i]['head_input_flipped'])
            votes.append((tracked_end,margin))
        enough=len(votes)>=max(3,int(np.ceil(cfg['min_appearance_support']*len(idx))))
        winner,score=vote_winner(votes,cfg['min_vote_fraction'])
        for i in idx:
            results[i].update(head_appearance_votes=len(votes),head_appearance_reference_frames=len(ref),
                head_appearance_score=score,head_appearance_reference='manual_and_auto' if any(snapshot[j]['head_identity_confirmed'] for j in ref) else 'auto_seeded')
        if not enough or winner is None:continue
        old=first['head_endpoint']
        tracked_old=None if old is None else old ^ int(first['head_input_flipped'])
        conflict=tracked_old is not None and tracked_old!=winner
        for i in idx:
            r=results[i]
            if conflict:
                r.update(head_endpoint=None,head_status='unconfirmed_A_B_endpoints',head_reason='appearance_motion_conflict',head_evidence_score=0.)
            else:
                r.update(head_endpoint=winner ^ int(r['head_input_flipped']),head_status='unconfirmed_appearance_candidate',
                    head_reason='appearance_and_seed_evidence' if tracked_old is not None else 'appearance_temporal_reference',
                    head_evidence_score=score)


def taper_votes(profiles, indexes, flipped, cfg):
    """Compare near-tip radius relative to the local body, on arc-resampled poses.

    A sharper end has a smaller tip/body ratio. This is an engineering candidate,
    not pharynx detection. Tip pixels themselves are omitted (both approach zero).
    """
    votes=[]
    if profiles is None:return votes
    for i in indexes:
        r=profiles[i, ::-1] if flipped[i] else profiles[i]
        ratios=[]
        for v in (r,r[::-1]):
            n=len(v)-1
            tip=v[max(1,round(.02*n)):max(2,round(.12*n))+1]
            body=v[round(.20*n):round(.30*n)+1]
            if not np.isfinite(tip).all() or not np.isfinite(body).all() or np.median(body)<=0:break
            ratios.append(float(np.median(tip)/np.median(body)))
        if len(ratios)!=2:continue
        contrast=abs(ratios[0]-ratios[1])/max(sum(ratios),1e-12)
        if contrast>=cfg['min_shape_contrast']:
            votes.append((int(np.argmax(ratios)),contrast))
    return votes


def vote_winner(votes, fraction):
    if not votes:return None,0.
    counts=np.bincount([v[0] for v in votes],minlength=2)
    winner=int(np.argmax(counts));agreement=float(counts[winner]/len(votes))
    score=agreement*float(np.mean([v[1] for v in votes if v[0]==winner]))
    return (winner if agreement>=fraction else None),score


def normalize_anchors(manual, count=None):
    """Read old single-point and new multi-point records without changing the input."""
    if manual is None: return []
    values = [manual] if isinstance(manual, dict) else manual
    if not isinstance(values, list): raise ValueError('头部锚点必须为记录或记录列表')
    anchors=[];seen=set()
    for index,item in enumerate(values):
        if not isinstance(item,dict): raise ValueError('头部锚点格式错误')
        frame=item.get('frame',0);point=np.asarray(item.get('point'),float)
        if (type(frame) is not int or frame<0 or (count is not None and frame>=count)
                or point.shape!=(2,) or not np.isfinite(point).all()):
            raise ValueError('人工头部锚点需要有效帧号和两个有限坐标')
        anchor_id=item.get('id',f'legacy-{index}-f{frame}')
        if not isinstance(anchor_id,str) or not anchor_id or anchor_id in seen:
            raise ValueError('头部锚点 ID 无效或重复')
        seen.add(anchor_id);anchors.append(dict(item,id=anchor_id,frame=frame,point=point.tolist()))
    return anchors


def detect_head_tail(centerlines, valid, manual=None, mode='twardis', parameters=None, segment_ids=None, radius_profiles=None, intensity_profiles=None, fps=30.):
    """Return original endpoint index 0/1, status, evidence and segment per frame.

    manual is one {frame, point} record or a list. Identity propagates in
    both directions only within a continuous reliable track. Evidence is a
    rule-based score, not a calibrated probability of anatomical correctness.
    """
    if mode not in ('twardis', 'manual_only', 'taper_seed', 'manual_seed'):
        raise ValueError('head_detection 必须是 taper_seed/manual_seed/twardis/manual_only')
    cfg = dict(DEFAULTS, **(parameters or {}))
    window = cfg['window_frames']
    if type(window) is not int or window < 3 or window % 2 != 1:
        raise ValueError('头尾检测窗口必须为至少 3 的奇数')
    for key, value in cfg.items():
        if key != 'window_frames' and (not isinstance(value, (float, int)) or not np.isfinite(value) or not 0 < value <= 1):
            raise ValueError(f'头尾检测参数 {key} 必须在 (0,1] 内')
    p = np.asarray(centerlines, float)
    valid = np.asarray(valid, bool)
    if p.ndim != 3 or p.shape[2] != 2 or p.shape[1] < 2 or valid.shape != (len(p),):
        raise ValueError('头尾检测的中心线或质量数组尺寸错误')
    n = len(p)
    profiles=None if radius_profiles is None else np.asarray(radius_profiles,float)
    if profiles is not None and profiles.shape!=p.shape[:2]:
        raise ValueError('头尾检测的半径轮廓尺寸错误')
    if intensity_profiles is not None and np.asarray(intensity_profiles).shape!=p.shape[:2]:
        raise ValueError('头尾检测的灰度轮廓尺寸错误')
    segment_ids=np.zeros(n,int) if segment_ids is None else np.asarray(segment_ids)
    if segment_ids.shape!=(n,):raise ValueError('身份片段数组尺寸错误')
    anchors=normalize_anchors(manual,n)
    results = [dict(head_endpoint=None, head_status='unconfirmed_A_B_endpoints',
                    head_identity_confirmed=False, head_evidence_score=0.,
                    head_reason='posture_unreliable', head_segment=-1,
                    head_anchor_count=0,head_anchor_conflict=False,
                    head_shape_votes=0,head_motion_votes=0,head_input_flipped=False,
                    head_appearance_votes=0,head_appearance_reference_frames=0,
                    head_appearance_score=0.,head_appearance_reference='none') for _ in range(n)]
    lengths = np.linalg.norm(np.diff(p, axis=1), axis=2).sum(axis=1)
    finite = np.isfinite(p).all(axis=(1, 2)) & (lengths > 0)
    usable = valid & finite
    ends = p[:, [0, -1]]
    separated = np.linalg.norm(ends[:, 0]-ends[:, 1], axis=1) > cfg['close_endpoints_bl']*lengths
    for i in np.flatnonzero(usable & ~separated): results[i]['head_reason'] = 'endpoints_too_close'
    usable &= separated
    segments = []
    tracked = ends.copy()
    flipped = np.zeros(n, bool)
    current = []
    for i in range(n):
        if current and segment_ids[i]!=segment_ids[current[-1]]:
            segments.append(current);current=[]
        if not usable[i]:
            if current: segments.append(current); current = []
            continue
        if current:
            last = current[-1]
            distance = np.linalg.norm(tracked[last, :, None]-ends[i, None, :], axis=2)
            # Exact two-object assignment, equivalent to Hungarian matching.
            direct, reverse = distance.trace(), distance[0, 1]+distance[1, 0]
            flip = reverse < direct
            step = distance[[0, 1], [1, 0] if flip else [0, 1]].max()
            length = min(lengths[last], lengths[i])
            if step > cfg['max_step_bl']*length or abs(direct-reverse) < cfg['min_assignment_margin_bl']*length:
                segments.append(current); current = []
            else:
                flipped[i] = flip
                tracked[i] = ends[i, ::-1] if flip else ends[i]
        current.append(i)
    if current: segments.append(current)
    if mode in ('taper_seed','manual_seed'):
        from head_initialization import initialize
        return initialize(results,p,segments,flipped,segment_ids,profiles,anchors,cfg,fps,mode)
    for segment_id, indexes in enumerate(segments):
        idx = np.asarray(indexes)
        head = None; score = 0.; confirmed = False
        local=[a for a in anchors if a['frame'] in indexes]
        conflict=False;shape=[];votes=[]
        reason = 'automatic_detection_disabled' if mode == 'manual_only' else 'insufficient_window'
        if local:
            anchor_votes=[];invalid=False
            for anchor in local:
                f=anchor['frame'];distances=np.linalg.norm(tracked[f]-anchor['point'],axis=1)
                if distances.min()>cfg['manual_max_distance_bl']*lengths[f]:invalid=True
                else:anchor_votes.append(int(np.argmin(distances)))
            conflict=len(set(anchor_votes))>1
            if conflict:reason='manual_anchor_conflict'
            elif invalid:reason='manual_point_not_near_endpoint'
            else:
                head=anchor_votes[0];confirmed=True;reason='manual_anchor';score=1.
        # Do not silently replace a rejected manual point with an automatic one.
        elif mode == 'twardis' and len(idx) >= window:
            ordered=np.array([p[i,::-1] if flipped[i] else p[i] for i in idx])
            relative=relative_endpoint_angles(ordered)
            for start in range(len(idx)-window+1):
                delta=np.diff(relative[start:start+window],axis=0)
                movement=np.abs(np.arctan2(np.sin(delta),np.cos(delta))).sum(axis=0)
                contrast = abs(movement[0]-movement[1])/max(float(movement.sum()), 1e-12)
                if (np.isfinite(movement).all() and movement.max() >= cfg['min_motion_rad']
                        and contrast >= cfg['min_contrast']):
                    votes.append((int(np.argmax(movement)), contrast))
            reason = 'insufficient_motion_evidence'
            motion_head,score=vote_winner(votes,cfg['min_vote_fraction'])
            if motion_head is not None:head=motion_head;reason='relative_endpoint_bending'
            elif votes:reason='conflicting_motion_votes'
            shape=taper_votes(profiles,idx,flipped,cfg)
            shape_head,shape_score=vote_winner(shape,cfg['min_vote_fraction'])
            enough_shape=len(shape)>=max(window,int(np.ceil(cfg['min_shape_support']*len(idx))))
            if enough_shape:
                if shape_head is None:
                    head=None;reason='conflicting_shape_votes';score=shape_score
                elif motion_head is not None and motion_head!=shape_head:
                    head=None;reason='shape_motion_conflict';score=0.
                else:
                    head=shape_head;score=shape_score
                    reason='taper_and_motion_agree' if motion_head is not None else 'endpoint_taper'
        for i in idx:
            results[i].update(head_segment=segment_id, head_evidence_score=score, head_reason=reason,
                              head_input_flipped=bool(flipped[i]),
                              head_anchor_count=len(local),head_anchor_conflict=conflict,
                              head_shape_votes=len(shape),head_motion_votes=len(votes))
            if head is not None:
                results[i].update(head_endpoint=int(head)^int(flipped[i]),
                    head_status='confirmed_by_user_point' if confirmed else 'unconfirmed_taper_candidate' if reason in ('endpoint_taper','taper_and_motion_agree') else 'unconfirmed_relative_motion_candidate',
                    head_identity_confirmed=confirmed)
    for anchor in anchors:
        f=anchor['frame']
        if not usable[f]:results[f].update(head_reason='manual_anchor_unreliable',head_anchor_count=1)
    appearance_fusion(results,intensity_profiles,segment_ids,mode,cfg)
    from head_temporal import stabilize
    stabilize(results,p,segment_ids,fps,mode)
    return results


def endpoint_labels(row):
    if row.get('head_identity_confirmed') is True or str(row.get('head_identity_confirmed')).lower()=='true': return 'H', 'T'
    if row.get('head_detection_status',row.get('head_status')) in (*CANDIDATE_STATUSES,'candidate'): return 'H?', 'T?'
    return 'A', 'B'

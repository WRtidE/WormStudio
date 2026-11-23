"""Shared head annotations and orientation evidence, independent of orchestration."""
import numpy as np


DEFAULTS = dict(window_frames=5, min_motion_bl=.005, min_contrast=.15,
                min_vote_fraction=.65, close_endpoints_bl=.10,
                max_step_bl=.25, min_assignment_margin_bl=.02,
                manual_max_distance_bl=.15, min_shape_contrast=.15,
                min_shape_support=.60, min_motion_rad=.04,
                min_appearance_margin=.12, min_appearance_support=.60)


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


def anchors_in_crop(manual, annotation_crop, result_crop):
    """Express saved crop-local or source-frame head clicks in the result canvas."""
    records = normalize_anchors(manual)
    for record in records:
        origin = [0,0] if record.pop('point_space',None) == 'source' else annotation_crop[:2]
        record['point'] = (np.asarray(record['point']) + origin - np.asarray(result_crop[:2])).tolist()
    return records

"""Quality-gated, frame-indexed presentation of saved measurements."""
import csv
import math
import json

from result_metrics import number
from quality_schema import flag, permissions, describe


def present_frame(row):
    """The same gates are used before writing measurements and drawing video."""
    allowed=permissions(row);quality=describe(row)
    posture=allowed['posture'];mask=allowed['area'];head=allowed['head']
    confirmed = head and flag(row, 'head_identity_confirmed')
    method=row.get('head_detection_status',row.get('head_status',''))
    candidate=posture and (method in ('unconfirmed_twardis_motion_candidate','unconfirmed_taper_candidate','unconfirmed_relative_motion_candidate','unconfirmed_appearance_candidate','unconfirmed_temporal_candidate','candidate') or (head and not confirmed))
    def value(key, valid):
        return number(row.get(key)) if valid else None
    result = dict(
        worm_id=int(row['worm_id']), mask_valid=mask, posture_valid=posture,
        quality=quality,quality_schema_version=quality['version'],
        identity_valid=flag(row,'identity_valid'),identity_status=row.get('identity_status','legacy_unassessed'),
        identity_note=('旧结果：身份与标定未按新规则评估' if quality['legacy'] else
            '身份待复核；面积仅作匿名轮廓参考' if not flag(row,'identity_valid') else
            '独立复核后连续' if row.get('identity_status')=='reviewed_continuous' else '初始标注后暂定连续'),
        quality_reason=row.get('quality_reason', ''),
        head_anchor_conflict=flag(row,'head_anchor_conflict'),head_reason=row.get('head_reason',''),
        length_um=value('length_um', posture), area_um2=value('area_um2', mask),
        volume_um3=value('volume_estimate_um3', allowed['volume']),
        mean_width_um=value('mean_width_um', posture), speed_um_s=value('speed_um_s', allowed['motion']),
        head_angle_deg=value('head_candidate_angle_smoothed_deg', head),
        head_status='人工已确认 · 当前可靠片段' if confirmed else '自动候选 · 可选复核' if candidate else '无法自动判断',
        head_candidate=candidate,head_method=method,
        head_evidence_score=number(row.get('head_evidence_score')),
        head_shape_votes=number(row.get('head_shape_votes')),head_motion_votes=number(row.get('head_motion_votes')),
        head_appearance_votes=number(row.get('head_appearance_votes')),head_appearance_reference_frames=number(row.get('head_appearance_reference_frames')),
        head_appearance_reference=row.get('head_appearance_reference','none'),
        head_temporal_block=number(row.get('head_temporal_block')),head_temporal_seed_frame=number(row.get('head_temporal_seed_frame')),
        head_temporal_conflict=flag(row,'head_temporal_conflict'),head_temporal_changed=flag(row,'head_temporal_changed'),
        head_local_reason=row.get('head_local_reason',''),
        head_segment=number(row.get('head_segment')),
        head_review_allowed=posture and row.get('centerline_topology','simple')=='simple' and not flag(row,'endpoint_a_inferred') and not flag(row,'endpoint_b_inferred'),
        endpoint_labels=['H','T'] if confirmed else ['H?','T?'] if candidate else ['A','B'],
        head_confirmed=confirmed, movement_state=row.get('movement_state', 'unknown') if allowed['motion'] else 'unknown')
    result['measurement_mode'] = row.get('measurement_mode','physical')
    result.update(display_valid=flag(row,'display_valid'),display_source=row.get('display_source','legacy'),
        display_inside_fraction=number(row.get('display_inside_fraction')),display_reason=row.get('display_reason',''))
    try:
        bbox=json.loads(row.get('mask_bbox_crop_xyxy') or 'null')
        result['mask_bbox_crop_xyxy']=bbox if mask and isinstance(bbox,list) and len(bbox)==4 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in bbox) and bbox[2]>bbox[0] and bbox[3]>bbox[1] else None
    except (ValueError,TypeError):result['mask_bbox_crop_xyxy']=None
    result['identity_link_id']=row.get('identity_link_id') or None
    result['identity_link_source']=row.get('identity_link_source') or None
    result['identity_human_confirmed']=flag(row,'identity_human_confirmed')
    if row.get('identity_status')=='auto_reassociated':result['identity_note']='源图多帧匹配后自动关联；关联依据可查看，支持片段复核与撤销。'
    result['tracklet_id']=row.get('tracklet_id') or None
    result['tracklet_status']=row.get('tracklet_status','not_assessed')
    result['tracklet_pose_valid']=flag(row,'tracklet_pose_valid')
    result['tracklet_motion_valid']=flag(row,'tracklet_motion_valid')
    for name,gate in [('length_px','tracklet_pose_valid'),('mean_width_px','tracklet_pose_valid'),('speed_px_s','tracklet_motion_valid')]:
        result['tracklet_'+name]=value('tracklet_'+name,flag(row,gate))
    if not result['identity_valid'] and result['tracklet_status']=='stable_anonymous':
        result['identity_note']='后续稳定片段可作匿名局部分析；与跳变前个体的关联尚未确认。'
    if result['measurement_mode'] == 'pixels':
        for field, source, gate in [('length_px','length_px',posture),('area_px2','area_px2',mask),
                ('volume_px3','volume_estimate_px3',allowed['volume']),('mean_width_px','mean_width_px',posture),
                ('speed_px_s','speed_px_s',allowed['motion'])]:
            result[field] = value(source,gate)
    return result


def load_frame_metrics(path, fps, count):
    if not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        raise ValueError('结果缺少有效帧率')
    if not isinstance(count, int) or count <= 0:
        raise ValueError('结果缺少有效帧数')
    frames = [[] for _ in range(count)]
    raw_rows=[]
    seen, ids = set(), set()
    with path.open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            frame, worm = int(row['frame']), int(row['worm_id'])
            if not 0 <= frame < count or (frame, worm) in seen:
                raise ValueError('逐帧指标存在越界或重复记录')
            seen.add((frame, worm)); ids.add(worm)
            raw_rows.append(row)
            frames[frame].append(present_frame(row))
    if not ids or len(seen) != count * len(ids):
        raise ValueError('逐帧指标不完整；请重新运行指标阶段')
    for rows in frames:
        rows.sort(key=lambda r: r['worm_id'])
    from quality_audit import audit
    return dict(fps=fps, frame_count=count, worm_ids=sorted(ids), frames=frames,quality_audit=audit(raw_rows))

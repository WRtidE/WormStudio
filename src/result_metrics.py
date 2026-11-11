"""Present saved phenotype measurements using the user's PPT definitions."""
import csv
import json
import math


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def phenotype_results(folder):
    """Read existing outputs; never recompute or fill missing measurements."""
    path = folder/'summary.json'
    report = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    from measurement_units import metric_key, pixel_mode
    rows = report.get('worms')
    if rows is None:
        with (folder/'worm_summary.csv').open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
    fps = number(report.get('fps'))
    config = report.get('config', {})
    window = number(config.get('circling_window_s'))
    result = dict(source='线虫表型指标定义.pptx', fps=fps,
                  scale_um_per_px=number(report.get('scale_um_per_px')), measurement_mode=report.get('measurement_mode','physical'), worms=[])
    activity_labels = dict(A='A · 活跃、快速爬行', B='B · 较慢或局部活动', C='C · 无明显位移、仅头部点动',
        unknown_insufficient_valid_data='无法分类：有效数据不足', stationary_head_unconfirmed='无法区分 C 类：头尾未确认',
        stationary_without_detected_head_bends='未见明显位移，未检测到头摆')
    state_labels = dict(forward='向前', backward='向后', forward_candidate='向前候选（头尾未确认）',
        backward_candidate='向后候选（头尾未确认）', circling_candidate='转圈候选', stationary='静止',
        lateral_or_turning='侧移 / 转向', moving_head_unconfirmed='运动中，头尾未确认',
        moving_posture_uncertain='运动中，姿态不可靠', unknown='未知 / 无效')
    for row in rows:
        total = number(row.get('frames'))
        valid = number(row.get('valid_frames'))
        masks = number(row.get('mask_valid_frames'))
        head_status = row.get('head_status', '')
        identity_frames=number(row.get('identity_valid_frames'))
        identity_note=('旧结果：个体身份尚未按统一质量规则评估。' if identity_frames is None else
            f'身份连续性可用 {identity_frames:g}/{total:g} 帧；个体汇总仅使用这些帧，不跨接触和复核边界连接轨迹。')
        confirmed = head_status == 'confirmed_by_user_point'
        candidate = head_status in ('unconfirmed_taper_candidate', 'unconfirmed_wormtracer_frequency_candidate',
                                    'unconfirmed_twardis_motion_candidate','unconfirmed_relative_motion_candidate','unconfirmed_appearance_candidate','unconfirmed_temporal_candidate','mixed_head_assignments')
        head_note = ('已由人工头部点确认。' if confirmed else
                     '仅有自动头端候选，尚未确认咽部等解剖特征。' if candidate else
                     '当前仅有 A/B 端点；未确认头尾，不能据此判断头摆和向前 / 向后。')
        known=number(row.get('head_known_frames'))
        if known is not None:
            manual=number(row.get('head_confirmed_frames')) or 0
            automatic=number(row.get('head_candidate_frames')) or 0
            unknown=number(row.get('head_unknown_frames')) or 0
            head_note=f'人工确认并连续追踪 {manual:g} 帧 · 自动候选 {automatic:g} 帧 · 无法判断 {unknown:g} 帧。'
            if row.get('head_detection_method')=='head-tail-v7-initialized-orientation' and report.get('head_detection_mode') in ('taper_seed','manual_seed'):
                head_note+=' 人工头点优先；尖锐程度模式在起始清晰帧选定较钝头端候选，随后保持骨架顺序。长缺口或身份边界不强行延续。'
            head_note+=(' 自动候选比较两端渐细程度，并结合端点运动；形状不足时回退运动候选，证据冲突时保留 A/B。' if row.get('head_detection_method')=='head-tail-v4-taper-motion' else '' if row.get('head_detection_method') in ('head-tail-v5-appearance-relative-motion','head-tail-v6-temporal-block-orientation','head-tail-v7-initialized-orientation') else ' 此历史结果采用旧版头尾规则；算法细节见该运行记录。')
            if row.get('head_detection_method') in ('head-tail-v5-appearance-relative-motion','head-tail-v6-temporal-block-orientation'):head_note+=' 当前算法结合灰度外观、相对中体的端部摆动与片段内方向一致性；外观模板由已有定向片段建立，仍可能整体反向。'
            if row.get('head_detection_method')=='head-tail-v6-temporal-block-orientation':head_note+=' 短缺口经整条骨架匹配后共用固定方向种子，局部摆动分歧不会重新选头。'
            head_note+=' 画面使用 H/T 表示显示方向，是否已确认请以头尾状态为准；自动候选不等于人工确认。'
            if row.get('head_manual_requested') and not manual:
                head_note+=' 人工点未能匹配可靠端点，请在清晰帧重新标记头端。'
            reasons=row.get('head_reason_counts',{})
            labels=dict(insufficient_window='连续可靠帧不足',insufficient_motion_evidence='两端运动差异不足',
                conflicting_motion_votes='运动判断不一致',endpoints_too_close='两端过近',
                posture_unreliable='姿态不可靠',manual_anchor_unreliable='人工标注帧姿态不可靠',
                manual_point_not_near_endpoint='人工点未匹配端点',automatic_detection_disabled='未开启自动检测',
                shape_motion_conflict='形状与运动证据冲突',conflicting_shape_votes='形状投票不一致',
                endpoint_taper='两端渐细程度给出候选',taper_and_motion_agree='形状与运动候选一致')
            if isinstance(reasons,dict):
                detail='；'.join(f'{labels[k]} {v} 帧' for k,v in reasons.items() if k in labels and v)
                if detail:head_note+=' '+detail+'。'
        specs = [
            ('median_length_um','体长','μm','growth','有效中轴线长度的中位数。','没有通过质量筛选的中心线帧。'),
            ('median_area_um2','面积','μm²','growth','有效身体掩膜面积的中位数。','没有通过质量筛选的掩膜帧。'),
            ('median_volume_estimate_um3','体积（估算）','μm³','growth','按中轴分段圆台估算，假设圆形截面；不是三维实测体积。','没有可用的体积估算帧；自接触等异常姿态已排除。'),
            ('head_candidate_amplitude_deg','头摆幅度','°','motion','头颈夹角绝对值的 95 分位数。','头尾未确认。' if not (confirmed or candidate) else '没有可用的头部角度。'),
            ('directional_bends_per_min','头摆频率','次/min','motion','向一个方向弯曲记一次；单向弯曲次数 / 有效观察秒数 × 60。','头尾未确认。' if not (confirmed or candidate) else '没有足够的有效头摆观察时间。'),
            ('mean_speed_um_s','运动速度','μm/s','motion','有效连续质心轨迹的平均速度；未校正载物台漂移。','缺少连续的有效质心轨迹。')]
        metrics = []
        for key,label,unit,group,definition,missing in specs:
            key = metric_key(key,report)
            if pixel_mode(report): unit=unit.replace('μm','px')
            value = number(row.get(key))
            head_metric = key.startswith('head_candidate_') or key == 'directional_bends_per_min'
            strict = report.get('quality_schema_version') == 'wormstudio-quality-v2'
            manual_available = (number(row.get('head_confirmed_frames')) or 0) > 0 or confirmed
            if head_metric and (not (confirmed or candidate) or (strict and not manual_available)): value = None
            status = 'missing' if value is None else 'candidate' if head_metric and not confirmed and not strict else 'estimate' if 'volume' in key else 'measured'
            note = missing if value is None else '头端候选结果，头尾尚未人工确认。' if status=='candidate' else ''
            if key == 'directional_bends_per_min' and value is not None:
                count = number(row.get('directional_bend_count'))
                duration = number(row.get('observed_valid_duration_s'))
                if count is not None and duration is not None: note += f' 单向弯曲 {count:g} 次，有效观察 {duration:.2f} s。'
            if key == 'head_candidate_amplitude_deg' and value is not None:
                half = number(row.get('head_candidate_half_excursion_deg'))
                if half is not None: note += f' 补充：角度 P5–P95 半跨度 {half:.2f}°。'
            metrics.append(dict(key=key,label=label,unit=unit,group=group,value=value,status=status,definition=definition,note=note))
        counts = {key:number(value) for key,value in row.get('movement_state_counts',{}).items()} if isinstance(row.get('movement_state_counts',{}),dict) else {}
        movement = []
        for key,label in (('forward','向前'),('backward','向后'),('circling_candidate','转圈（候选）')):
            note = ''; count = None
            if not counts: note = '当前汇总缺少运动状态数据。'
            elif valid is None or valid == 0: note = '没有有效姿态，无法可靠判断。'
            elif key != 'circling_candidate' and not (confirmed or candidate): note = '待确认头尾。'
            elif key == 'circling_candidate' and (not fps or window is None): note = '缺少帧率或转圈观察窗口。'
            elif key == 'circling_candidate' and (not total or total < math.ceil(window*fps)):
                note = f'视频不足 {window:g} s 观察窗口。'
            elif key == 'circling_candidate' and (masks is None or masks < math.ceil(window*fps)):
                note = f'有效掩膜帧不足 {window:g} s 观察窗口。'
            else:
                count = counts.get(key,0) or 0
                if key != 'circling_candidate': count += counts.get(key+'_candidate',0) or 0
                note = '候选帧，需复核轨迹。' if key=='circling_candidate' else '头尾未确认，仅为候选方向。' if not confirmed else '使用人工确认的头部方向。'
            movement.append(dict(key=key,label=label,count=count,ratio=count/total if count is not None and total else None,note=note))
        state_counts = [dict(key=key,label=state_labels.get(key,key),count=value,ratio=value/total if value is not None and total else None)
                        for key,value in counts.items()]
        activity = row.get('activity_class_provisional','unknown_insufficient_valid_data')
        result['worms'].append(dict(worm_id=number(row.get('worm_id')),frames=total,valid_frames=valid,mask_valid_frames=masks,
            identity_note=identity_note,identity_valid_frames=identity_frames,
            metrics=metrics,head=dict(confirmed=confirmed,label='人工确认 + 自动候选' if head_status=='mixed_head_assignments' else
                '人工已确认' if confirmed else '外观＋相对摆动候选，待确认' if head_status in ('unconfirmed_relative_motion_candidate','unconfirmed_appearance_candidate','unconfirmed_temporal_candidate') else 'TWARDIS 候选，待确认' if head_status=='unconfirmed_twardis_motion_candidate' else
                '候选，待确认' if candidate else '未确认',note=head_note),
            activity=dict(code=activity,label=activity_labels.get(activity,'无法分类'),provisional=activity in ('A','B','C')),
            movement=movement,state_counts=state_counts))
    return result

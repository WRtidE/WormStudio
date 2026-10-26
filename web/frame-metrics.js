'use strict';
const renderFrameMetrics = (() => {
  const el = id => document.getElementById(id), video = el('result-video');
  let timeline = null, sample = '', selected = null, position = -1, request = 0, pending = null;
  const states = {stationary:'静止',forward:'向前',backward:'向后',forward_candidate:'向前候选',backward_candidate:'向后候选',lateral_or_turning:'侧移 / 转向',circling_candidate:'转圈候选',moving_posture_uncertain:'运动中，姿态不可靠',moving_head_unconfirmed:'运动中，头尾未确认',unknown:'未知 / 需复核'};
  const reasons = {crossing_without_two_reliable_anchors:'缺少自交前后的可靠姿态',ambiguous_branch_pairing:'自交走向无法唯一确定',unresolved_self_contact_or_missing_tips:'自接触或端点不可见，无法恢复',centerline_missing:'无可靠中线',centerline_geometry_review:'中线几何需复核',length_outlier:'体长异常',centerline_temporal_jump:'中线发生不连续跳变',tracking_contact_or_area_outlier:'掩膜接触或面积异常'};
  Object.assign(reasons,{identity_unverified:'接触或跳变后，个体身份尚未独立确认',inter_worm_contact:'虫间接触',invalid_decoded_source_frame:'源帧已排除',fragmented_mask:'掩膜碎裂',image_boundary_truncation:'虫体越过画面边界',centroid_jump:'质心异常跳变',missing_mask:'缺少掩膜',sam2_tracking_quality:'跟踪质量未通过',physical_scale_not_supplied:'已选择像素单位，不输出物理尺寸', scale_not_verified:'尺度估计尚未确认'});
  const dimensions={source:'源帧',mask:'掩膜',pose:'姿态',identity:'个体身份',head:'头尾',scale:'尺度'};
  const statuses={not_excluded:'未标记异常',excluded:'已排除',passed:'通过',rejected:'未通过',provisional_continuous:'暂定连续',reviewed_continuous:'独立复核后连续',contact_ambiguous:'虫间接触',post_contact_or_jump_unverified:'接触或跳变后待复核',jump_review:'跳变待复核',confirmed:'人工确认',candidate:'自动候选',unknown:'未知',supplied:'已提供标定',pixels:'未标定 · 像素模式', unverified_estimate:'估计待确认',legacy_unassessed:'旧结果未评估',missing:'未记录'};
  statuses.source_frame_excluded='源帧无效，本帧身份不作判断';
  const values = [['length_um','体长','μm'],['area_um2','面积','μm²'],['volume_um3','体积（估算）','μm³'],['mean_width_um','平均体宽','μm'],['speed_um_s','质心速度','μm/s'],['head_angle_deg','头部弯曲角','°']];
  function draw(frame, force=false) {
    if (!timeline) return;
    frame = Math.max(0, Math.min(timeline.frame_count-1, frame));
    if (frame === position && !force) return;
    position = frame;
    el('metric-frame').value = el('metric-frame-number').value = frame;
    el('frame-position').textContent = `第 ${frame} / ${timeline.frame_count-1} 帧 · ${(frame/timeline.fps).toFixed(3)} s`;
    el('frame-prev').disabled = frame === 0; el('frame-next').disabled = frame === timeline.frame_count-1;
    const row = timeline.frames[frame].find(r => r.worm_id === selected);
    el('frame-cards').replaceChildren();
    if (!row) { el('frame-quality').textContent = '此帧缺少记录，请重新运行指标阶段。'; return; }
    const good = row.mask_valid && row.posture_valid && row.identity_valid && !row.quality?.legacy;
    el('frame-quality').classList.toggle('needs-review', !good);
    el('frame-quality').textContent = `${good?'通过当前质量筛选':'需复核：部分指标不可用'} · ${row.identity_note} · ${row.head_status} · ${states[row.movement_state] || row.movement_state}${!good && row.quality_reason?' · '+row.quality_reason.split(';').map(r=>reasons[r]||r).join('；'):''}`;
    el('quality-dimensions').replaceChildren();
    for(const [key,label] of Object.entries(dimensions)){
      const q=row.quality?.dimensions[key];if(!q)continue;
      const item=document.createElement('li'),title=document.createElement('strong'),detail=document.createElement('span');
      title.textContent=`${label}：${statuses[q.status]||q.status}`;
      detail.textContent=[q.reason.split(';').map(r=>reasons[r]||r).filter(Boolean).join('；'),q.evidence].filter(Boolean).join(' · ');
      item.append(title,detail);el('quality-dimensions').append(item);
    }
    for (const [originalKey, title, originalUnit] of values) {
      const pixel=row.measurement_mode==='pixels',key=pixel?originalKey.replace('_um','_px'):originalKey,unit=pixel?originalUnit.replace('μm','px'):originalUnit;
      const card = document.createElement('article'); card.className = 'frame-metric'+(row[key] === null?' missing':'');
      const label = document.createElement('span'); label.textContent = key==='area_um2'&&!row.identity_valid&&!row.quality?.legacy?'匿名轮廓面积':title;
      const value = document.createElement('strong'); value.textContent = row[key] === null ? '—' : Number(row[key]).toLocaleString('zh-CN',{maximumFractionDigits:2});
      const suffix = document.createElement('small'); suffix.textContent = unit;
      card.append(label, value, suffix); el('frame-cards').append(card);
    }
  }
  function fromTime(time) {
    if (timeline && pending === null) draw(Math.floor(time*timeline.fps+1e-4));
  }
  function seek(frame) {
    if (!timeline || !Number.isInteger(frame) || frame<0 || frame>=timeline.frame_count) return;
    video.pause(); pending = frame;
    // Seek inside the frame's presentation interval, away from rounding boundaries.
    video.currentTime = (frame+.25)/timeline.fps;
    draw(frame);
  }
  video.addEventListener('timeupdate', () => { if(!video.paused)fromTime(video.currentTime); });
  video.addEventListener('seeked', () => { const frame=pending; pending=null; if(frame!==null)draw(frame);else if(timeline)draw(Math.floor(video.currentTime*timeline.fps+1e-4)); });
  video.addEventListener('play', () => { pending = null; });
  if ('requestVideoFrameCallback' in video) {
    const tick = (_, metadata) => { if(!video.paused)fromTime(metadata.mediaTime); video.requestVideoFrameCallback(tick); };
    video.requestVideoFrameCallback(tick);
  }
  el('metric-frame').oninput = e => seek(Number(e.target.value));
  el('metric-frame-number').oninput = e => { const n=Number(e.target.value);if(e.target.value!=='' && Number.isInteger(n) && timeline && n>=0 && n<timeline.frame_count)seek(n); };
  el('metric-frame-number').onchange = e => { if(timeline && (e.target.value==='' || Number(e.target.value)<0 || Number(e.target.value)>=timeline.frame_count))e.target.value=position; };
  el('frame-prev').onclick = () => seek(position-1); el('frame-next').onclick = () => seek(position+1);
  el('frame-worm').onchange = e => { selected=Number(e.target.value);draw(position,true); };
  return async data => {
    const ready = data.artifacts.some(a => a.key === 'metrics');
    if (!ready) { timeline=null;sample='';request++;el('frame-inspector').hidden=true;return; }
    if (sample===data.sample) return;
    sample=data.sample;timeline=null;position=-1;pending=null;const version=++request;
    el('frame-inspector').hidden=true;el('frame-metrics-error').hidden=true;
    try {
      const response=await fetch('/api/frame-metrics?run='+encodeURIComponent(sample),{cache:'no-store'});
      const result=await response.json();if(!response.ok)throw Error(result.error || '逐帧指标不可用');
      if(version!==request)return;
      timeline=result;selected=result.worm_ids[0];el('frame-worm').replaceChildren();
      for(const id of result.worm_ids){const option=document.createElement('option');option.value=id;option.textContent='W'+id;el('frame-worm').append(option);}
      el('metric-frame').max=el('metric-frame-number').max=result.frame_count-1;
      el('frame-inspector').hidden=false;draw(Math.floor(video.currentTime*result.fps+1e-4),true);
    } catch(error) { if(version===request){el('frame-metrics-error').textContent=error.message;el('frame-metrics-error').hidden=false;sample='';} }
  };
})();

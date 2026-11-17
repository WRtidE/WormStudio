'use strict';
// Workspace presentation only: all measurement gates come from /api/frame-metrics.
window.StudioResults = (() => {
  const el = id => document.getElementById(id);
  const video = el('studio-result-video');
  const showEvents = window.createResultEventTrack(el('result-event-track'), el('result-event-legend'), el('result-event-picker'), frame => {
    seek(frame);
    el('result-canvas').focus({preventScroll:true});
  });
  const dimensions = {source:'源帧', mask:'掩膜', pose:'姿态', identity:'个体身份', head:'头尾', scale:'尺度'};
  const statuses = {not_excluded:'未标记异常', excluded:'已排除', passed:'通过', rejected:'未通过',
    provisional_continuous:'暂定连续', reviewed_continuous:'复核后连续', contact_ambiguous:'虫间接触',
    post_contact_or_jump_unverified:'身份待复核', jump_review:'跳变待复核', source_frame_excluded:'本帧不作判断',
    confirmed:'人工确认', candidate:'自动候选', unknown:'未确认', supplied:'已提供标定',
    pixels:'未标定 · 像素模式', unverified_estimate:'估计待确认', legacy_unassessed:'旧结果未评估', missing:'未记录'};
  statuses.auto_reassociated='源图匹配后自动关联';statuses.fragment_review_required='片段关联待复核';
  const reasons = {identity_unverified:'接触或跳变后，尚未独立确认个体身份', inter_worm_contact:'虫间接触',
    invalid_decoded_source_frame:'源帧已排除', fragmented_mask:'掩膜碎裂', image_boundary_truncation:'虫体越过画面边界',
    centroid_jump:'质心异常跳变', missing_mask:'缺少掩膜', sam2_tracking_quality:'跟踪质量未通过',
    physical_scale_not_supplied:'已选择像素单位，不输出物理尺寸', scale_not_verified:'尺度估计尚未确认', continuity_is_provisional:'初始标注后暂定连续，不等同于独立身份确认',
    not_recorded:'此项没有独立记录', centerline_missing:'缺少可靠中线', centerline_geometry_review:'中线几何需复核',
    length_outlier:'体长异常', centerline_temporal_jump:'中线不连续', tracking_contact_or_area_outlier:'接触或面积异常',
    crossing_without_two_reliable_anchors:'缺少自交前后的可靠姿态', ambiguous_branch_pairing:'自交走向无法唯一确定',
    unresolved_self_contact_or_missing_tips:'自接触或端点不可见'};
  const metrics = [['length_um','体长','μm'],['area_um2','面积','μm²'],['volume_um3','体积估算','μm³'],
    ['mean_width_um','平均体宽','μm'],['body_extension','身体伸展度',''],
    ['length_width_ratio','长宽比',''],['speed_bl_s','体长归一化速度','体长/s'],
    ['midbody_abs_curvature_per_um','中段绝对曲率','μm⁻¹'],['speed_um_s','质心速度','μm/s'],['head_angle_deg','头部弯曲角','°']];
  let latest = null, key = '', timeline = null, worm = null, position = 0, pending = null;
  let controller = null, revision = 0, ranges = [], visible = false, headAnchors = {};
  let identityReport={edges:[]},identityEdge=null,identityTimer=null;
  const controls = ['result-frame','result-frame-number','result-prev','result-next','result-worm','result-issue-filter','result-prev-issue','result-next-issue'];
  function text(id, value) { el(id).textContent = AnalysisPresentation.text(value); }
  const rowAt = frame => timeline?.frames[frame]?.find(row => row.worm_id === worm);
  const translate = value => String(value || '').split(';').map(reason => reasons[reason] || reason).filter(Boolean).join('；');
  const needsReview = (row, dimension) => dimension==='scale' && row?.measurement_mode==='pixels' ? false : !row || (dimension === 'all'
    ? Object.keys(dimensions).some(name => needsReview(row, name))
    : !row.quality?.dimensions[dimension]?.valid || ['legacy_unassessed','missing','candidate','unknown'].includes(row.quality?.dimensions[dimension]?.status));

  function issueRanges() {
    ranges = []; let count = 0;
    const filter = el('result-issue-filter').value;
    for (let frame = 0; frame < timeline.frame_count; frame++) {
      if (!needsReview(rowAt(frame), filter)) continue;
      count++;
      const last = ranges.at(-1);
      if (last && last.end === frame - 1) last.end = frame;
      else ranges.push({start:frame, end:frame});
    }
    text('result-issue-count', `${count} / ${timeline.frame_count} 帧`);
    text('result-issue-note', ranges.length ? `${ranges.length} 个连续问题段 · ${filter === 'all' ? '含头尾候选与未评估项' : dimensions[filter]}` : '当前筛选未发现问题帧。');
    const track = el('result-quality-track'); track.replaceChildren();
    showEvents(timeline, worm, ranges);
    for (const range of ranges) {
      const span = document.createElement('span');
      span.style.left = `${range.start / timeline.frame_count * 100}%`;
      span.style.width = `${(range.end - range.start + 1) / timeline.frame_count * 100}%`;
      track.append(span);
    }
  }
  function draw(frame, force = false) {
    if (!timeline) return;
    frame = Math.max(0, Math.min(timeline.frame_count - 1, frame));
    if (frame === position && !force) return;
    position = frame;
    el('result-frame-error').hidden=true;el('result-frame-number').removeAttribute('aria-invalid');
    el('result-frame').value = el('result-frame-number').value = frame;
    text('result-frame-label', `第 ${frame} / ${timeline.frame_count - 1} 帧 · ${(frame / timeline.fps).toFixed(3)} s`);
    el('result-prev').disabled = frame === 0;
    el('result-next').disabled = frame === timeline.frame_count - 1;
    el('result-prev-issue').disabled = !ranges.some(range => range.start < frame);
    el('result-next-issue').disabled = !ranges.some(range => range.start > frame);
    const row = rowAt(frame);
    if (!row) return;
    window.ResultPlayer.frame(row);
    el('head-a').disabled=el('head-b').disabled=!row.head_review_allowed;
    const contactDirection=row.head_reason==='two_sided_contact_orientation';
    const labels=row.endpoint_labels || ['A','B'];
    const colors = row.self_contact_state ? (row.head_confirmed || row.head_candidate ? ['红色','蓝色'] : ['灰色','灰色']) : ['蓝色','橙色'];
    const palette = row.self_contact_state ? (row.head_confirmed || row.head_candidate ? ['#f64c2a','#00a1e1'] : ['#aaa','#aaa']) : [];
    for (const [i,id] of ['head-a','head-b'].entries()) el(id).style.setProperty('--endpoint-color',palette[i] || '');
    el('head-a').textContent=row.head_confirmed?'保持 H 端为头':`选择 ${labels[0]} 端为头`;
    el('head-b').textContent=row.head_confirmed?'纠正：T 端才是头':`选择 ${labels[1]} 端为头`;
    text('result-head-status',row.head_status);
    const headReasons={tracked_endpoint_order:'沿同一条虫的中线匹配传递定向',endpoint_order_unanchored:'当前片段无法接回已知头端，可在清晰帧重新点选',endpoint_anchor_conflict:'头部锚点与几何对应有冲突，需复核',taper_seed_initialization:'起始清晰帧按尖锐程度初始化，此后保持骨架点序',initial_taper_insufficient:'起始清晰帧的尖锐程度差异不足',initial_taper_conflict:'起始清晰帧的形状判断不一致',manual_seed_propagated:'沿短缺口骨架关联继承人工种子方向，仍为未确认候选',temporal_orientation_conflict:'局部方向与连续块分歧，按骨架关联保留端点身份',temporal_orientation_reference:'短缺口两侧骨架匹配，沿连续块方向跟踪',relative_endpoint_bending:'端部相对中体的摆动差异',appearance_and_seed_evidence:'灰度外观与片段初始定向一致',appearance_temporal_reference:'同身份段的灰度外观时序匹配',appearance_motion_conflict:'外观与初始定向冲突',endpoint_taper:'两端渐细程度的连续帧投票',taper_and_motion_agree:'两端形状与运动投票一致',twardis_endpoint_motion:'两端运动差异',shape_motion_conflict:'形状与运动证据冲突',conflicting_shape_votes:'形状投票不一致',insufficient_window:'连续可靠帧不足',insufficient_motion_evidence:'两端差异不足',conflicting_motion_votes:'运动投票不一致',posture_unreliable:'当前姿态不可靠',endpoints_too_close:'两端过近',manual_anchor:'当前连续片段已有人工锚点',manual_anchor_conflict:'同一片段锚点冲突',manual_point_not_near_endpoint:'锚点未匹配可靠端点',automatic_detection_disabled:'自动检测已关闭',manual_anchor_unreliable:'锚点所在帧姿态不可靠'};
    headReasons.trajectory_block_orientation='用相邻片段的端部轨迹接回头尾顺序';
    headReasons.trajectory_frame_orientation='用邻近清晰帧的端部轨迹推定这一帧的方向';
    headReasons.centerline_overlap_orientation='根据两帧重合虫身的位置和切线方向接回头尾';
    headReasons.endpoint_continuity_conflict='前后轨迹给出的头端与虫身对应关系冲突，头尾待复核';
    text('result-head-explanation',row.head_confirmed?'H 为已确认头，T 为尾。此连续片段无需重复确认；发现错误时可展开纠正或撤销锚点。':row.head_candidate?'H? / T? 为候选头尾。':'A / B 表示当前片段内的两个端点，头尾方向待确认。');
    Object.assign(headReasons,{initial_mark:'结合前后帧身体运动和绕柱顺序保持初始标记端',auto_taper_seed:'端部外形初始化后持续跟踪，仍为候选',short_gap_body_candidate:'短缺口后多个身体对应支持接回同一端',multi_frame_appearance_candidate:'多帧端部外观支持认回初始端',unresolved:'身体运动和外观证据不足',no_supported_body_path:'没有通过检查的完整身体路径'});
    text('result-head-evidence',`依据：${headReasons[row.head_reason]||row.head_reason||'旧结果未记录'}${row.head_candidate&&Number.isFinite(row.head_evidence_score)?` · 规则分 ${row.head_evidence_score.toFixed(2)}（非准确率）`:''}。正式头摆指标使用人工确认的可靠片段。`);
    if(row.head_candidate&&Number.isFinite(row.head_shape_votes))el('result-head-evidence').textContent+=` 本片段形状有效票 ${row.head_shape_votes}，运动窗口票 ${row.head_motion_votes}。`;
    if(row.head_trajectory_frames?.length)el('result-head-evidence').textContent+=` 轨迹参考帧 ${row.head_trajectory_frames.join('、')}。`;
    if(Number.isFinite(row.head_temporal_seed_frame)) el('result-head-evidence').textContent+=` 连续方向块 ${row.head_temporal_block}，固定种子第 ${row.head_temporal_seed_frame} 帧${row.head_temporal_conflict?'；局部证据有分歧，保留候选待复核':''}。`;
    if(Number.isFinite(row.head_appearance_votes)) el('result-head-evidence').textContent+=` 外观有效票 ${row.head_appearance_votes}，其他片段参考帧 ${row.head_appearance_reference_frames}；模板来源 ${row.head_appearance_reference==='auto_seeded'?'自动候选，仍待验证':row.head_appearance_reference==='manual_and_auto'?'含人工锚点':'无'}。`;
    const displayLabels={formal:'可靠姿态',geometry_review:'待复核 · 身份待复核的几何',diagnostic_candidate:'待复核 · 诊断候选',mask_refit:'待复核 · 当前掩膜重新拟合',temporal_mask_review:'待复核 · 由相邻姿态和本帧掩膜拟合',unavailable:'没有可显示的可靠几何'};
    if(row.contact_bridge||contactDirection){
      text('result-head-explanation',row.head_candidate?'H? / T? 表示从清晰帧继承的候选头尾方向。':'A / B 表示尚未定向的两个端点。');
      const b=row.contact_bridge;
      const notes=b?[`接触前后参照帧 ${b.anchors.join('、')}`,
        b.measured?'两端可见，双向路径与原求解器吻合':b.path_supported?'双向路径吻合，保留为重建候选':'前后两侧对中线路径仍有分歧']:[];
      if(contactDirection)notes.push(`头尾方向来自第 ${row.head_temporal_seed_frame} 帧`);
      text('result-head-evidence',notes.join('。')+'。');
    }
    if(row.endpoints_unlinked)text('result-head-explanation','当前帧无法关联端点。灰色圆点表示两端位置，头尾顺序待定。');
    if(row.display_source!=='legacy')el('result-head-explanation').textContent+=` 骨架：${displayLabels[row.display_source]||row.display_source}。`;
    text('head-choice-note',row.head_review_allowed?`${colors[0]} ${labels[0]} / ${colors[1]} ${labels[1]}，按钮颜色与画面对应。请选择实际头端，确认后应用于当前片段。`:'本帧端点或身份不可靠，请切换至清晰、可靠的姿态帧。');
    el('head-choice-note').textContent+=el('result-original').checked?' 当前为原始画面，请先取消“原始画面”查看端点标记。':'';
    if(el('result-original').checked)el('head-a').disabled=el('head-b').disabled=true;
    if(row.head_anchor_conflict) text('head-review-message','本片段头部锚点冲突，正式头摆已暂停。请在锚点列表核对并撤销错误记录。');
    else text('head-review-message',row.head_reason==='manual_point_not_near_endpoint'?'锚点远离可靠端点，请核对原记录。':'');
    const audit=timeline.quality_audit?.worms?.[String(worm)];
    text('result-gate-audit',audit?`共 ${audit.frames} 帧；掩膜 ${audit.mask_valid}；几何姿态 ${audit.geometric_pose_valid}；正式姿态 ${audit.formal_posture_valid}。其中 ${audit.identity_blocked_geometry} 帧几何可用但身份待复核。此处是规则接受数，不是准确率。`:'旧结果缺少分项记录。');
    el('result-gate-triggers').replaceChildren();
    for(const trigger of audit?.triggers||[]){const b=document.createElement('button');b.className='small-button';b.textContent=`定位第 ${trigger.frame} 帧：${statuses[trigger.status]||trigger.status}`;b.onclick=()=>seek(trigger.frame);el('result-gate-triggers').append(b);}
    renderAnchors();
    el('identity-open').disabled=!(identityReport.edges||[]).some(e=>e.worm_id===worm&&e.reference_end_frame!==null);
    el('identity-open').hidden=el('identity-open').disabled;
    if(audit?.tracklet_pose_valid!==undefined) el('result-gate-audit').textContent+=` 匿名片段姿态 ${audit.tracklet_pose_valid} 帧，额外提供 ${audit.tracklet_additional_pose} 帧局部结果；不合并为已确认个体。`;
    if(audit?.identity_auto_reassociated!==undefined) el('result-gate-audit').textContent+=` 自动关联 ${audit.identity_auto_reassociated} 帧；人工确认 ${audit.identity_human_confirmed} 帧。`;
    const legacy = row.quality?.legacy;
    const identity = row.quality?.dimensions.identity;
    const unverified = !row.identity_valid || legacy;
    const excluded = row.quality?.dimensions.source?.valid === false;
    text('result-quality-badge', legacy ? '旧版结果' : needsReview(row, 'all') ? '有待复核项' : '通过当前筛选');
    el('result-identity').classList.toggle('needs-review', unverified);
    text('result-identity-title', legacy ? '身份尚未评估' : excluded ? '源帧无效，身份不作判断' : unverified ? (row.tracklet_status==='stable_anonymous'?'局部片段可用 · 跨段身份未确认':'身份待复核') : statuses[identity?.status] || '身份状态已记录');
    text('result-identity-note', legacy ? '此结果未按新质量规则评估。请重新运行后再作质量判断。' : excluded ? '本帧不用于测量；已有的身份复核状态继续保留。' : row.identity_note);
    text('result-metric-note', legacy ? '旧版指标 · 质量尚未重新评估' : unverified ? '个体指标暂停 · 可用面积仅作匿名参考' : '仅显示通过质量规则的测量值');
    el('studio-metric-cards').replaceChildren();el('studio-extra-metrics').replaceChildren();
    for (const [metric, label, unit] of metrics) {
      const pixel = row.measurement_mode === 'pixels';
      const value = row[pixel ? metric.replace('_um','_px') : metric], available = typeof value === 'number' && Number.isFinite(value);
      const card = document.createElement('article'); card.className = 'studio-metric' + (available ? '' : ' missing');
      const name = document.createElement('span');
      name.textContent = metric === 'area_um2' && unverified && !legacy ? '匿名轮廓面积' : label;
      const number = document.createElement('strong'); number.textContent = available ? value.toLocaleString('zh-CN', {maximumFractionDigits:metric==='midbody_abs_curvature_per_um'?6:2}) : '—';
      const suffix = document.createElement('small'); suffix.textContent = pixel ? unit.replace('μm','px') : unit;
      card.append(name, number, suffix); el(['volume_um3','mean_width_um','body_extension','midbody_abs_curvature_per_um','length_width_ratio','speed_bl_s'].includes(metric)?'studio-extra-metrics':'studio-metric-cards').append(card);
    }
    if(row.tracklet_status==='stable_anonymous') {
      for(const [field,label,unit] of [['tracklet_length_px','片段局部体长','px'],['tracklet_speed_px_s','片段局部速度','px/s']]) {
        const card=document.createElement('article');card.className='studio-metric';
        const labelNode=document.createElement('span'),valueNode=document.createElement('strong'),note=document.createElement('small');
        labelNode.textContent=label;valueNode.textContent=Number.isFinite(row[field])?row[field].toFixed(2):'—';
        note.textContent=`${unit} · ${row.tracklet_id} · 匿名`;card.append(labelNode,valueNode,note);el('studio-extra-metrics').append(card);
      }
    }
    // Keep expanded evidence open while stepping through the same worm.
    const open = new Set([...el('result-dimensions').querySelectorAll('details[open]')].map(item => item.dataset.dimension));
    el('result-dimensions').replaceChildren();
    for (const [name, label] of Object.entries(dimensions)) {
      const q = row.quality?.dimensions[name];
      const details = document.createElement('details'); details.dataset.dimension = name;
      details.open = open.has(name); details.className = needsReview(row, name) ? 'needs-review' : '';
      const summary = document.createElement('summary'), title = document.createElement('span'), state = document.createElement('b');
      title.textContent = label; state.textContent = statuses[q?.status] || q?.status || '未记录';
      summary.append(title, state);
      const note = document.createElement('p'); note.textContent = translate(q?.reason) || '本项通过当前规则。';
      const evidence = document.createElement('pre');
      try { evidence.textContent = AnalysisPresentation.text(JSON.stringify(JSON.parse(q?.evidence), null, 2)); }
      catch { evidence.textContent = AnalysisPresentation.text(q?.evidence || '没有记录证据。'); }
      details.append(summary, note, evidence); el('result-dimensions').append(details);
    }
  }
  function artifactUrl(data, key, inline=false) {
    const query = new URLSearchParams({key, run:data.sample});
    if (inline) query.set('inline', '1');
    if (data.summaryOnly) query.set('group', data.group || '');
    return '/artifact?' + query;
  }
  el('result-original').onchange=()=>{if(!latest||!timeline)return;const artifact=latest.artifacts.find(a=>a.key===(el('result-original').checked?'source-video':'video'));if(!artifact){el('result-original').checked=false;return;}video.pause();pending=position;window.ResultPlayer.source(el('result-original').checked);video.src=artifactUrl(latest,artifact.key,true);video.load();draw(position,true);};
  for(const [id,endpoint] of [['head-a',0],['head-b',1]]) el(id).onclick=async()=>{
    el('head-a').disabled=el('head-b').disabled=true;
    try{const state=await(await fetch('/api/progress')).json();const r=await fetch('/api/confirm-head',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':state.token},body:JSON.stringify({sample:latest.sample,worm_id:worm,frame:position,endpoint})});const d=await r.json();if(!r.ok)throw Error(d.error);location.reload();}
    catch(e){text('head-review-message',e.message);draw(position,true);}
  };
  el('head-first').onclick=()=>{el('result-video-details').open=true;el('result-video-details').scrollIntoView({block:'start',behavior:'smooth'});el('result-original').checked=false;el('result-original').dispatchEvent(new Event('change'));seek(0,true);el('head-correction').open=true;};
  el('head-auto').onclick=async()=>{
    el('head-auto').disabled=true;text('head-auto-message','正在另存结果并计算候选…');
    try{const state=await(await fetch('/api/progress')).json();const response=await fetch('/api/auto-head',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':state.token},body:JSON.stringify({sample:latest.sample,head_mode:'taper_seed'})});const data=await response.json();if(!response.ok)throw Error(data.error);location.reload();}
    catch(e){text('head-auto-message',e.message);el('head-auto').disabled=false;}
  };
  function renderAnchors(){
    const target=el('head-anchor-list');target.replaceChildren();
    const anchors=headAnchors[String(worm)]||[];
    if(!anchors.length){target.textContent='尚无人工头部锚点。';return;}
    for(const anchor of anchors){
      const row=document.createElement('div'),jump=document.createElement('button'),remove=document.createElement('button'),note=document.createElement('p');
      jump.className=remove.className='small-button';jump.textContent=`第 ${anchor.frame} 帧`;jump.onclick=()=>seek(anchor.frame);
      note.textContent=`${anchor.id} · ${anchor.source||'旧版人工点'} · ${anchor.confirmed_at||'历史记录未提供时间'}`;
      remove.textContent='撤销此锚点';remove.onclick=async()=>{remove.disabled=true;try{
        const state=await(await fetch('/api/progress')).json();const response=await fetch('/api/remove-head',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':state.token},body:JSON.stringify({sample:latest.sample,worm_id:worm,anchor_id:anchor.id})});
        const result=await response.json();if(!response.ok)throw Error(result.error);location.reload();
      }catch(e){text('head-review-message',e.message);remove.disabled=false;}};
      row.append(jump,remove,note);target.append(row);
    }
  }
  function identityPair(){
    if(!identityEdge)return;
    const offset=Number(el('identity-offset').value),e=identityEdge;
    const before=Math.min(e.reference_end_frame,Math.max(e.reference_start_frame,e.reference_end_frame-3)+offset);
    const after=Math.min(e.end_frame,e.start_frame+offset);
    for(const [side,f] of [['before',before],['after',after]]) {
      text(`identity-${side}-label`,`${side==='before'?'关联前':'关联后'} · 第 ${f} 帧`);
      el(`identity-${side}-image`).src=`/api/identity-frame?run=${encodeURIComponent(latest.sample)}&worm=${e.worm_id}&frame=${f}`;
    }
  }
  function identitySelect(){
    identityEdge=identityReport.edges.find(e=>e.id===el('identity-edge').value);if(!identityEdge)return;
    const e=identityEdge,labels={seed_continuous:'初始标注后暂定连续',auto_reassociated:'源图多帧匹配通过，自动关联',ambiguous:'图像证据存在歧义',unresolved:'尚未关联',human_same:'人工确认同一条虫',human_different:'人工确认不同目标',human_unknown:'人工记录无法判断',stale_review:'前序关联已变化，此复核需重新核对'};
    text('identity-edge-state',`${labels[e.decision]||e.decision} · 第 ${e.start_frame}–${e.end_frame} 帧 · ${e.reason}`);
    text('identity-evidence',JSON.stringify({reference:[e.reference_start_frame,e.reference_end_frame],decision:e.decision,reason:e.reason,checks:e.evidence?.checks,image_evidence:e.evidence,review:e.review,rules:identityReport.rules},null,2));
    el('identity-undo').disabled=!e.review;el('identity-offset').value=0;
    el('identity-reviewer').value=e.review?.reviewer||'';el('identity-review-evidence').value=e.review?.evidence||'';
    text('identity-message','');identityPair();
  }
  el('identity-open').onclick=()=>{
    el('identity-edge').replaceChildren();
    for(const e of identityReport.edges.filter(e=>e.worm_id===worm&&e.reference_end_frame!==null)) {
      const option=document.createElement('option');option.value=e.id;option.textContent=`第 ${e.start_frame}–${e.end_frame} 帧 · ${e.accepted?'已关联':'待关联'}`;el('identity-edge').append(option);
    }
    const current=identityReport.edges.find(e=>e.worm_id===worm&&e.start_frame<=position&&e.end_frame>=position&&e.reference_end_frame!==null);
    if(current)el('identity-edge').value=current.id;identitySelect();el('identity-dialog').showModal();
  };
  el('identity-edge').onchange=identitySelect;el('identity-offset').oninput=identityPair;
  const stopIdentity=()=>{clearInterval(identityTimer);identityTimer=null;text('identity-play','同步播放');};
  el('identity-close').onclick=()=>el('identity-dialog').close();el('identity-dialog').onclose=stopIdentity;
  el('identity-play').onclick=()=>{if(identityTimer){stopIdentity();return;}text('identity-play','暂停');identityTimer=setInterval(()=>{el('identity-offset').value=(Number(el('identity-offset').value)+1)%4;identityPair();},450);};
  async function identitySubmit(decision){
    if(!identityEdge)return;
    const reviewer=el('identity-reviewer').value.trim(),evidence=el('identity-review-evidence').value.trim();
    if(decision!=='undo'&&(!reviewer||!evidence)){text('identity-message','请填写复核人和判断依据。');return;}
    stopIdentity();
    try {
      const state=await(await fetch('/api/progress')).json();
      const body=decision==='undo'?{sample:latest.sample,review_id:identityEdge.review.id}:{sample:latest.sample,link_id:identityEdge.id,decision,reviewer,evidence};
      const response=await fetch(decision==='undo'?'/api/remove-identity-review':'/api/review-identity',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':state.token},body:JSON.stringify(body)});
      const result=await response.json();if(!response.ok)throw Error(result.error);location.reload();
    }catch(error){text('identity-message',error.message);}
  }
  for(const decision of ['same','different','unknown'])el('identity-'+decision).onclick=()=>identitySubmit(decision);
  el('identity-undo').onclick=()=>identitySubmit('undo');
  function seek(frame, pause = true) {
    if (!timeline || !Number.isInteger(frame) || frame < 0 || frame >= timeline.frame_count) return;
    if (pause) video.pause();
    pending = frame;
    if (video.hasAttribute('src') && video.readyState >= 1) video.currentTime = (frame + .25) / timeline.fps;
    draw(frame, true);
  }
  function sync(time) { if (timeline && pending === null) draw(Math.floor(time * timeline.fps + 1e-4)); }
  document.addEventListener('keydown', event => {
    const panel = el('result-video-details'), target = event.target;
    if (!visible || !panel.open || !video.hasAttribute('src') || video.error || event.defaultPrevented ||
        event.ctrlKey || event.altKey || event.metaKey || event.shiftKey || event.isComposing ||
        document.querySelector('dialog[open]')) return;
    if (target !== document.body && !panel.contains(target)) return;
    if (target.isContentEditable || (target.closest('input,textarea,select,button,a,summary') &&
        target !== el('result-frame') && target !== el('result-play'))) return;
    if (event.key === ' ') {
      event.preventDefault();
      if (!event.repeat) el('result-play').click();
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      const seconds = event.key === 'ArrowLeft' ? -5 : 5;
      if (timeline) {
        const from = pending ?? Math.floor(video.currentTime * timeline.fps + 1e-4);
        seek(Math.max(0, Math.min(timeline.frame_count - 1, from + Math.round(seconds * timeline.fps))), false);
      } else if (Number.isFinite(video.duration)) {
        video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + seconds));
      }
    }
  });
  video.addEventListener('timeupdate', () => { if (!video.paused) sync(video.currentTime); });
  video.addEventListener('seeked', () => { const target = pending; pending = null; if (target !== null) draw(target, true); else sync(video.currentTime); });
  video.addEventListener('play', () => { pending = null; });
  video.addEventListener('loadedmetadata', () => { if (pending !== null && timeline) video.currentTime = (pending + .25) / timeline.fps; });
  if ('requestVideoFrameCallback' in video) {
    const tick = (_, metadata) => { if (!video.paused) sync(metadata.mediaTime); video.requestVideoFrameCallback(tick); };
    video.requestVideoFrameCallback(tick);
  }
  video.addEventListener('error', () => {
    el('result-video-message').hidden = false;
    text('result-video-message', '视频暂时无法播放。仍可用下方帧控件查看质量和指标，或导出视频到本机播放。');
  });
  el('result-frame').oninput = event => seek(Number(event.target.value));
  el('result-frame-number').oninput = event => {
    const value=event.target.value, frame=Number(value), max=timeline?.frame_count-1;
    const invalid=value==='' || !Number.isInteger(frame) || frame<0 || frame>max;
    el('result-frame-error').hidden=!invalid;event.target.setAttribute('aria-invalid',String(invalid));
    text('result-frame-error',invalid?`请输入 0–${max} 之间的整数帧号。`:'');
    if(!invalid)seek(frame);
  };
  el('result-prev').onclick = () => seek(position - 1);
  el('result-next').onclick = () => seek(position + 1);
  el('result-prev-issue').onclick = () => { const range = ranges.filter(range => range.start < position).at(-1); if (range) seek(range.start); };
  el('result-next-issue').onclick = () => { const range = ranges.find(range => range.start > position); if (range) seek(range.start); };
  el('result-worm').onchange = event => { worm = Number(event.target.value); renderPhenotypes.select(worm); issueRanges(); draw(position, true); };
  el('result-issue-filter').onchange = () => { if (timeline) { issueRanges(); draw(position, true); } };

  let detailsStarted=false, fullDetails=false;
  function attachVideo(data) {
    if(el('result-video-details').open && data.artifacts?.some(item=>item.key==='video') && !video.hasAttribute('src'))
      video.src=artifactUrl(data,'video',true);
  }
  async function load(data) {
    const version = ++revision; controller?.abort(); controller = new AbortController();
    const activeController = controller;
    timeline = null; pending = null; position = 0; ranges = [];headAnchors={};
    showEvents(null);
    stopIdentity();identityReport={edges:[]};identityEdge=null;el('identity-open').disabled=true;el('identity-open').hidden=true;
    el('result-original').checked=false;
    window.ResultPlayer.reset();
    renderPhenotypes(data.phenotypes && {...data.phenotypes,video_name:data.video_name||data.phenotypes.video_name},data.sample);
    el('result-summary').disabled=!data.phenotypes?.worms?.length;
    text('head-review-message','');el('result-frame-error').hidden=true;el('result-frame-number').removeAttribute('aria-invalid');
    video.pause(); video.removeAttribute('src'); video.load();
    controls.forEach(id => el(id).disabled = true);
    ['studio-metric-cards','studio-extra-metrics','studio-extra-artifacts','result-dimensions','result-quality-track','result-worm','studio-artifacts'].forEach(id => el(id).replaceChildren());
    text('result-frame-label', '正在读取结果…'); text('result-identity-title', '正在读取质量记录');
    text('result-identity-note', '逐帧指标与质量状态使用同一份记录。');
    text('result-quality-badge', '逐帧检查'); text('result-issue-count', '—');
    text('result-worm-count', ''); el('result-identity').classList.remove('needs-review');
    text('result-metric-note', '不可用的指标显示为 —');
    text('result-issue-note', '按连续帧归为问题段，点击即可定位。');
    el('result-frame').value = el('result-frame-number').value = 0;
    el('results-load-state').hidden = false; el('results-retry').hidden = true;
    text('results-load-message', '正在读取逐帧指标…');
    text('results-notice', data.notice || ''); el('results-notice').hidden = !data.notice || !!data.history_reopened;
    el('result-reannotate').hidden = !data.can_reannotate;
    el('result-more').hidden = !data.can_reannotate;
    el('result-export-empty').hidden = !!data.artifacts?.length;
    el('result-worm-picker').hidden = true;
    text('result-review-hint', data.can_reannotate ? '重新标注会创建新结果，保留已有记录。身份确认需要独立复核证据。' : '身份存疑时，面积仅作为匿名轮廓参考。');
    for (const artifact of data.artifacts || []) {
      const link = document.createElement('a');
      link.href = artifactUrl(data,artifact.key);
      link.textContent = '↓ ' + artifact.label; el(['video','metrics','summary'].includes(artifact.key)?'studio-artifacts':'studio-extra-artifacts').append(link);
    }
    el('result-video-message').hidden = true;
    if (data.artifacts?.some(item => item.key === 'video')) {
      video.hidden = false; attachVideo(data);
    } else {
      video.hidden = true; el('result-video-message').hidden = false;
      text('result-video-message', '本次未生成标注视频。已生成的逐帧指标仍可查看。');
    }
    fullDetails=el('result-video-details').open || (!data.summaryOnly && el('result-analysis-details').open);
    detailsStarted=fullDetails || el('summary-trends').open;
    if(!detailsStarted){el('results-load-state').hidden=true;return;}
    if (fullDetails && !data.summaryOnly && !data.artifacts?.some(item => item.key === 'metrics')) {
      text('results-load-message', '本次任务尚未生成逐帧指标。请在“处理进度”查看已完成的阶段。');
      text('result-identity-title', '尚无逐帧质量记录'); return;
    }
    const timeout = setTimeout(() => activeController.abort(), 15000);
    try {
      const endpoint=data.summaryOnly || !fullDetails?'/api/saved-timeline?group='+encodeURIComponent(data.group||'')+(fullDetails?'&detail=1':'')+'&run=':'/api/frame-metrics?run=';
      const response = await fetch(endpoint + encodeURIComponent(data.sample), {cache:'no-store', signal:activeController.signal});
      const result = await response.json();
      if (!response.ok) throw Error(result.error || '逐帧指标不可用');
      if (version !== revision) return;
      if (result.sample !== data.sample || !result.worm_ids?.length || result.frames?.length !== result.frame_count) throw Error('结果会话或逐帧记录不完整，请重新读取。');
      timeline = result; worm = result.worm_ids.includes(renderPhenotypes.selected()) ? renderPhenotypes.selected() : result.worm_ids[0];
      renderPhenotypes.setTimeline(result);
      if(!fullDetails){el("results-load-state").hidden=true;timeline=null;return;}
      el('head-auto').hidden=result.head_detection_mode==='taper_seed';el('head-auto').disabled=false;text('head-auto-message','');
      window.ResultPlayer.configure(result,frame=>sync(frame/result.fps));
      if (!data.summaryOnly) {
        const anchorsResponse=await fetch('/api/head-anchors?run='+encodeURIComponent(data.sample),{cache:'no-store',signal:activeController.signal});
        const anchorsResult=await anchorsResponse.json();if(!anchorsResponse.ok)throw Error(anchorsResult.error||'锚点读取失败');
        if(version!==revision)return;headAnchors=anchorsResult.anchors||{};
        const identityResponse=await fetch('/api/identity-links?run='+encodeURIComponent(data.sample),{cache:'no-store',signal:activeController.signal});
        const identityResult=await identityResponse.json();if(!identityResponse.ok)throw Error(identityResult.error||'身份关联读取失败');
        if(version!==revision)return;identityReport=identityResult;
      }
      for (const id of result.worm_ids) {
        const option = document.createElement('option'); option.value = id; option.textContent = '线虫 ' + String(id).padStart(2, '0');
        el('result-worm').append(option);
      }
      text('result-worm-count', result.worm_ids.length + ' 条');
      el('result-worm-picker').hidden=result.worm_ids.length<2;
      el('result-worm').value=worm;
      controls.forEach(id => el(id).disabled = false);
      el('result-frame').max = el('result-frame-number').max = result.frame_count - 1;
      el('result-issue-filter').value = 'all';
      issueRanges(); draw(0, true); el('results-load-state').hidden = true;
    } catch (error) {
      if (version !== revision) return;
      text('result-identity-title', '质量记录未加载');
      text('results-load-message', error.name === 'AbortError' ? '读取超时，请重试。视频和文件导出仍可使用。' : error.message);
      el('results-retry').hidden = false;
    } finally { clearTimeout(timeout); }
  }
  function update(data) {
    latest = data;
    if (data.status !== 'complete') {
      if (key) { key = ''; revision++; controller?.abort(); timeline = null; showEvents(null); video.pause(); }
      return;
    }
    const nextKey = JSON.stringify([data.sample, data.artifacts, !!data.summaryOnly, data.group]);
    if (visible && key !== nextKey) { key = nextKey; load(data); }
  }
  for(const id of ['result-analysis-details','result-video-details','summary-trends']){
    el(id).addEventListener('toggle',()=>{
      if(id==='result-video-details'){
        window.ResultPlayer.setVisible(visible && el(id).open);
        if(!el(id).open)video.pause();
        else if (document.activeElement === el(id).querySelector('summary')) el('result-canvas').focus({preventScroll:true});
      }
      if(!visible || latest?.status!=='complete' || !el(id).open)return;
      if(!detailsStarted || (id!=='summary-trends' && !fullDetails))load(latest);
      else if(id==='result-video-details')attachVideo(latest);
    });
  }
  el('results-retry').onclick = () => { if (latest?.status === 'complete') load(latest); };
  return {update, setVisible(value) { visible = value; window.ResultPlayer.setVisible(value && el('result-video-details').open); if (!value) video.pause(); else if (latest) update(latest); }};
})();

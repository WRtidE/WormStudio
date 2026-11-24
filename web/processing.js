'use strict';
const $=s=>document.querySelector(s);
let latest=null, stopped=false, renderedRows='', renderedBatch='';
const terminal=new Set(['complete','failed','cancelled']);
const labels={pending:'等待中',running:'处理中',complete:'已完成',reused:'已复用',failed:'已停止'};
function duration(seconds){if(!Number.isFinite(seconds))return '未记录';seconds=Math.max(0,Math.floor(seconds));const m=Math.floor(seconds/60),s=seconds%60;return m>=60?`${Math.floor(m/60)}:${String(m%60).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`}
function text(id,value){$(id).textContent=AnalysisPresentation.text(value)}
function validFrames(row,key){const n=row[key],total=row.frames;return n===null||n===undefined||!total?'—':`${n} / ${total} · ${(100*n/total).toFixed(1)}%`}
function sessionFinished(data){return terminal.has(data.status)&&!data.batch?.running;}
function drawBatch(data,items){
 const panel=$('#batch-progress');
 panel.hidden=items.length<2;
 if(panel.hidden)return;
 const states={pending:'待处理',running:'处理中',complete:'已完成',failed:'失败',cancelled:'已中断',removed:'已移出'};
 const sameVideo=items.every(item=>item.video_path===items[0].video_path);
 const entries=items.map(item=>{
  // The child can finish just before its queue entry is persisted.
  const status=item.status==='running'&&item.sample===data.sample&&terminal.has(data.status)?data.status:item.status;
  const name=sameVideo&&item.chamber_id!=null?'槽 '+item.chamber_id:item.name;
  return {name,status};
 });
 const key=JSON.stringify(entries);
 if(key===renderedBatch)return;
 renderedBatch=key;
 const unit=items.every(item=>item.chamber_id!=null)?'个槽':'项';
 const complete=entries.filter(item=>item.status==='complete').length;
 text('#batch-progress-count',`已完成 ${complete} / ${items.length} ${unit}`);
 $('#batch-progress-items').replaceChildren(...entries.map(({name,status})=>{
  const row=document.createElement('li'),label=document.createElement('small');
  row.dataset.status=status;label.textContent=states[status]||status;row.append(name,label);return row;
 }));
}
function draw(data){
 window.LivePreview?.update(data);
 latest=data;document.body.classList.remove('ready','preparing','running','complete','failed','cancelled');document.body.classList.add(data.batch?.running?'running':data.status);
 text('#video-name',data.video_name||'正在准备任务');
 const items=ResultNavigation.group(data);
 drawBatch(data,items);
 const current=items.find(item=>item.sample===data.sample);
 const prefix=current?.chamber_id!=null?'槽 '+current.chamber_id+' · ':'';
 const finished=terminal.has(data.status), row=data.stages[data.active];
 const title=data.status==='complete'?'处理完成':data.status==='failed'?'处理遇到问题':data.status==='cancelled'?'处理已停止':row?`第 ${data.active+1} / ${data.stages.length} 步 · ${AnalysisPresentation.stage(row)}`:'正在准备处理';
 text('#progress-title',prefix+title+(finished&&data.batch?.running?'，正在更新队列':''));
 const scope=items.length<2?'总':current?.chamber_id!=null?'本槽':'本项';
 text('#percent-label',scope+'进度');text('#elapsed-label',scope+'耗时');
 const quality=data.quality,needsReview=quality?.needs_review>0;
 $('#quality-card').hidden=!needsReview;
 if(needsReview){
  text('#quality-title',`${quality.needs_review}/${quality.total} 条虫结果需复核`);
  $('#quality-worms').replaceChildren();for(const worm of quality.worms.filter(w=>w.needs_review)){const li=document.createElement('li');const name=document.createElement('strong');name.textContent=`线虫 ${worm.worm_id}`;const detail=document.createElement('span');detail.textContent=`有效姿态 ${validFrames(worm,'valid_frames')}；有效掩膜 ${validFrames(worm,'mask_valid_frames')}`;const reason=document.createElement('small');reason.textContent=worm.issues.join('；');li.append(name,detail,reason);$('#quality-worms').append(li)}
 }
 $('#overall-percent').replaceChildren(document.createTextNode(String(Math.floor(data.percent))),Object.assign(document.createElement('span'),{textContent:'%'}));
 $('#overall-bar i').style.width=`${data.percent}%`;$('#overall-bar').setAttribute('aria-valuenow',String(data.percent));
 const rowsKey=JSON.stringify(data.stages.map(s=>[s.name,s.status]));
 if(rowsKey!==renderedRows){renderedRows=rowsKey;$('#stage-list').replaceChildren();data.stages.forEach((s,i)=>{const li=document.createElement('li');li.className='stage-row '+s.status;const icon=document.createElement('span');icon.className='stage-icon';icon.textContent=s.status==='complete'||s.status==='reused'?'✓':s.status==='failed'?'!':String(i+1).padStart(2,'0');const copy=document.createElement('div');copy.className='stage-copy';const title=document.createElement('b');title.textContent=AnalysisPresentation.stage(s);copy.append(title);const status=document.createElement('span');status.className='stage-status';status.textContent=labels[s.status];li.append(icon,copy,status);$('#stage-list').append(li)})}
 $('#current-stage').hidden=finished;
 text('#current-detail',row?.detail||data.detail);
 const hasCount = row?.total > 0 && Number.isFinite(row.current);
 text('#unit-count',hasCount ? `本步已处理 ${row.current} / ${row.total} ${row.unit}` : row?.detail || data.detail || '等待启动');
 text('#stage-elapsed',row ? `本步耗时 ${duration(row.seconds)}` : '');
 $('#task-bar').classList.toggle('indeterminate',!finished&&!row?.total);$('#task-bar i').style.width=finished?(data.status==='complete'?'100%':`${(row?.fraction||0)*100}%`):`${(row?.fraction||0)*100}%`;
 if(finished||row?.total)$('#task-bar').setAttribute('aria-valuenow',String(finished&&data.status==='complete'?100:Math.round((row?.fraction||0)*100)));else $('#task-bar').removeAttribute('aria-valuenow');
 text('#elapsed',duration(data.elapsed_seconds));text('#total-frames',data.frames||'—');text('#total-worms',data.worm_count||'—');text('#device',data.device||'—');text('#sample',data.sample||'—');text('#output-path',data.output_dir||'正在准备输出目录');
 $('#error-card').hidden=!data.error;text('#error-text',AnalysisPresentation.error(data.error));
 text('#run-notice',data.notice||'');$('#run-notice').hidden=!data.notice||!!data.history_reopened;
 const done=sessionFinished(data);
 $('#result-card').hidden=!done||!(data.status==='complete'||items.some(item=>item.status==='complete'));
 $('#finish-session').hidden=!done;
 $('#new-video').hidden=!done||!!data.read_only;
 $('#review-video').hidden=!done||!data.can_reannotate;
 $('.processing-actions').hidden=!done;
 $('#finish-session').firstChild.nodeValue=data.status==='complete'?'完成并退出 ':'退出本次处理 ';
}
async function poll(){
 if(stopped)return;const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),6000);
 try{const response=await fetch('/api/progress',{cache:'no-store',signal:controller.signal});if(!response.ok)throw Error('进度暂不可用');const data=await response.json();draw(data);document.body.classList.remove('offline');$('#offline-notice').hidden=true;stopped=sessionFinished(data);text('#connection-state',stopped?'记录已加载':'实时同步');text('#last-update',data.history_reopened?'历史结果':'')}
 catch(error){document.body.classList.add('offline');$('#offline-notice').hidden=false;text('#connection-state','连接中断')}
 finally{clearTimeout(timeout);if(!stopped)setTimeout(poll,1000)}
}
$('#new-video').onclick=()=>{ window.location.href='/select-video' };
 $('#review-video').onclick=()=>{ window.location.href='/review' };

$('#finish-session').onclick=async()=>{if(!latest||!sessionFinished(latest))return;const b=$('#finish-session');b.disabled=true;try{const response=await fetch('/close',{method:'POST',headers:{'X-Annotation-Token':latest.token}});if(!response.ok)throw Error('无法结束会话');stopped=true;window.LivePreview?.stop();text('#connection-state','会话已结束');text('#last-update','处理记录和结果文件已保存，可以关闭此页面。');b.textContent='会话已结束';$('#new-video').disabled=true;$('#review-video').disabled=true;document.querySelectorAll('#result-card a').forEach(a=>{const span=document.createElement('span');span.textContent=a.textContent;a.replaceWith(span)})}catch(e){b.disabled=false;text('#last-update','连接已断开，可以关闭此页面；结果保留在输出目录。')}};
poll();


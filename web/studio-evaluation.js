'use strict';
window.EvaluationUI = (() => {
  // File previews have no backend or saved annotation session.
  if (window.location.protocol === 'file:') return null;
  const el = id => document.getElementById(id), canvas = el('eval-canvas'), ctx = canvas.getContext('2d');
  let catalog=null, taskId='', session=null, frames={}, frameIndex=0, objectId='', mode='mask', pending=[], history=[];
  let image=null, imageVersion=0, requestVersion=0, dirty=false, saving=false, token='', revision=null, query='', started=0, elapsed=0;
  let loadedAuthor='', loadedRole='', comparisons=[];
  const frame = () => session && frames[session.task.frames[frameIndex]];
  const object = () => frame()?.objects.find(item => item.local_id === objectId);
  const status = (message,error=false) => { el('eval-feedback').textContent=message;el('eval-feedback').classList.toggle('error',error); };
  const blankFrame = n => ({frame:n,source_status:'unknown',complete:false,objects:[],unknown_regions:[],notes:''});
  const blankObject = id => ({local_id:id,mask:{status:'unknown',polygons:[],holes:[]},centerline:{status:'unknown',segments:[],direction:'unoriented'},
    endpoints:{a:{visibility:'unknown',point:null},b:{visibility:'unknown',point:null}},identity:{status:'unknown',individual_id:null,evidence:''},
    head_tail:{status:'unknown',head_endpoint:null,evidence:''},self_crossing:{status:'unknown',connections:[],evidence:''}});
  function changed(){dirty=true;el('eval-save-badge').textContent='有未保存修改';frame().complete=false;el('eval-complete').checked=false;}
  function remember(){history.push(structuredClone(frame()));if(history.length>40)history.shift();}
  async function request(url, options={}) {
    const response=await fetch(url,{cache:'no-store',...options});const result=await response.json();
    if(!response.ok)throw Error(result.error||'评测请求失败');return result;
  }
  function renderTasks(){
    const list=el('eval-task-list');list.replaceChildren();if(!catalog)return;
    const visible=catalog.tasks.filter(t=>(t.name+' '+t.stratum+' '+(t.stratum==='event'?'事件候选':'随机帧')+' '+t.first_frame).toLowerCase().includes(query));
    el('eval-task-count').textContent=`${visible.length} / ${catalog.tasks.length}`;
    for(const task of visible){
      const button=document.createElement('button');button.className='file-row'+(task.task_id===taskId?' selected':'');
      const copy=document.createElement('span');copy.className='file-label';const title=document.createElement('strong'),detail=document.createElement('small');
      title.textContent=task.name;detail.textContent=task.stratum==='random'?`随机帧 · ${task.first_frame}`:`事件候选 · ${task.first_frame}–${task.last_frame}`;
      copy.append(title,detail);button.append(copy);button.onclick=()=>chooseTask(task.task_id);list.append(button);
    }
  }
  function discard(){return !dirty || window.confirm('当前任务有未保存修改。确定放弃这些修改并加载其他记录？');}
  async function chooseTask(id){
    if(saving || !discard())return;
    taskId=id;dirty=false;session=null;frames={};image=null;pending=[];imageVersion++;requestVersion++;
    el('eval-editor').hidden=true;el('eval-save').disabled=el('eval-submit').disabled=true;draw();renderTasks();
    const task=catalog.tasks.find(t=>t.task_id===id);el('eval-title').textContent=task.name;
    localStorage.setItem('wormstudio-eval-task',id);
    if(el('eval-author').value.trim())await load();else status('填写署名并选择角色，然后点击“加载本人记录”。');
  }
  async function load(){
    if(saving || !taskId || !discard())return;
    const author=el('eval-author').value.trim(),role=el('eval-role').value;
    if(!author){status('请先填写标注者署名。',true);return;}
    const version=++requestVersion;
    el('eval-editor').hidden=true;el('eval-save').disabled=el('eval-submit').disabled=true;
    session=null;image=null;pending=[];dirty=false;imageVersion++;draw();
    try{
      const data=await request(`/api/evaluation/task?task=${encodeURIComponent(taskId)}&role=${role}&annotator=${encodeURIComponent(author)}`);
      if(version!==requestVersion)return;
      session=data;loadedAuthor=author;loadedRole=role;comparisons=data.comparisons||[];
      frames=Object.fromEntries(data.task.frames.map(n=>[n,blankFrame(n)]));
      for(const saved of data.annotation?.frames||[])frames[saved.frame]=saved;
      revision=data.annotation?.revision_id||null;dirty=false;elapsed=data.annotation?.active_seconds||0;started=performance.now();
      el('eval-save-badge').textContent=data.annotation?`已加载版本 ${data.annotation.sequence}`:'尚无标注';
      el('eval-role-badge').textContent=role==='adjudicator'?'裁决':role==='annotator_b'?'独立 B':'独立 A';
      el('eval-editor').hidden=false;el('eval-save').disabled=el('eval-submit').disabled=false;
      el('eval-adjudication-label').hidden=el('eval-adjudication').hidden=role!=='adjudicator';
      el('eval-adjudication').value=data.annotation?.review?.decision_notes||'';
      el('eval-comparisons').hidden=role!=='adjudicator';
      el('eval-overlay').hidden=role!=='adjudicator';el('eval-overlay').value='current';
      el('eval-comparison-text').textContent=role==='adjudicator'?JSON.stringify(comparisons.map(r=>({annotator:r.annotator,revision_id:r.revision_id,frames:r.frames})),null,2):'';
      localStorage.setItem('wormstudio-eval-author',author);localStorage.setItem('wormstudio-eval-role',role);
      el('eval-frame-index').max=data.task.frames.length-1;el('eval-frame-index').disabled=false;
      await showFrame(0);
    }catch(error){if(version===requestVersion)status(error.message,true);}
  }
  function renderFields(){
    const current=frame();if(!current)return;
    el('eval-source').value=current.source_status;el('eval-notes').value=current.notes||'';el('eval-complete').checked=current.complete;
    const select=el('eval-object');select.replaceChildren();
    if(!current.objects.some(o=>o.local_id===objectId))objectId=current.objects[0]?.local_id||'';
    for(const item of current.objects){const option=document.createElement('option');option.value=item.local_id;option.textContent='目标 '+item.local_id;select.append(option);}
    select.value=objectId;const o=object();
    document.querySelectorAll('[data-eval-tool]').forEach(button=>{button.disabled=!o&&button.dataset.evalTool!=='unknown';button.classList.toggle('active',button.dataset.evalTool===mode);});
    for(const [id,value] of Object.entries({'eval-mask-state':o?.mask.status||'unknown','eval-line-state':o?.centerline.status||'unknown',
      'eval-a-state':o?.endpoints.a.visibility||'unknown','eval-b-state':o?.endpoints.b.visibility||'unknown',
      'eval-identity-state':o?.identity.status||'unknown','eval-individual':o?.identity.individual_id||'',
      'eval-identity-evidence':o?.identity.evidence||'','eval-head':o?.head_tail.head_endpoint||'unknown',
      'eval-head-evidence':o?.head_tail.evidence||'','eval-crossing-state':o?.self_crossing.status||'unknown','eval-crossing-evidence':o?.self_crossing.evidence||''})){
      el(id).value=value;el(id).disabled=!o;
    }
    el('eval-clear-geometry').disabled=!o;
  }
  async function showFrame(index){
    if(!session || index<0 || index>=session.task.frames.length)return;
    if(pending.length){status('请先完成当前路径，或用撤销移除未完成的点。',true);return;}
    frameIndex=index;history=[];objectId='';image=null;const version=++imageVersion;draw();renderFields();
    const n=session.task.frames[index],event=session.task.event;
    const inEvent=event&&n>=event.start_frame&&n<=event.end_frame;
    el('eval-frame-label').textContent=`第 ${n} 帧 · ${index+1}/${session.task.frames.length} ${event?(inEvent?'事件范围（待复核）':'事件前后上下文'):'随机抽样'}`;
    el('eval-frame-index').value=index;el('eval-prev').disabled=index===0;el('eval-next').disabled=index===session.task.frames.length-1;
    canvas.width=session.video.width;canvas.height=session.video.height;fit();
    status('正在读取原始帧…');
    const im=new Image();im.onload=()=>{if(version!==imageVersion)return;image=im;draw();status('仅依据原始画面独立标注；不可见身体段、身份和头尾可以保留未知。');};
    im.onerror=()=>{if(version===imageVersion)status('原帧读取失败或哈希改变。请检查源文件，勿用旧坐标继续标注。',true);};
    im.src=`/api/evaluation/frame?task=${encodeURIComponent(taskId)}&frame=${n}`;
  }
  function fit(){
    const viewport=el('eval-viewport');const scale=el('eval-zoom').value==='fit'?Math.min((viewport.clientWidth-24)/canvas.width,(viewport.clientHeight-24)/canvas.height):Number(el('eval-zoom').value);
    viewport.style.overflow=el('eval-zoom').value==='fit'?'hidden':'auto';
    if(scale>0){canvas.style.width=(canvas.width*scale)+'px';canvas.style.height=(canvas.height*scale)+'px';}draw();
  }
  function path(points,color,closed=false,fill=false){
    if(!points.length)return;ctx.beginPath();ctx.moveTo(...points[0]);for(const p of points.slice(1))ctx.lineTo(...p);if(closed)ctx.closePath();
    ctx.strokeStyle=color;ctx.lineWidth=Math.max(2,canvas.width/400);ctx.stroke();if(fill){ctx.fillStyle=color+'35';ctx.fill();}
  }
  function draw(){
    ctx.clearRect(0,0,canvas.width,canvas.height);if(!image)return;ctx.drawImage(image,0,0,canvas.width,canvas.height);
    const overlay=el('eval-overlay').value;
    const current=overlay==='current'?frame():comparisons[Number(overlay)]?.frames.find(f=>f.frame===frame()?.frame);if(!current)return;
    for(const o of current.objects){const color=o.local_id===objectId?'#4ac898':'#9cb6dd';
      ctx.beginPath();for(const p of [...o.mask.polygons,...o.mask.holes]){if(!p.length)continue;ctx.moveTo(...p[0]);for(const xy of p.slice(1))ctx.lineTo(...xy);ctx.closePath();}
      ctx.fillStyle=color+'35';ctx.fill('evenodd');
      for(const p of o.mask.polygons)path(p,color,true);for(const p of o.mask.holes)path(p,'#ceaa78',true);
      for(const p of o.centerline.segments)path(p,'#ffffff');for(const p of o.self_crossing.connections)path(p,'#e8a758');
      for(const [name,end] of Object.entries(o.endpoints))if(end.point){ctx.beginPath();ctx.arc(...end.point,Math.max(4,canvas.width/160),0,Math.PI*2);ctx.fillStyle='#f3d46b';ctx.fill();ctx.font=`${Math.max(14,canvas.width/65)}px sans-serif`;ctx.fillText(name.toUpperCase(),end.point[0]+8,end.point[1]-8);}
    }
    for(const p of current.unknown_regions)path(p,'#d88e81',true,true);path(pending,'#f0bc5b');
    for(const p of pending){ctx.beginPath();ctx.arc(...p,Math.max(3,canvas.width/300),0,Math.PI*2);ctx.fillStyle='#f0bc5b';ctx.fill();}
  }
  canvas.addEventListener('click',event=>{
    if(!session||!image||saving)return;
    if(el('eval-overlay').value!=='current'){status('正在查看独立标注；切换到“本人裁决草稿”后再绘制。',true);return;}
    if(frame().source_status!=='usable'){status('请先将源帧状态设为“可观察”；损坏或未知帧只记录未知。',true);return;}
    if(!object()&&mode!=='unknown'){status('先添加一个目标，再绘制其轮廓或中线。',true);return;}
    const bounds=canvas.getBoundingClientRect();const p=[Math.min(canvas.width-1,Math.max(0,(event.clientX-bounds.left)*canvas.width/bounds.width)),Math.min(canvas.height-1,Math.max(0,(event.clientY-bounds.top)*canvas.height/bounds.height))];
    if(mode==='a'||mode==='b'){remember();object().endpoints[mode]={visibility:'visible',point:p};changed();renderFields();}
    else{pending.push(p);dirty=true;el('eval-save-badge').textContent='路径未完成';}draw();
  });
  function finish(){
    if(!session||saving)return;const minimum=['mask','hole','unknown'].includes(mode)?3:2;
    if(pending.length<minimum){status(`此工具至少需要 ${minimum} 个点。`,true);return;}
    remember();const o=object();
    if(mode==='unknown')frame().unknown_regions.push(pending);
    else if(mode==='mask'||mode==='hole'){o.mask[mode==='mask'?'polygons':'holes'].push(pending);o.mask.status='visible';}
    else if(mode==='centerline'){o.centerline.segments.push(pending);o.centerline.status=o.centerline.segments.length>1?'partial':'visible';}
    else if(mode==='crossing'){o.self_crossing.connections.push(pending);o.self_crossing.status='present';}
    pending=[];changed();renderFields();draw();status('路径已加入草稿；仍需保存。');
  }
  async function save(submit){
    if(!session||saving)return;
    if(pending.length){status('请先完成当前路径。',true);return;}
    if(el('eval-author').value.trim()!==loadedAuthor||el('eval-role').value!==loadedRole){status('署名或角色已更改，请先加载对应记录。',true);return;}
    if(!token){status('本地会话未连接，暂时不能保存。',true);return;}
    saving=true;
    const controls=[...document.querySelectorAll('#evaluation-workspace input, #evaluation-workspace select, #evaluation-workspace textarea, #evaluation-workspace button')];
    const disabled=controls.map(control=>control.disabled);controls.forEach(control=>control.disabled=true);
    try{
      const review=loadedRole==='adjudicator'?{compared_revision_ids:comparisons.map(r=>r.revision_id),decision_notes:el('eval-adjudication').value.trim()}:null;
      const result=await request('/api/evaluation/save',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':token},body:JSON.stringify({
        task_id:taskId,role:loadedRole,annotator:loadedAuthor,base_revision:revision,status:submit?'submitted':'draft',
        frames:Object.values(frames),review,active_seconds:elapsed+(performance.now()-started)/1000})});
      revision=result.revision_id;elapsed=result.active_seconds;started=performance.now();dirty=false;
      el('eval-save-badge').textContent=`${submit?'已提交':'草稿已保存'} · 版本 ${result.sequence}`;status('已保存独立版本；原始文件与分析结果保持原样。');
    }catch(error){status(error.message,true);}
    finally{saving=false;controls.forEach((control,index)=>control.disabled=disabled[index]);}
  }
  el('eval-load').onclick=load;el('eval-save').onclick=()=>save(false);el('eval-submit').onclick=()=>save(true);
  el('eval-prev').onclick=()=>showFrame(frameIndex-1);el('eval-next').onclick=()=>showFrame(frameIndex+1);
  el('eval-frame-index').oninput=e=>showFrame(Number(e.target.value));el('eval-zoom').onchange=fit;
  el('eval-overlay').onchange=draw;
  el('eval-object').onchange=e=>{if(pending.length){e.target.value=objectId;status('先完成当前路径。',true);return;}objectId=e.target.value;renderFields();draw();};
  el('eval-add-object').onclick=()=>{if(!session||saving)return;if(pending.length){status('先完成当前路径，再添加目标。',true);return;}remember();let id=1;while(frame().objects.some(o=>o.local_id===String(id)))id++;objectId=String(id);frame().objects.push(blankObject(objectId));changed();renderFields();draw();};
  document.querySelectorAll('[data-eval-tool]').forEach(button=>button.onclick=()=>{if(pending.length){status('先完成或撤销当前路径，再更换工具。',true);return;}mode=button.dataset.evalTool;renderFields();el('eval-tool-hint').textContent=mode==='a'||mode==='b'?'点击可见端点；A/B 尚不表示解剖头尾。':mode==='centerline'?'每条路径只描可见连续段；分段时再次绘制，空隙不连线。':mode==='crossing'?'沿可辨认的自交分支连接绘制，并填写连接依据。':'逐点绘制，完成后点击“完成当前路径”。';});
  el('eval-finish').onclick=finish;
  el('eval-undo').onclick=()=>{if(pending.length)pending.pop();else if(history.length){frames[frame().frame]=history.pop();dirty=true;renderFields();}draw();};
  el('eval-clear-geometry').onclick=()=>{if(!object())return;remember();const o=object(),empty=blankObject(o.local_id);o.mask=empty.mask;o.centerline=empty.centerline;o.endpoints=empty.endpoints;o.head_tail=empty.head_tail;o.self_crossing=empty.self_crossing;pending=[];changed();renderFields();draw();};
  el('eval-source').onchange=e=>{remember();frame().source_status=e.target.value;changed();};
  el('eval-notes').oninput=e=>{frame().notes=e.target.value;dirty=true;el('eval-save-badge').textContent='有未保存修改';};
  el('eval-complete').onchange=e=>{frame().complete=e.target.checked;dirty=true;el('eval-save-badge').textContent='有未保存修改';};
  el('eval-adjudication').oninput=()=>{dirty=true;};
  for(const name of ['mask','line','a','b'])el('eval-'+name+'-state').onchange=e=>{const o=object();if(!o)return;remember();
    if(name==='mask')o.mask.status=e.target.value;else if(name==='line')o.centerline.status=e.target.value;else{o.endpoints[name].visibility=e.target.value;if(e.target.value!=='visible')o.endpoints[name].point=null;}
    changed();draw();};
  el('eval-identity-state').onchange=e=>{if(!object())return;remember();object().identity.status=e.target.value;if(e.target.value!=='confirmed')object().identity.individual_id=null;changed();renderFields();};
  el('eval-individual').oninput=e=>{if(object()){object().identity.individual_id=e.target.value.trim()||null;changed();}};
  el('eval-identity-evidence').oninput=e=>{if(object()){object().identity.evidence=e.target.value;changed();}};
  el('eval-head').onchange=e=>{if(!object())return;remember();object().head_tail.status=e.target.value==='unknown'?'unknown':'confirmed';object().head_tail.head_endpoint=e.target.value==='unknown'?null:e.target.value;changed();};
  el('eval-head-evidence').oninput=e=>{if(object()){object().head_tail.evidence=e.target.value;changed();}};
  el('eval-crossing-state').onchange=e=>{if(object()){remember();object().self_crossing.status=e.target.value;changed();}};
  el('eval-crossing-evidence').oninput=e=>{if(object()){object().self_crossing.evidence=e.target.value;changed();}};
  window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  new ResizeObserver(fit).observe(el('eval-viewport'));
  el('eval-author').value=localStorage.getItem('wormstudio-eval-author')||'';
  el('eval-role').value=localStorage.getItem('wormstudio-eval-role')||'annotator_a';
  return {filter(value){query=value.trim().toLowerCase();renderTasks();},setToken(value){token=value;},async setVisible(value){
    if(!value)return;fit();if(catalog)return;
    try{catalog=await request('/api/evaluation/catalog');const c=catalog.counts;
      el('eval-dataset-status').textContent=`${catalog.dataset} · 开发 ${c.development||0} / 验证 ${c.validation||0} / 留出 ${c.test||0} / 待划分 ${c.pending||0}。任务仅用于独立标注，待划分材料不进入试点评测。`;
      renderTasks();const last=localStorage.getItem('wormstudio-eval-task');if(catalog.tasks.some(t=>t.task_id===last))await chooseTask(last);
    }catch(error){status(error.message,true);el('eval-dataset-status').textContent='评测任务暂不可用；其他工作台功能仍可使用。';}
  }};
})();

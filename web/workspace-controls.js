'use strict';
(() => {
  if (location.protocol==='file:') return;
  const el=id=>document.getElementById(id);
  async function action(path,body={}) {
    if(path==='/api/open-run')return window.StudioWorkspace.openSavedResult(body.sample);
    if(path!=='/api/remove-history')await window.StudioWorkspace?.prepareToLeave();
    const state=await (await fetch('/api/progress')).json();
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':state.token},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok) throw Error(result.error||'操作失败');
    if(path==='/api/remove-history')return refresh();
    location.reload();
  }
  function button(text,handler) {
    const b=document.createElement('button'); b.className='plain-button';b.textContent=text;
    b.onclick=()=>{b.disabled=true;Promise.resolve().then(handler).catch(e=>{el('status-text').textContent=e.message;}).finally(()=>b.disabled=false);};
    return b;
  }
  const historyList=el('saved-run-list');
  let opened=null, drag=null, suppressClick=false;
  function reveal(row) {
    opened?.classList.remove('show-delete');
    opened=row;
    opened?.classList.add('show-delete');
  }
  document.addEventListener('pointerdown',event=>{
    suppressClick=false;
    if(opened&&!opened.contains(event.target))reveal(null);
  },true);
  historyList.addEventListener('pointerdown',event=>{
    const row=event.target.closest('.history-row');
    if(!row||event.button!==0||!event.isPrimary||event.target.closest('.history-delete'))return;
    drag={row,id:event.pointerId,x:event.clientX,y:event.clientY};
    event.target.setPointerCapture(event.pointerId);
  });
  historyList.addEventListener('pointermove',event=>{
    if(drag?.id!==event.pointerId)return;
    const dx=event.clientX-drag.x,dy=event.clientY-drag.y;
    if(Math.abs(dy)>8&&Math.abs(dy)>Math.abs(dx)){drag=null;return;}
    if(Math.abs(dx)<24)return;
    reveal(dx<0?drag.row:null);
    suppressClick=true;
    drag=null;
  });
  for(const name of ['pointerup','pointercancel','lostpointercapture']){
    historyList.addEventListener(name,event=>{if(drag?.id===event.pointerId)drag=null;});
  }
  historyList.addEventListener('click',event=>{
    if(!event.detail||event.target.closest('.history-delete'))return;
    if(suppressClick||opened?.contains(event.target)){
      event.stopImmediatePropagation();
      if(!suppressClick)reveal(null);
      suppressClick=false;
    }
  },true);
  let wheelRow=null,wheelDelta=0,wheelTime=0,wheelHandled=false;
  historyList.addEventListener('wheel',event=>{
    const row=event.target.closest('.history-row');
    if(!row||event.ctrlKey||Math.abs(event.deltaX)<=Math.abs(event.deltaY))return;
    event.preventDefault();
    const now=performance.now();
    if(now-wheelTime>180){wheelDelta=0;wheelHandled=false;}
    wheelTime=now;
    if(wheelHandled)return;
    if(wheelRow!==row){wheelRow=row;wheelDelta=0;}
    wheelDelta+=event.deltaX*(event.deltaMode===1?16:event.deltaMode===2?row.clientWidth:1);
    if(Math.abs(wheelDelta)>=24){reveal(wheelDelta>0?row:null);wheelHandled=true;}
  },{passive:false});
  historyList.addEventListener('keydown',event=>{
    const row=event.target.closest('.history-row');
    if(row&&event.key==='Delete'){
      event.preventDefault();reveal(row);row.querySelector('.history-delete').focus();
    }else if(opened&&event.key==='Escape'){
      opened.querySelector('.history-entry').focus();reveal(null);
    }
  });
  async function refresh() {
    try {
      await window.StudioWorkspace?.ready;
      const response=await fetch('/api/projects');if(!response.ok)throw Error('无法读取历史任务');
      const data=await response.json();
      window.StudioWorkspace?.updateRuns(data.runs);
      el('target-video-list').replaceChildren(...data.targets.map(t=>{const b=button('标注 '+t.name,()=>action('/api/new-video',{video_path:t.path}));b.title='标注 '+t.name;return b;}));
      reveal(null);historyList.replaceChildren();
      for(const run of data.history||data.runs) {
        const item=document.createElement('div');item.className='history-row';
        const remove=button('删除',()=>action('/api/remove-history',{sample:run.sample}));
        remove.className='history-delete';remove.setAttribute('aria-label','从历史任务删除 '+run.sample);
        const face=document.createElement('div');face.className='history-face';item.append(remove,face);
        const entry=button('',()=>action(run.complete?'/api/open-run':'/api/resume-run',{sample:run.sample}));entry.classList.add('history-entry');
        const title=document.createElement('span');title.className='history-name';title.textContent=run.sample;
        const label=document.createElement('small');label.textContent=run.complete?'查看结果':'继续任务';
        entry.title=run.sample+' · '+label.textContent;entry.append(title,label);face.append(entry);
        if(run.has_metrics&&!run.complete){const metrics=button('指标',()=>action('/api/open-run',{sample:run.sample}));metrics.title=run.sample+' · 查看已有指标';face.append(metrics);}
        el('saved-run-list').append(item);
      }
    } catch(e) {el('status-text').textContent='任务列表读取失败：'+e.message;}
  }
  window.WorkspaceActions={run:action,refresh};
  el('refresh-projects').onclick=refresh;
  el('cancel-run').onclick=()=>action('/api/cancel').catch(e=>{el('status-text').textContent=e.message;});
  setInterval(async()=>{try{const p=await(await fetch('/api/progress')).json();el('cancel-run').hidden=!['running','preparing'].includes(p.status);}catch{}},2000);
  refresh();
})();

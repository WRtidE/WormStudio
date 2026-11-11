'use strict';
(() => {
  if (location.protocol==='file:') return;
  const el=id=>document.getElementById(id);
  async function action(path,body={}) {
    if(path==='/api/open-run')return window.StudioWorkspace.openSavedResult(body.sample);
    await window.StudioWorkspace?.prepareToLeave();
    const state=await (await fetch('/api/progress')).json();
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':state.token},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok) throw Error(result.error||'操作失败');
    location.reload();
  }
  function button(text,handler) {
    const b=document.createElement('button'); b.className='plain-button';b.textContent=text;
    b.onclick=()=>{b.disabled=true;Promise.resolve().then(handler).catch(e=>{el('status-text').textContent=e.message;}).finally(()=>b.disabled=false);};
    return b;
  }
  async function refresh() {
    try {
      await window.StudioWorkspace?.ready;
      const response=await fetch('/api/projects');if(!response.ok)throw Error('无法读取历史任务');
      const data=await response.json();
      window.StudioWorkspace?.updateRuns(data.runs);
      el('target-video-list').replaceChildren(...data.targets.map(t=>{const b=button('标注 '+t.name,()=>action('/api/new-video',{video_path:t.path}));b.title='标注 '+t.name;return b;}));
      el('saved-run-list').replaceChildren();
      for(const run of data.runs) {
        const item=document.createElement('div');item.className='history-row';
        const entry=button('',()=>action(run.complete?'/api/open-run':'/api/resume-run',{sample:run.sample}));entry.classList.add('history-entry');
        const title=document.createElement('span');title.className='history-name';title.textContent=run.sample;
        const label=document.createElement('small');label.textContent=run.complete?'查看结果':'继续任务';
        entry.title=run.sample+' · '+label.textContent;entry.append(title,label);item.append(entry);
        if(run.has_metrics&&!run.complete){const metrics=button('指标',()=>action('/api/open-run',{sample:run.sample}));metrics.title=run.sample+' · 查看已有指标';item.append(metrics);}
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

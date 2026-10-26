'use strict';
(() => {
  if (location.protocol==='file:') return;
  const el=id=>document.getElementById(id);
  async function action(path,body={}) {
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
      const data=await (await fetch('/api/projects')).json();
      el('target-video-list').replaceChildren(...data.targets.map(t=>button('标注 '+t.name,()=>action('/api/new-video',{video_path:t.path}))));
      el('saved-run-list').replaceChildren();
      for(const run of data.runs) {
        const item=document.createElement('div');
        item.append(button((run.complete?'打开结果 · ':'查看已有指标 · ')+run.sample,()=>action('/api/open-run',{sample:run.sample})));
        if(!run.complete)item.append(button('继续任务',()=>action('/api/resume-run',{sample:run.sample})));
        el('saved-run-list').append(item);
      }
    } catch(e) {el('status-text').textContent='任务列表读取失败：'+e.message;}
  }
  el('refresh-projects').onclick=refresh;
  el('cancel-run').onclick=()=>action('/api/cancel').catch(e=>{el('status-text').textContent=e.message;});
  setInterval(async()=>{try{const p=await(await fetch('/api/progress')).json();el('cancel-run').hidden=!['running','preparing'].includes(p.status);}catch{}},2000);
  refresh();
})();

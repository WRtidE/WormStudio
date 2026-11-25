'use strict';

window.createChannelBatch = function ({request, token, getFiles, current, openSetup, openRun, retryImport, refreshComparison}) {
  const el = key => document.getElementById('count-batch-' + key);
  const panel = document.getElementById('count-batch');
  const labels = {pending:'等待计数', running:'计数中', paused:'计数已暂停', interrupted:'上次中断',
    complete:'已完成', failed:'失败', skipped:'已取消'};
  const storageKey = 'worm-channel-staging';
  const stagedRows = new Map(), confirmed = new Map();
  let editing = null;
  let staged = [], state = null, busy = false, visible = false, timer, revision = 0, submitted = null;

  function saved(item) {
    return !!item.path && item.available !== false && !item.persisting && !item.persistError;
  }
  function persist() {
    sessionStorage.setItem(storageKey, JSON.stringify({files:staged.filter(saved).map(f=>f.id),
      confirmed:Object.fromEntries(confirmed), editing:editing?.id, submitted}));
  }
  function button(text, click) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'small-button';
    node.textContent = text;
    node.onclick = click;
    return node;
  }
  function row(name, status) {
    const li = document.createElement('li'), title = document.createElement('span'), detail = document.createElement('span');
    title.textContent = name;
    title.title = name;
    detail.textContent = status;
    li.append(title,detail);
    return li;
  }
  function controls() {
    const items = state?.items || [];
    const waiting = items.some(i=>['pending','interrupted'].includes(i.status));
    const live = items.find(i=>['running','paused'].includes(i.status));
    const failed = items.some(i=>i.status==='failed');
    const complete = items.filter(i=>i.status==='complete').length;
    const total = staged.length, index = editing ? staged.indexOf(editing) : -1;
    const source = current(), here = editing && source.id===editing.id && source.setup;
    const next = el('next'), name = editing?.name || (!total && live?.name) || '';
    panel.toggleAttribute('data-staging', !!total);
    el('title').textContent = total ? '批量标注' : '批量计数';
    el('summary').textContent = editing ? `视频 ${index+1} / ${total}` : total ? `已确认 ${confirmed.size} / ${total}` :
      items.length ? `已完成 ${complete} / ${items.length}${waiting && !state.running ? ' · 队列已暂停' : ''}` : '';
    el('filename').textContent = name; el('filename').title = name;
    el('staging').hidden = !total;
    el('history').hidden = !items.length;
    el('empty').hidden = !!total || !!items.length;
    el('back').hidden = !total || index===0;
    el('back').textContent = editing ? '上一个' : '返回标注';
    el('back').disabled = busy;
    next.hidden = !total && !live;
    if (editing) {
      next.textContent = editing.persistError ? '重试导入' : !saved(editing) ? (editing.persisting?'正在导入…':'整理队列') :
        !here ? '返回标注' : confirmed.size===total-1 ? '确认检测线' : '确认并继续';
      next.disabled = busy || !!editing.persisting || (saved(editing) && here && !source.ready);
    } else if (total) {
      next.textContent = waiting || live ? '等待当前批次结束' : `开始批量计数（${total}）`;
      next.disabled = busy || !state?.available || waiting || !!live || confirmed.size!==total || !staged.every(saved);
    } else { next.textContent='查看当前进度'; next.disabled=busy; }
    const link = el('export');
    link.hidden = !complete;
    if (complete) {
      link.href = '/api/channel/batch-export?' + new URLSearchParams({id:state.id});
      link.download = '通道计数批次-' + state.id.slice(0,8) + '.xlsx';
    }
    el('pause').hidden = !state?.running || !waiting;
    el('resume').hidden = !!state?.running || !waiting;
    el('retry').hidden = !failed || !!state?.running || !!live;
    el('cancel').hidden = !waiting;
    for (const key of ['pause','resume','retry','cancel']) el(key).disabled = busy;
    el('toggle').textContent = el('body').hidden ? '查看队列' : '收起队列';
  }
  function edit(item) {
    editing=item;
    if (item) confirmed.delete(item.id);
    el('message').textContent='';
    persist(); staging();
    if (visible && item) openSetup(item.id);
  }
  function setExpanded(on) {
    if (on === !el('body').hidden) return;
    el('body').hidden=!on;
    el('toggle').setAttribute('aria-expanded',String(on));
    controls();
    if (on && visible && location.protocol !== 'file:') void refresh();
  }
  function stageRow(item) {
    const li = row(item.relativePath || item.name, item.persisting ? '正在保存' : item.persistError ||
      (saved(item) ? confirmed.has(item.id) ? '已确认' : item===editing ? '标注中' : '待标注' : '文件不可用'));
    if (item===editing) li.setAttribute('aria-current','step');
    const actions = document.createElement('span'); actions.className='count-batch-actions';
    if (item.persistError) actions.append(button('重试导入',()=>retryImport(item)));
    if (saved(item)) actions.append(button('标注',()=>{ if (!busy) { edit(item); setExpanded(false); } }));
    actions.append(button('移出',()=>{
      if (busy) return;
      staged = staged.filter(f=>f!==item); confirmed.delete(item.id);
      if (item===editing) edit(staged.find(f=>!confirmed.has(f.id)) || null);
      else { persist(); staging(); }
    }));
    li.append(actions);
    stagedRows.set(item,li);
    return li;
  }
  function staging() {
    stagedRows.clear();
    el('list').replaceChildren(...staged.map(stageRow));
    controls();
  }
  function resultList() {
    el('results').replaceChildren(...(state?.items || []).map(item=>{
      const info = [labels[item.status], item.status==='complete' ? item.total+' 次越线' : '',item.error].filter(Boolean);
      const li = row(item.name, info.join(' · '));
      if (['complete','running','paused','interrupted'].includes(item.status)) {
        li.append(button(item.status==='complete' ? '查看结果' : '查看进度',()=>openRun(item)));
      }
      return li;
    }));
  }
  function accept(data) {
    const changed = JSON.stringify(state) !== JSON.stringify(data);
    if (changed && data.items.some((item,i)=>item.status==='complete' &&
        (state?.items[i]?.run!==item.run || state.items[i].status!=='complete'))) void refreshComparison();
    state = data;
    if (submitted && submitted.id===state.id) {
      const ids = new Set(submitted.files);
      staged = staged.filter(f=>!ids.has(f.id));
      for (const id of ids) confirmed.delete(id);
      submitted = null;
      if (!staged.includes(editing)) editing=staged.find(f=>!confirmed.has(f.id)) || null;
      persist(); staging();
      if (visible && !staged.length) {
        const first=state.items.find(item=>['running','paused','complete'].includes(item.status));
        if (first) openRun(first);
      }
    }
    if (changed) resultList();
    controls();
  }
  function schedule(delay) {
    clearTimeout(timer);
    if (visible) timer=setTimeout(refresh,delay);
  }
  async function refresh() {
    if (busy || !visible) return;
    const version = ++revision;
    try {
      const data = await request('/api/channel/batch');
      if (version!==revision) return;
      accept(data);
      el('connection').textContent = data.error || '';
      if (data.running || data.items.some(i=>['running','paused'].includes(i.status))) schedule(1000);
    } catch (e) {
      if (version===revision) { el('connection').textContent=e.message; schedule(3000); }
    }
  }
  async function command(action) {
    if (busy || !state) return;
    const body = {action,id:state.id};
    if (action==='start') {
      const files = staged.map(item=>item.id);
      if (!submitted || JSON.stringify(submitted.files)!==JSON.stringify(files)) submitted = {id:crypto.randomUUID(),files};
      if (confirmed.size!==files.length || !staged.every(item=>saved(item) && confirmed.has(item.id))) return;
      Object.assign(body, submitted, {previous:state.id, settings_by_file:Object.fromEntries(confirmed)});
      persist();
    }
    busy=true; revision++; clearTimeout(timer); controls();
    el('message').textContent='';
    try {
      const key = await token();
      accept(await request('/api/channel/batch',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':key},body:JSON.stringify(body)}));
    } catch(e) { el('message').textContent=e.message; }
    finally { busy=false; controls(); schedule(0); }
  }
  for (const key of ['pause','resume','retry','cancel']) el(key).onclick=()=>void command(key);
  el('toggle').onclick=()=>setExpanded(el('body').hidden);
  el('back').onclick=()=>edit(editing ? staged[staged.indexOf(editing)-1] : staged.at(-1));
  el('next').onclick=event=>{
    if (busy || event.detail>1) return;
    if (!staged.length) {
      const live=state?.items.find(item=>['running','paused'].includes(item.status));
      if (live) openRun(live);
    } else if (!editing) void command('start');
    else if (editing.persistError) retryImport(editing);
    else if (!saved(editing)) setExpanded(true);
    else {
      const source=current();
      if (source.id!==editing.id || !source.setup) { openSetup(editing.id); return; }
      if (!source.ready) return;
      confirmed.set(editing.id,{...source.settings});
      const index=staged.indexOf(editing);
      edit(staged.find((item,i)=>i>index && !confirmed.has(item.id)) || staged.find(item=>!confirmed.has(item.id)) || null);
    }
  };

  return {
    stage(items) {
      const ids = new Set(staged.map(f=>f.id));
      for (const item of items) if (!ids.has(item.id)) { staged.push(item); ids.add(item.id); }
      if (!editing) edit(staged.find(item=>!confirmed.has(item.id)) || null);
      else { persist(); staging(); }
      setExpanded(false);
    },
    uploaded(item) {
      const li = stagedRows.get(item);
      if (li) { li.replaceWith(stageRow(item)); persist(); controls(); }
    },
    get stagedCount() { return staged.length; },
    get expanded() { return !el('body').hidden; },
    setExpanded,
    isEditing(id) { return editing?.id===id; },
    changed(id) {
      if (!confirmed.delete(id)) return;
      editing=staged.find(item=>item.id===id);
      persist(); staging();
    },
    parameters() { if (staged.length) controls(); },
    visible(on) {
      visible=on; clearTimeout(timer); revision++;
      if (on && location.protocol!=='file:') {
        if (editing) openSetup(editing.id);
        void refresh();
      }
    },
    restore() {
      let stored;
      try { stored = JSON.parse(sessionStorage.getItem(storageKey)) || {}; } catch { stored = {}; }
      const library = new Map(getFiles().map(f=>[f.id,f]));
      staged = (stored.files || []).map(id=>library.get(id)).filter(Boolean);
      submitted = stored.submitted || null;
      for (const item of staged) if (stored.confirmed?.[item.id]) confirmed.set(item.id,stored.confirmed[item.id]);
      editing=staged.find(item=>item.id===stored.editing && !confirmed.has(item.id)) || staged.find(item=>!confirmed.has(item.id)) || null;
      if (state) accept(state);
      staging();
      if (visible && editing) openSetup(editing.id);
    }
  };
};

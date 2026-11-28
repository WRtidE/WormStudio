'use strict';

// The two panels grow towards the middle, so the lower divider reverses the drag direction.
function bindChannelDivider(panel, direction, isOpen, setOpen) {
  const body = document.getElementById(panel.id+'-body');
  const handle = document.getElementById(panel.id+'-divider');
  const workspace = panel.parentElement, work = document.getElementById('count-work');
  const empty = document.getElementById('count-empty');
  const store = window.WorkspaceLinks.layoutStore, key = 'studio:'+panel.id;
  let drag = null, preferred;

  function setHeight(height) {
    preferred = height;
    if (height === undefined) body.style.removeProperty('--pane-height');
    else body.style.setProperty('--pane-height', height+'px');
  }
  function restore() {
    if (drag) return;
    const {height} = store.read(key);
    setHeight(Number.isFinite(height) && height > 0 ? height : undefined);
  }
  function save() { store.write(key, preferred === undefined ? {} : {height:preferred}); }
  function size() { return isOpen() ? body.offsetHeight : 0; }
  function limit() {
    const middle = work.hidden ? empty : work;
    return Math.max(0, size() + middle.offsetHeight - Math.min(150, workspace.clientHeight * .24));
  }
  function resize(requested) {
    const height = Math.round(Math.max(0, Math.min(requested, limit())));
    const expanded = height >= 5;
    if (!expanded && !isOpen()) return;
    setHeight(expanded ? height : undefined);
    if (expanded !== isOpen()) setOpen(expanded);
  }
  handle.addEventListener('pointerdown', event => {
    if (drag || event.button !== 0 || !event.isPrimary) return;
    drag = {id:event.pointerId, y:event.clientY, height:size()};
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
    handle.focus({preventScroll:true});
    handle.setPointerCapture(event.pointerId);
    handle.toggleAttribute('data-dragging', true);
    event.preventDefault();
  });
  function move(event) {
    if (drag?.id !== event.pointerId || event.clientY === drag.y && !drag.moved) return;
    drag.moved = true;
    resize(drag.height + direction * (event.clientY - drag.y));
  }
  function finish(event) {
    if (drag?.id !== event.pointerId) return;
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', finish);
    window.removeEventListener('pointercancel', finish);
    if (drag.moved) save();
    drag = null;
    handle.removeAttribute('data-dragging');
  }
  handle.addEventListener('lostpointercapture', finish);
  function reset() { setHeight(undefined); save(); }
  handle.addEventListener('dblclick', reset);
  handle.addEventListener('keydown', event => {
    if (event.key === 'Home') reset();
    else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      resize(size() + direction * (event.key === 'ArrowUp' ? -24 : 24));
      save();
    } else return;
    event.preventDefault();
  });
  const observer = new ResizeObserver(() => {
    const height = size();
    handle.setAttribute('aria-valuenow', String(Math.min(100, Math.round(100 * height / (limit() || 1)))));
    handle.setAttribute('aria-valuetext', isOpen() ? height+' 像素' : '已收起');
  });
  for (const element of [body, panel, work, empty]) observer.observe(element);
  store.subscribe(restore);
  restore();
}

window.createChannelWorkspace = function ({request, token, openVideo, getFiles, retryImport}) {
  const el = id => document.getElementById(id);
  const tabs = [...document.querySelectorAll('[data-count-tab]')];
  const svg = el('count-view'), bars = el('cross-chart');
  const viewport = window.ResultViewport.bindZoom({stage:svg, controls:el('count-zoom-tools'), onZoom:draw,
    paint({width, height, transform:{s, x, y}}) {
      svg.setAttribute('viewBox', [-x/s, -y/s, width/s, height/s].join(' '));
    }});
  let gesture = null;
  const channelInput = el('count-channels'), channelFilter = el('cross-filter');
  const labelWidth = 60, labelHeight = 22;
  const lineNames = {mask_top_ratio:'上边界', tripwire_ratio:'计数线', mask_bottom_ratio:'下边界'};
  const states = {running:'计数中', paused:'已暂停', interrupted:'计数中断', failed:'计数失败', complete:'已完成'};
  let file = null, page = 'setup', task = null, run = null, activeRun = null, draft = null, history = [];
  let setupPhoto = null, setupError = '', drawnPhoto = null, guides, labelLayer, highlighted = null;
  let generation = 0, refreshRevision = 0, setupRevision = 0, timer, pending = false, writes = Promise.resolve();
  const route = (name, params) => '/api/channel/' + name + '?' + new URLSearchParams({file:file.id, ...params});
  const renderReview = window.createChannelReview({request, route});
  const refreshComparison = window.createChannelComparison({request,post,getFiles,openRun:(entry,channel)=>{
    openVideo(entry.file); chooseRun(entry.run,channel);
  }});
  const batch = window.createChannelBatch({request,token,getFiles,retryImport,refreshComparison,
    openSetup:id=>{
      openVideo(id);
      if (file?.id===id && draft) changePage('setup');
    },
    current:()=>({id:file?.id, setup:page==='setup', ready:!!setupPhoto && !!file?.available && !pending &&
      channelInput.validity.valid && channelInput.valueAsNumber===draft?.num_channels, settings:draft}),
    openRun:item=>{
      if (!getFiles().some(f=>f.id===item.file)) { notice('count-message','该视频已从工作区移除'); return; }
      openVideo(item.file); chooseRun(item.run);
    }});
  const comparison = el('count-comparison');
  bindChannelDivider(el('count-batch'), 1, () => batch.expanded, batch.setExpanded);
  bindChannelDivider(comparison, -1, () => comparison.open, open => { comparison.open = open; });
  let live = null, newPreviewRun = null;

  function stopLive() {
    if (!live) return;
    clearTimeout(live.timer);
    live.source.close();
    live = null;
  }

  function position(frame) {
    el('count-position').textContent = frame > 0 ? '预览 · ' + channelFrameLabel(frame,task.fps) : '';
    el('count-latest').disabled = pending || !task.picture || frame >= task.frame;
  }

  async function playLive(player) {
    if (player !== live || player.busy) return;
    player.busy = true;
    let delay = 60;
    try {
      if (task.status==='paused' || document.hidden || pending) { delay=120; return; }
      if (player.frame >= task.frame) {
        if (task.status==='complete') {
          if (gesture || viewport.zoomed) {
            // Keep the inspected area on screen when playback reaches the last frame.
            stopLive();
            render();
          } else changePage('result');
        }
        return;
      }
      if (!player.frame && task.status==='running' && task.frame<Math.min(task.frames || 6,6)) return;
      const frame = player.frame+1;
      const photo = await player.source.read(frame,1);
      if (!photo || player!==live) return;
      const interval = 1000/Math.min(task.fps || 24,60);
      let painted = await new Promise(requestAnimationFrame);
      while (player===live && !pending && task.status!=='paused' && painted+1 < player.nextPaint) {
        painted = await new Promise(requestAnimationFrame);
      }
      if (player!==live || pending || task.status==='paused') return;
      const oldCounts = player.photo?.counts;
      player.frame = frame;
      player.photo = photo;
      if (!oldCounts || photo.counts.some((n,i)=>oldCounts[i]!==n)) distribution();
      draw();
      position(frame);
      notice('count-photo-status','');
      player.source.prefetch(frame,1,task.frame);
      // Rebase after a stall or pause instead of flashing several catch-up frames.
      player.nextPaint = (painted-player.nextPaint < interval/2 ? player.nextPaint : painted) + interval;
      delay = Math.max(0,interval-25);
    } catch (error) {
      if (player === live) {
        notice('count-photo-status',error.message);
        delay = 800;
      }
    } finally {
      player.busy = false;
      if (player === live) player.timer=setTimeout(()=>playLive(player),delay);
    }
  }

  function syncLive() {
    if (page!=='count' || !task || (live && live.run!==task.id)) stopLive();
    if (page!=='count' || !task) return;
    if (!['running','paused','complete'].includes(task.status)) { stopLive(); return; }
    if (live || task.status==='complete') return;
    // Joining an existing job starts at its checkpoint. A new job starts with frame one.
    const joining = task.picture && task.id!==newPreviewRun;
    const player = {run:task.id, frame:joining?task.frame:0, photo:joining?{...task.picture,counts:task.counts}:null,
      source:window.createChannelFrames({request,route,run:task.id}), busy:false, nextPaint:0};
    live = player;
    newPreviewRun = null;
    player.timer=setTimeout(()=>playLive(player),0);
  }

  function notice(id, text) {
    el(id).textContent = text || '';
    el(id).hidden = !text;
  }
  async function post(id, action, details) {
    const key = await token();
    return request('/api/channel/task', {method:'POST', headers:{'Content-Type':'application/json','X-Annotation-Token':key},
      body:JSON.stringify({file:id, action, ...details})});
  }
  function saveDraft() {
    const version = generation, id = file.id, settings = {...draft};
    writes = writes.catch(() => {}).then(() => post(id, 'draft', {settings}));
    writes.catch(error => { if (generation === version) notice('count-message', error.message); });
  }

  function svgItem(parent, tag, attrs, text) {
    const item = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value);
    if (text != null) item.textContent = text;
    parent.append(item);
    return item;
  }

  function rulerLabels(bounds, unit, selected) {
    const count = bounds.length;
    const labels = new Set();
    if (!count) return labels;
    const centers = bounds.map(([a,b]) => (a+b)/2);
    const minGap = 28*unit;
    const bandWidth = (bounds[count-1][1]-bounds[0][0])/count;
    const step = 2 ** Math.max(0, Math.ceil(Math.log2(minGap/Math.max(unit,bandWidth))));
    const positions = [];
    function add(channel) {
      if (!(channel >= 1 && channel <= count)) return;
      const x = centers[count-channel];
      if (positions.some(other => Math.abs(other-x) < minGap)) return;
      labels.add(channel);
      positions.push(x);
    }
    add(selected);
    add(count);
    add(1);
    for (let ch = count - count%step; ch >= 1; ch -= step) add(ch);
    return labels;
  }

  function positionLineLabels() {
    const {transform:{x,y,s}, height,h} = viewport.view;
    const halfHeight = labelHeight/2, gap = labelHeight+4, inset = 5;
    const left = Math.max(inset, x - labelWidth - 12);
    // Lay out the handles in screen pixels, including the margin beside the image.
    labelLayer.setAttribute('transform', `matrix(${1/s} 0 0 ${1/s} ${-x/s} ${-y/s})`);
    const visible = [];
    for (const group of labelLayer.children) {
      const target = y + draft[group.dataset.line] * h * s;
      const onScreen = target >= 0 && target <= height;
      group.style.display = onScreen ? '' : 'none';
      if (onScreen) visible.push({group, target});
    }
    let next = inset + halfHeight;
    visible.forEach(({group,target}, index) => {
      const limit = height - inset - halfHeight - (visible.length-1-index)*gap;
      const center = Math.min(limit, Math.max(next,target));
      group.setAttribute('transform', `translate(${left} ${center})`);
      group.querySelector('path').setAttribute('d', `M ${labelWidth} 0 l 12 ${target-center}`);
      next = center + gap;
    });
  }

  function draw() {
    const setup = page === 'setup', photo = setup ? setupPhoto : live ? live.photo : task?.picture;
    el('count-zoom-tools').hidden = !photo || page === 'result';
    if (!photo || page === 'result') {
      svg.replaceChildren();
      drawnPhoto = null;
      return;
    }
    const {width:w,height:h,bounds} = photo, params = setup ? draft : task.settings;
    if (!drawnPhoto || drawnPhoto.width!==w || drawnPhoto.height!==h) {
      viewport.layout(w,h);
      svg.replaceChildren();
      svgItem(svg, 'image', {href:photo.image, width:w, height:h});
      guides = svgItem(svg,'g',{});
    } else if (drawnPhoto !== photo) {
      svg.firstElementChild.setAttribute('href',photo.image);
    }
    drawnPhoto = photo;
    guides.replaceChildren();
    const unit = 1 / (svg.getScreenCTM()?.a || 1);
    if (setup) {
      const bottom = params.mask_bottom_ratio*h;
      svgItem(guides,'rect',{width:w,height:params.mask_top_ratio*h,fill:'#173429',opacity:.16});
      svgItem(guides,'rect',{y:bottom,width:w,height:h-bottom,fill:'#173429',opacity:.16});
    } else if (highlighted != null && bounds.length) {
      const band = bounds[bounds.length-highlighted];
      if (band) svgItem(guides,'rect',{x:band[0], width:band[1]-band[0], height:h, fill:'#5cad7d', opacity:.36});
    }
    const edge = params.mask_top_ratio*h;
    const labels = rulerLabels(bounds, unit, setup ? null : highlighted);
    bounds.forEach(([a,b],i) => {
      const ch=bounds.length-i, center=(a+b)/2;
      svgItem(guides,'line',{x1:center,x2:center,y1:edge,y2:edge+4*unit,stroke:'#f0cc69','stroke-width':unit});
      if (labels.has(ch)) {
        svgItem(guides,'text',{x:center,y:Math.min(h-2*unit,edge+17*unit),'font-size':12*unit,'font-weight':600,
          'text-anchor':'middle',fill:'white',stroke:'#375042','stroke-width':2.5*unit,'paint-order':'stroke'},ch);
      }
    });
    for (const key of Object.keys(lineNames)) {
      const y = params[key]*h, crossing = key === 'tripwire_ratio';
      const group = svgItem(guides, 'g', setup ? {'data-line':key} : {});
      svgItem(group,'line',{x1:0,x2:w,y1:y,y2:y,stroke:crossing?'#d95545':'#efcd69','stroke-width':crossing?2:1.5,
        'stroke-dasharray':crossing?'none':'6 4','vector-effect':'non-scaling-stroke'});
      if (setup) {
        svgItem(group,'line',{x1:0,x2:w,y1:y,y2:y,stroke:'transparent','stroke-width':16,'vector-effect':'non-scaling-stroke'});
      }
    }
    if (setup && viewport.view) {
      // Paint the handles last so neighboring lines cannot intercept their drag targets.
      labelLayer = svgItem(guides, 'g', {class:'count-line-labels'});
      for (const [key,title] of Object.entries(lineNames)) {
        const group = svgItem(labelLayer, 'g', {'data-line':key});
        svgItem(group,'path',{});
        svgItem(group,'rect',{width:labelWidth,height:labelHeight,y:-labelHeight/2,rx:4});
        svgItem(group,'line',{x1:7,x2:7,y1:-5,y2:5});
        svgItem(group,'text',{x:15,y:0,'dominant-baseline':'central'},title);
      }
      positionLineLabels();
    }
    if (!setup) bounds.forEach(([a,b],i) => {
      const ch=bounds.length-i;
      const hit=svgItem(guides,'rect',{x:a,width:b-a,height:h,fill:'transparent','data-channel':ch});
      svgItem(hit,'title',{},'通道 '+ch+'，'+(photo.counts || task.counts)[ch-1]+' 次越线');
    });
  }

  function moveLine(key, value) {
    const limits = {mask_top_ratio:[0,draft.tripwire_ratio-.001], mask_bottom_ratio:[draft.tripwire_ratio+.001,1],
      tripwire_ratio:[draft.mask_top_ratio+.001,draft.mask_bottom_ratio-.001]};
    const [lo,hi] = limits[key];
    draft[key] = Math.max(lo,Math.min(hi,Math.round(value*1000)/1000));
    batch.changed(file.id); draw();
  }
  svg.onpointerdown = e => {
    if (!e.isPrimary || e.button !== 0 || !drawnPhoto || !viewport.view) return;
    const line = e.target.closest('[data-line]')?.dataset.line;
    gesture = {id:e.pointerId, x:e.clientX, y:e.clientY, dragged:false, line,
      lineStart:line ? draft[line] : null, startY:e.clientY,
      channel:e.target.closest('[data-channel]')?.dataset.channel};
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  };
  svg.onpointermove = e => {
    if (gesture?.id !== e.pointerId || !drawnPhoto) return;
    const dx = e.clientX - gesture.x, dy = e.clientY - gesture.y;
    if (gesture.line) {
      const delta = (e.clientY - gesture.startY) / (viewport.view.transform.s * drawnPhoto.height);
      moveLine(gesture.line, gesture.lineStart + delta);
    } else {
      if (!gesture.dragged && Math.hypot(dx,dy) < 4) return;
      if (viewport.zoomed) {
        svg.classList.add('is-panning');
        viewport.pan(dx, dy);
        if (page === 'setup') positionLineLabels();
      }
    }
    gesture.dragged = true;
    gesture.x = e.clientX;
    gesture.y = e.clientY;
  };
  function endGesture(e) {
    if (gesture?.id !== e.pointerId) return;
    const {line, dragged, channel} = gesture;
    gesture = null;
    svg.classList.remove('is-panning');
    if (line && dragged) saveDraft();
    if (channel && !dragged && page === 'count' && e.type === 'pointerup') selectChannel(Number(channel));
  }
  svg.onpointerup = svg.onpointercancel = svg.onlostpointercapture = endGesture;
  new ResizeObserver(() => {
    if (drawnPhoto) { viewport.layout(drawnPhoto.width, drawnPhoto.height); draw(); }
  }).observe(svg);

  function gridNotice(photo) {
    if (!photo) return '';
    if (!photo.bounds.length) return '首帧未识别到通道边界，请核对通道数。';
    return photo.grid_method === 'equidistant' ? '通道壁峰值不足以确定边界，当前按等距划分，请核对网格。' : '';
  }
  async function loadSetup() {
    const version = ++setupRevision;
    setupPhoto = null; setupError = ''; notice('count-message',''); render();
    try {
      const photo = await request(route('setup', {channels:draft.num_channels}));
      if (version !== setupRevision) return;
      setupPhoto = photo;
      if (page==='setup') notice('count-message',gridNotice(photo));
    } catch (error) {
      if (version !== setupRevision) return;
      setupError = error.message;
    }
    render();
  }
  function updateStartButton() {
    el('count-start').toggleAttribute('data-retry', !!setupError);
    const invalid = !channelInput.validity.valid;
    el('count-channel-error').hidden = !invalid;
    channelInput.setAttribute('aria-invalid', String(invalid));
    el('count-start').disabled = pending || (!activeRun && (invalid || !file.available || (!setupPhoto && !setupError)));
  }

  channelInput.oninput = () => { batch.changed(file.id); updateStartButton(); batch.parameters(); };
  channelInput.onchange = () => {
    const n = channelInput.valueAsNumber;
    if (!channelInput.validity.valid || n === draft.num_channels) return;
    draft.num_channels=n; saveDraft(); void loadSetup();
  };

  function selectChannel(ch) { highlighted = ch === highlighted ? null : ch; render(); }
  bars.onclick = e => {
    const button=e.target.closest('button[data-channel]');
    if (button) selectChannel(Number(button.dataset.channel));
  };
  channelFilter.onchange = () => {
    highlighted = channelFilter.value ? Number(channelFilter.value) : null;
    render();
    if (highlighted != null) bars.querySelector('[aria-pressed=true]').scrollIntoView({block:'nearest', inline:'nearest'});
  };

  function distribution() {
    const counts = live ? (live.photo?.counts || task.counts.map(()=>0)) : task.counts;
    const max = Math.max(1,...counts);
    if (bars.childElementCount !== counts.length) {
      bars.replaceChildren();
      channelFilter.replaceChildren(new Option('全部通道', ''));
      for (let ch=1; ch<=counts.length; ch++) {
        channelFilter.add(new Option('通道 ' + String(ch).padStart(2,'0'), String(ch)));
      }
      bars.style.gridTemplateColumns = 'repeat(' + counts.length + ',minmax(22px,1fr))';
      for (let ch=counts.length; ch>0; ch--) {
        const button = document.createElement('button');
        button.type='button'; button.dataset.channel=ch;
        const label=document.createElement('span');
        label.textContent=String(ch).padStart(2,'0');
        button.append(document.createElement('b'), document.createElement('i'), label);
        bars.append(button);
      }
    }
    for (const button of bars.children) {
      const ch=Number(button.dataset.channel), n=counts[ch-1];
      button.firstElementChild.textContent=String(n);
      button.children[1].style.height=Math.max(2,36*n/max)+'px';
      button.classList.toggle('count-zero',n===0);
      button.setAttribute('aria-pressed',String(ch===highlighted));
      button.title='通道 '+ch+'，'+n+' 次越线';
      button.setAttribute('aria-label',button.title);
    }
    el('cross-total').textContent=counts.reduce((sum,n)=>sum+n,0);
    el('count-scope').textContent = live ? '预览累计' : page==='result' || task.status==='complete' ? '全视频' : '已处理帧累计';
    channelFilter.value = highlighted ?? '';
    notice('cross-selected', highlighted == null ? '' : counts[highlighted-1] + ' 次越线');
    const scoped=el('cross-export-selected');
    scoped.hidden=page!=='result'||highlighted==null;
    if(!scoped.hidden){
      scoped.href=route('export',{run:task.id,channel:highlighted});
      scoped.download=file.name+'-通道'+highlighted+'.xlsx';
    }
    const link=el('cross-export');
    link.hidden=page!=='result';
    if (!link.hidden) {
      link.href=route('export',{run:task.id});
      link.download=file.name+'-通道计数.xlsx';
    }
  }

  function render() {
    syncLive();
    batch.parameters();
    const setup=page==='setup', result=page==='result';
    el('count-work').hidden=!draft;
    el('count-empty').hidden=!!draft;
    el('count-import').hidden=!!file?.persisting || file?.kind==='video' && !!file.path;
    el('count-playhead').hidden=!draft || page!=='count';
    for (const tab of tabs) {
      const name=tab.dataset.countTab;
      tab.disabled=!draft || pending || (name==='result' && task?.status!=='complete') || (name==='count' && !task);
      if (name===page) tab.setAttribute('aria-current','page'); else tab.removeAttribute('aria-current');
    }
    renderReview(result ? task : null, file, highlighted);
    el('count-layout').hidden=!draft || result;
    if (!draft) return;
    el('count-config').hidden=!setup;
    el('count-distribution').hidden=setup;
    el('count-transport').hidden=page!=='count';
    for (const row of el('channel-history-list').children) {
      const entry=row.querySelector('.history-entry'), remove=row.querySelector('.history-delete');
      entry.disabled=pending;
      if (entry.dataset.run===task?.id) entry.setAttribute('aria-current','true'); else entry.removeAttribute('aria-current');
      remove.disabled=pending || entry.dataset.run===activeRun;
      remove.title=entry.dataset.run===activeRun?'计数结束后可移除':'从列表移除，保留已保存的结果';
    }
    if (setup) {
      updateStartButton();
      el('count-start').textContent=setupError?'重新读取首帧':activeRun?'查看计数进度':'开始计数';
      const status = !file.available ? '原视频不可用' : setupError || (!setupPhoto ? '读取视频首帧…' : '');
      notice('count-photo-status', status);
    } else {
      distribution();
      if (!result) {
        const status=live && task.status==='complete' ? '计数完成 · 预览中' : states[task.status] || task.status;
        const device=task.gpu_fallback ? '已切换 CPU' : task.device?.toUpperCase();
        el('count-status').textContent=device ? status+' · '+device : status;
        el('count-status').title=task.gpu_fallback ? 'GPU 运算中断，已从首帧用 CPU 重新计算' : task.gpu_name || '';
        const button=el('count-pause');
        button.disabled=pending || (task.status==='interrupted' && !file.available);
        button.textContent=({running:'暂停',paused:'继续计数',interrupted:'继续计数',complete:'查看结果',failed:'调整设置'})[task.status];
        if (task.status==='interrupted' && activeRun && activeRun!==task.id) {
          button.textContent='查看当前计数';
          button.disabled=pending;
        }
        const progress=el('count-progress');
        const percent = task.frames > 0 ? Math.min(100,Math.floor(task.frame * 100 / task.frames)) : null;
        const processed = '已处理 ' + task.frame + (task.frames > 0 ? ' / ' + task.frames : '') + ' 帧';
        el('count-processed').textContent = percent == null ? processed : percent + '% · ' + processed;
        progress.hidden=!task.frames && !['running','paused'].includes(task.status);
        if (task.frames>0) {
          progress.max=task.frames; progress.value=task.frame;
          progress.setAttribute('aria-valuetext',processed);
        }
        else { progress.removeAttribute('value'); progress.removeAttribute('aria-valuetext'); }
        el('count-latest').hidden=!live || task.status==='complete';
        position(live ? live.frame : task.frame);
        notice('count-photo-status',(live ? live.photo : task.picture) ? '' : task.status==='running'?'等待首帧…':'暂无计数画面');
      }
    }
    draw();
  }

  function changePage(next) {
    if (next === page) return;
    page=next;
    notice('count-message',page==='setup'?gridNotice(setupPhoto):task?.error || task?.warning);
    if (page==='setup' && !setupPhoto && file.available) void loadSetup(); else render();
  }
  function chooseRun(id, channel=null) {
    clearTimeout(timer);
    run=id;
    void refresh(true,channel);
  }

  async function refresh(initial=false,selectedChannel=null) {
    clearTimeout(timer);
    const version=++refreshRevision;
    try {
      const data=await request(route('state',run?{run}:{}));
      if (version!==refreshRevision) return;
      if(selectedChannel!=null)highlighted=selectedChannel;
      else if (task?.id!==data.task?.id) highlighted=null;
      if(task && task.id===data.task?.id && task.status!=='complete' && data.task.status==='complete')void refreshComparison();
      task=data.task; activeRun=data.active_run;
      if (!draft) { draft=data.draft; channelInput.value=draft.num_channels; }
      if (initial) page=!run && batch.isEditing(file.id)?'setup':task?.status==='complete'?'result':task?'count':'setup';
      else if (page==='count' && task?.status==='complete' && !live) page='result';
      if (page!=='setup' || initial) notice('count-message',page==='setup'?'':task?.error || task?.warning);
      if (history.join()!==data.history.join()) {
        history=data.history;
        el('channel-history-list').replaceChildren(...history.map(id=>{
          const row=el('history-row-template').content.firstElementChild.cloneNode(true);
          const entry=document.createElement('button');
          entry.className='history-entry'; entry.dataset.run=id;
          entry.textContent=id.slice(4,6)+'月'+id.slice(6,8)+'日 '+id.slice(8,10)+':'+id.slice(10,12)+':'+id.slice(12,14);
          entry.onclick=()=>chooseRun(id);
          row.querySelector('.history-face').append(entry);
          const remove=row.querySelector('.history-delete');
          remove.setAttribute('aria-label','从计数记录移除 '+entry.textContent);
          remove.onclick=()=>action('remove-history',id);
          return row;
        }));
      }
      el('channel-history').hidden=!history.length;
      if (initial && page==='setup' && file.available) void loadSetup(); else render();
      if (activeRun) timer=setTimeout(refresh, activeRun===task?.id ? 500 : 1500);
    } catch (error) {
      if (version!==refreshRevision) return;
      if (!draft) {
        el('count-empty-label').textContent=error.message;
        el('count-retry').hidden=false;
      } else notice('count-message',error.message);
      if (activeRun) timer=setTimeout(refresh,1500);
    }
  }

  async function action(name,targetRun=task?.id) {
    if (pending) return;
    const version=generation;
    pending=true; clearTimeout(timer); refreshRevision++; render();
    try {
      await writes;
      if (version!==generation) return;
      const data=await post(file.id,name,name==='remove-history'?{run:targetRun}:{settings:draft,run:targetRun});
      if (version!==generation) return;
      if (name==='remove-history') {
        const selected=targetRun===task?.id || targetRun===run;
        if (selected) run=null;
        await refresh(selected);
        return;
      }
      if (task?.id!==data.task.id) highlighted=null;
      if (name==='start') newPreviewRun=data.task.id;
      task=data.task; run=task.id; page='count';
      await refresh();
    } catch (error) {
      if (version===generation) {
        notice('count-message',error.message);
        if (activeRun) timer=setTimeout(refresh,1500);
      }
    } finally {
      if (version===generation) { pending=false; render(); }
    }
  }
  el('count-import').onclick=()=>el('import-files').click();
  el('count-retry').onclick=()=>{
    el('count-retry').hidden=true;
    el('count-empty-label').textContent='读取计数记录…';
    void refresh(true);
  };
  el('count-start').onclick=()=>{
    if (setupError) void loadSetup();
    else if (activeRun) chooseRun(activeRun);
    else void action('start');
  };
  el('count-latest').onclick=()=>{
    // Rejoin at the checkpoint; discard any older preview frame still decoding.
    stopLive();
    render();
  };
  el('count-pause').onclick=()=>{
    if (task.status==='failed') changePage('setup');
    else if (task.status==='complete') changePage('result');
    else if (activeRun && activeRun!==task.id) chooseRun(activeRun);
    else void action(task.status==='running'?'pause':'resume');
  };
  for (const tab of tabs) tab.onclick=()=>changePage(tab.dataset.countTab);

  return {
    batch,
    flush:()=>writes,
    select(item) {
      if (file?.id===item?.id && file?.name===item?.name && file?.available===item?.available && file?.path===item?.path) return;
      file=item?{...item}:null;
      generation++; setupRevision++; refreshRevision++; clearTimeout(timer);
      task=draft=run=activeRun=setupPhoto=highlighted=newPreviewRun=null;
      viewport.clear(); gesture=drawnPhoto=null;
      svg.classList.remove('is-panning');
      page='setup'; history=[]; pending=false; setupError='';
      el('channel-history-list').replaceChildren(); el('channel-history').hidden=true;
      el('channel-name').textContent=file?.name||'工作台';
      const video=file?.kind==='video' && file.path;
      notice('count-message','');
      el('count-empty-label').textContent=video?'读取计数记录…':file?.persisting?'正在保存视频':file ? '当前文件不能用于计数，请选择或导入视频。' : '从左侧选择视频，或导入要计数的视频。';
      el('count-retry').hidden=true;
      render();
      if (video) {
        const version=generation;
        writes.catch(()=>{}).then(()=>{ if (version===generation && !run) void refresh(true); });
      }
    }
  };
};

(function(root) {
  'use strict';
  function pathKey(value) {
    const path = String(value || '').replace(/\\/g, '/').replace(/\/+$/, '');
    return /^[a-z]:\//i.test(path) || path.startsWith('//') ? path.toLowerCase() : path;
  }
  function runsForFile(file, runs) {
    if (!file?.path) return [];
    return runs.filter(run => pathKey(run.video) === pathKey(file.path))
      .sort((a,b) => b.updated-a.updated);
  }
  const layouts = new WeakMap(), controllers = new Set(), docked = new WeakMap();
  let store = null;
  function createStore() {
    const storageKey = 'worm-studio-layout-v1';
    let cache = {}, pending = {}, token = null, resetPending = false, inFlight = null, timer;
    let status = '', stopped = false, epoch = 0;
    const subscribers = new Set();
    try { cache = JSON.parse(localStorage.getItem(storageKey) || '{}') || {}; } catch {}
    const local = () => { try { localStorage.setItem(storageKey, JSON.stringify(cache)); } catch {} };
    const notify = kind => subscribers.forEach(fn => fn(kind));
    const feedback = message => {
      status = message;
      window.dispatchEvent(new CustomEvent('workspace-layout-status', {detail:message}));
    };
    async function hydrate() {
      if (location.protocol === 'file:') return;
      try {
        const response = await fetch('/api/layout', {cache:'no-store', signal:AbortSignal.timeout(8000)});
        const data = await response.json();token = data.token;
        if (!response.ok) throw Error(data.error || '请重启软件以启用布局保存');
        const remote = data.exists ? data.panels : cache;
        if (!data.exists) pending = {...cache,...pending};
        cache = resetPending ? {...pending} : {...remote,...pending};
        local();notify('restore');feedback('');
      } catch (error) { feedback('布局暂存浏览器 · '+error.message); }
    }
    const ready = hydrate();
    async function flush() {
      await ready;
      if (!token && !stopped) await hydrate();
      if (inFlight) { await inFlight; if (!status) return flush(); return; }
      if (!token || (!resetPending && !Object.keys(pending).length)) return;
      const batch = pending, resetting = resetPending, revision = epoch;pending = {};resetPending = false;
      inFlight = (async () => {
        try {
          const response = await fetch('/api/layout', {method:'POST', keepalive:true,
            headers:{'Content-Type':'application/json','X-Annotation-Token':token},
            body:JSON.stringify({panels:batch, reset:resetting}), signal:AbortSignal.timeout(8000)});
          const result = await response.json();if (!response.ok) throw Error(result.error || '保存失败');
          feedback('');
        } catch (error) {
          if(revision!==epoch)return;
          pending = {...batch,...pending};resetPending ||= resetting;
          feedback('布局尚未保存到本机 · '+error.message);
          if (!stopped) timer = setTimeout(flush, 5000);
        }
      })();
      await inFlight;inFlight = null;
      if (!status && (Object.keys(pending).length || resetPending)) return flush();
    }
    window.addEventListener('pagehide', () => {stopped=true;clearTimeout(timer);void flush();});
    void ready.then(flush);
    return {
      read:key=>({...cache[key]}), flush,
      subscribe:fn=>{subscribers.add(fn);return ()=>subscribers.delete(fn);},
      write(key,value) { cache[key]={...value};pending[key]={...value};local();clearTimeout(timer);timer=setTimeout(flush,180); },
      reset() { epoch++;cache={};pending={};resetPending=true;local();notify('reset');void flush(); },
      get status() { return status; }
    };
  }
  function readLayout(key) { return store?.read(key) || {}; }
  function writeLayout(key, value) { store?.write(key,value); }
  function setPanelOpen(panel, open) {
    if (!panel) return;
    const dock = docked.get(panel);
    if (dock) { dock.select(panel,open); return; }
    panel.classList.toggle('is-collapsed', !open);
    const content = panel.querySelector(':scope > .floating-content');
    if (content) content.hidden = !open;
    const toggle = panel.querySelector('.floating-toggle');
    if (toggle) {
      toggle.textContent = open ? '−' : '+';
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', (open ? '收起' : '展开') + panel.dataset.floatingPanel);
    }
    panel.dispatchEvent(new Event('panelresize'));
  }
  function panelKey(panel) {
    const page = document.body.classList.contains('annotation-page') ? 'annotation' :
      document.body.classList.contains('processing-page') ? 'processing' : 'studio';
    return page + ':' + (panel.id || panel.dataset.floatingPanel || panel.dataset.resizable);
  }
  function finite(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100000; }
  function makeLayout(panel, {floating = false, axis = 'both', minWidth = 200, minHeight = 100, key = panelKey(panel), bounds, applySize} = {}) {
    if (layouts.has(panel)) return layouts.get(panel);
    const saved = readLayout(key), preferred = {};
    for (const name of ['x','y','width','height']) if (finite(saved[name])) preferred[name] = saved[name];
    if (typeof saved.collapsed === 'boolean') preferred.collapsed = saved.collapsed;
    let gesture = null;
    const originalStyle = panel.getAttribute('style');
    const rectBounds = () => bounds ? bounds() : {width: panel.offsetParent?.clientWidth || 0, height: panel.offsetParent?.clientHeight || 0};
    function apply() {
      if (docked.has(panel) || !panel.offsetWidth || !panel.offsetHeight) return;
      const area = rectBounds(); if (!area.width || !area.height) return;
      const collapsed = panel.classList.contains('is-collapsed');
      if (applySize) applySize(preferred, area);
      else if (!collapsed) {
        if (finite(preferred.width) && axis !== 'y') panel.style.width = Math.max(Math.min(minWidth, area.width-16), Math.min(area.width-16, preferred.width)) + 'px';
        if (finite(preferred.height) && axis !== 'x') {
          panel.style.height = Math.max(Math.min(minHeight, area.height-16), Math.min(area.height-16, preferred.height)) + 'px';
          panel.style.maxHeight = Math.max(40, area.height-16) + 'px';
        }
      }
      if (floating && (finite(preferred.x) || finite(preferred.y))) {
        // Retain the preferred desktop geometry when temporarily clamped on small screens.
        const x = finite(preferred.x) ? preferred.x : panel.offsetLeft;
        const y = finite(preferred.y) ? preferred.y : panel.offsetTop;
        panel.style.left = Math.max(8, Math.min(Math.max(8, area.width-panel.offsetWidth-8), x)) + 'px';
        panel.style.top = Math.max(8, Math.min(Math.max(8, area.height-panel.offsetHeight-8), y)) + 'px';
        panel.style.right = panel.style.bottom = 'auto';
      }
    }
    function save() { writeLayout(key, preferred); }
    function restore(value = {}) {
      if (gesture) return;
      Object.keys(preferred).forEach(k => delete preferred[k]);
      for (const name of ['x','y','width','height']) if (finite(value[name])) preferred[name]=value[name];
      if (typeof value.collapsed === 'boolean') preferred.collapsed=value.collapsed;
      if (originalStyle === null) panel.removeAttribute('style'); else panel.setAttribute('style',originalStyle);
      panel.dispatchEvent(new Event('layoutdefault'));
      if (floating && !docked.has(panel)) setPanelOpen(panel, typeof preferred.collapsed==='boolean' ? !preferred.collapsed : panel.dataset.collapsed!=='true');
      apply();
    }
    function reset(persist = true) {
      Object.keys(preferred).forEach(k => delete preferred[k]);
      if (originalStyle === null) panel.removeAttribute('style'); else panel.setAttribute('style', originalStyle);
      if (floating) setPanelOpen(panel, panel.dataset.collapsed !== 'true');
      panel.dispatchEvent(new Event('layoutdefault')); if(persist!==false)save(); apply();
    }
    function finish() {
      if (!gesture) return;
      gesture = null; document.documentElement.classList.remove('layout-interacting'); save();
    }
    function start(event, mode) {
      if (docked.has(panel) || event.button !== 0 || (mode !== 'move' && panel.classList.contains('is-collapsed'))) return;
      const area = rectBounds(); if (!area.width || !area.height) return;
      gesture = {mode,px:event.clientX,py:event.clientY,x:panel.offsetLeft,y:panel.offsetTop,width:panel.offsetWidth,height:panel.offsetHeight};
      event.currentTarget.setPointerCapture(event.pointerId); event.preventDefault(); event.stopPropagation();
      document.documentElement.classList.add('layout-interacting');
    }
    function move(event) {
      if (!gesture) return;
      const g = gesture, dx = event.clientX-g.px, dy = event.clientY-g.py, area = rectBounds();
      if (g.mode === 'move') {
        preferred.x = Math.max(8, Math.min(Math.max(8, area.width-g.width-8), g.x+dx));
        preferred.y = Math.max(8, Math.min(Math.max(8, area.height-g.height-8), g.y+dy));
      } else {
        const west = g.mode.includes('w'), north = g.mode.includes('n');
        const horizontal = /e|w/.test(g.mode), vertical = /n|s/.test(g.mode);
        if (horizontal) {
          const max = floating ? (west ? g.x+g.width-8 : area.width-g.x-8) : area.width-16;
          preferred.width = Math.max(Math.min(minWidth,max), Math.min(max,g.width+(west?-dx:dx)));
          if (floating) preferred.x = west ? g.x+g.width-preferred.width : g.x;
        }
        if (vertical) {
          const max = floating ? (north ? g.y+g.height-8 : area.height-g.y-8) : area.height-16;
          preferred.height = Math.max(Math.min(minHeight,max), Math.min(max,g.height+(north?-dy:dy)));
          if (floating) preferred.y = north ? g.y+g.height-preferred.height : g.y;
        }
      }
      apply();
    }
    function bind(handle, mode) {
      handle.addEventListener('pointerdown', event => start(event,mode));
      handle.addEventListener('pointermove', move);
      for (const type of ['pointerup','pointercancel','lostpointercapture']) handle.addEventListener(type,finish);
      handle.addEventListener('dblclick', reset);
      handle.addEventListener('keydown', event => {
        if (event.key === 'Home') {event.preventDefault();reset();return;}
        const delta = {ArrowLeft:[-16,0],ArrowRight:[16,0],ArrowUp:[0,-16],ArrowDown:[0,16]}[event.key];
        if (!delta) return; event.preventDefault();event.stopPropagation();
        if (mode === 'move') { preferred.x = Math.max(8,panel.offsetLeft+delta[0]); preferred.y = Math.max(8,panel.offsetTop+delta[1]); }
        else {
          if (axis !== 'y' && delta[0]) preferred.width = Math.max(minWidth,panel.offsetWidth+delta[0]);
          if (axis !== 'x' && delta[1]) preferred.height = Math.max(minHeight,panel.offsetHeight+delta[1]);
        }
        apply();save();
      });
    }
    const directions = floating ? ['n','e','s','w','ne','se','sw','nw'] : axis === 'x' ? ['e'] : axis === 'y' ? [panel.dataset.resizeEdge || 's'] : ['e','s','se'];
    for (const direction of directions) {
      const handle = document.createElement('button');handle.type='button';handle.className='resize-handle resize-'+direction;
      handle.dataset.resize = direction;
      handle.setAttribute('aria-label', '调整'+(panel.dataset.floatingPanel || panel.dataset.resizable || panel.dataset.resizeLabel || '文件栏')+'大小');
      handle.title = '拖动调整大小 · 双击恢复默认 · 方向键微调';
      handle.tabIndex = direction === 'se' || directions.length===1 ? 0 : -1;
      if (direction === 'se') handle.textContent='◢';
      (panel.tagName === 'DETAILS' ? panel.querySelector('summary') : panel).append(handle);bind(handle,direction);
    }
    const observer = new ResizeObserver(apply);observer.observe(panel);observer.observe(panel.parentElement);
    window.addEventListener('resize',apply);panel.addEventListener('panelresize',apply);
    const result = {key,apply,save,reset,restore,bind,preferred};controllers.add(result);layouts.set(panel,result);apply();return result;
  }
  function mountPanels(doc = document) {
    doc.querySelectorAll('[data-floating-panel]').forEach((panel, index) => {
      if (panel.querySelector(':scope > .floating-bar')) return;
      const content = doc.createElement('div'); content.className = 'floating-content';
      content.id = (panel.id || 'floating-panel-' + index) + '-content';
      content.append(...panel.childNodes); panel.append(content);
      const bar = doc.createElement('div'); bar.className = 'floating-bar';
      const grip = doc.createElement('button'); grip.type = 'button'; grip.className = 'floating-grip';
      grip.textContent = panel.dataset.floatingPanel;grip.title = '拖动移动 · 方向键微调';
      const swap = doc.createElement('button'); swap.type = 'button'; swap.className = 'floating-swap';
      swap.textContent = '⇄'; swap.title = '移到画面另一侧'; swap.setAttribute('aria-label', swap.title);
      const reset = doc.createElement('button');reset.type='button';reset.className='floating-reset';
      reset.textContent='↺';reset.title='恢复此面板的默认大小和位置';reset.setAttribute('aria-label',reset.title);
      const toggle = doc.createElement('button'); toggle.type = 'button'; toggle.className = 'floating-toggle';
      toggle.setAttribute('aria-controls', content.id);bar.append(grip,swap,reset,toggle);panel.prepend(bar);
      const layout = makeLayout(panel,{floating:true});layout.bind(grip,'move');
      swap.onclick = () => {
        const area=panel.offsetParent;if(!area)return;
        layout.preferred.x=panel.offsetLeft<area.clientWidth/2?area.clientWidth-panel.offsetWidth-12:12;
        layout.preferred.y=panel.offsetTop;layout.apply();layout.save();
      };
      reset.onclick = layout.reset;
      toggle.onclick = () => {
        const open = panel.classList.contains('is-collapsed');setPanelOpen(panel,open);
        layout.preferred.collapsed=!open;layout.save();
      };
      setPanelOpen(panel, typeof layout.preferred.collapsed==='boolean' ? !layout.preferred.collapsed : panel.dataset.collapsed!=='true');
    });
    doc.querySelectorAll('[data-resizable]').forEach(panel => {
      makeLayout(panel,{axis:panel.dataset.resizeAxis || 'both',minWidth:160,minHeight:panel.dataset.resizeAxis==='y'?280:36,
        bounds:()=>({width:panel.parentElement.clientWidth,height:Math.max(240,document.documentElement.clientHeight-120)})});
    });
    const footer = doc.querySelector('.annotation-footer');
    if (footer) makeLayout(footer,{axis:'y',key:'annotation:footer',minHeight:48,
      bounds:()=>({width:innerWidth,height:Math.max(96,innerHeight-240)}),
      applySize:pref=>{if(finite(pref.height))footer.style.height=Math.max(48,Math.min(innerHeight-256,pref.height))+'px';}});
    doc.querySelectorAll('[data-reset-layout]').forEach(button => button.onclick = resetLayout);
    const library = doc.getElementById('library');
    if (library) {
      makeLayout(library,{axis:'x',key:'studio:library',minWidth:160,
        bounds:()=>({width:Math.min(500,Math.max(176,innerWidth-380))+16,height:innerHeight}),
        applySize:pref=>{if(finite(pref.width))doc.body.style.setProperty('--library-width',Math.max(160,Math.min(500,innerWidth-100,pref.width))+'px');}});
      library.addEventListener('layoutdefault',()=>doc.body.style.removeProperty('--library-width'));
    }
  }
  function mountDock(doc) {
    const panels=[...doc.querySelectorAll('[data-floating-panel]')];
    const host=doc.querySelector('.annotation-page .workspace,.processing-content,.results-workspace');
    if (!host || !panels.length || host.dataset.dockMounted) return;
    host.dataset.dockMounted='true';
    const destination=doc.querySelector('.result-player') || host;
    const dock=doc.createElement('section');dock.className='panel-dock';dock.hidden=true;dock.setAttribute('aria-label','画面工具区');
    const bar=doc.createElement('div');bar.className='panel-dock-bar';
    const tabs=doc.createElement('div');tabs.className='panel-dock-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','切换工具');
    const toggle=doc.createElement('button');toggle.type='button';toggle.className='dock-toggle';
    const body=doc.createElement('div');body.className='panel-dock-body';
    bar.append(tabs,toggle);dock.append(bar,body);
    const preview=doc.querySelector('.live-preview');if(preview)preview.after(dock);else destination.append(dock);
    const places=panels.map(panel=>{const mark=doc.createComment('floating panel position');panel.before(mark);return mark;});
    let compact=false, active=panels[0], expanded=true;
    const buttons=panels.map((panel,index)=>{
      const button=doc.createElement('button');button.type='button';button.textContent=panel.dataset.floatingPanel;button.setAttribute('role','tab');
      button.id='dock-tab-'+index;panel.id ||= 'dock-panel-'+index;button.setAttribute('aria-controls',panel.id);
      button.onclick=()=>select(panel,true);
      button.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?panels.length-1:(index+(event.key==='ArrowRight'?1:-1)+panels.length)%panels.length;select(panels[next],true);buttons[next].focus();};
      tabs.append(button);return button;
    });
    function select(panel,open=true) {
      active=panel;expanded=open;dock.classList.toggle('is-folded',!expanded);body.hidden=!expanded;
      panels.forEach((item,index)=>{
        item.hidden=item!==active || !expanded;
        item.querySelector('.floating-content').hidden=false;
        buttons[index].setAttribute('aria-selected',String(item===active));buttons[index].tabIndex=item===active?0:-1;
      });
      toggle.textContent=expanded?'收起':'展开';toggle.setAttribute('aria-expanded',String(expanded));
      toggle.setAttribute('aria-label',expanded?'收起工具，专注画面':'展开画面工具');
    }
    toggle.onclick=()=>select(active,!expanded);
    function update() {
      // Observe the actual workspace width, including the space occupied by the file library.
      const next=host.clientWidth>0 && host.clientWidth<=980;
      if(next===compact)return;
      compact=next;host.classList.toggle('compact-workspace',compact);dock.hidden=!compact;
      if(compact){
        panels.forEach((panel,index)=>{docked.set(panel,{select});body.append(panel);panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',buttons[index].id);});
        select(active,expanded);
      }else{
        panels.forEach((panel,index)=>{docked.delete(panel);places[index].after(panel);panel.hidden=false;panel.removeAttribute('role');panel.removeAttribute('aria-labelledby');const layout=layouts.get(panel);layout.restore(readLayout(layout.key));});
      }
    }
    const unsubscribe=store.subscribe(kind=>{if(kind==='reset'&&compact)select(panels[0],true);});window.addEventListener('pagehide',unsubscribe);
    new ResizeObserver(update).observe(host);window.addEventListener('resize',update);update();
  }
  function resetLayout() { store?.reset(); }
  function setFocusMode(value) {
    document.body.classList.toggle('canvas-focus',value);
    const button=document.getElementById('focus-canvas');
    if(button){button.textContent=value?'显示工具':'专注画面';button.setAttribute('aria-pressed',String(value));}
    document.querySelectorAll('iframe').forEach(frame=>{try{frame.contentWindow.WorkspaceLinks?.setFocusMode(value);}catch{}});
  }
  const api = {pathKey, runsForFile, setPanelOpen, mountPanels, resetLayout, setFocusMode, flushLayout:()=>store?.flush()};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else {
    try { store = window.parent!==window ? window.parent.WorkspaceLinks?.layoutStore : null; } catch {}
    store ||= createStore();api.layoutStore=store;root.WorkspaceLinks=api;
    const unsubscribe=store.subscribe(kind=>controllers.forEach(item=>kind==='reset'?item.reset(false):item.restore(readLayout(item.key))));
    window.addEventListener('pagehide',unsubscribe);
    const mount=()=>{mountPanels();mountDock(document);
      const toolbar=document.querySelector('.viewer>.viewer-toolbar'),more=document.querySelector('.annotation-more');
      if(toolbar&&more)new ResizeObserver(()=>{more.style.top=(toolbar.offsetTop+toolbar.offsetHeight+8)+'px';}).observe(toolbar);
      const focus=document.getElementById('focus-canvas');if(focus)focus.onclick=()=>setFocusMode(!document.body.classList.contains('canvas-focus'));
      const feedback=document.getElementById('layout-feedback');
      if(feedback){const update=()=>{feedback.hidden=!store.status;feedback.querySelector('span').textContent=store.status;};window.addEventListener('workspace-layout-status',update);feedback.querySelector('button').onclick=()=>void store.flush();update();}
    };
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();
  }
})(globalThis);

'use strict';
(() => {
  const el = id => document.getElementById(id);
  let run = null, cursor = 0, newest = 0, paused = false, steps = 0, jump = false;
  let generation = 0, disposed = false, state = null, timer, controller;
  let displayed = null, editing = false;
  const terminal = new Set(['complete', 'failed', 'cancelled']);
  const views = document.querySelector('.live-views');
  const screen = document.querySelector('.live-screen');
  const inspector = el('live-focus');
  let detailImage = null;

  function layout() {
    const image = el('live-image');
    const ratio = image.naturalWidth ? image.naturalWidth / image.naturalHeight : 1;
    const width = views.clientWidth - 24;
    const height = views.clientHeight - 8;
    if (width <= 0 || height <= 0) return;
    const cardWidth = inspector.open ? Math.min(300, Math.max(200, width * .26)) : 152;
    views.style.setProperty('--focus-width', cardWidth + 'px');
    views.classList.toggle('stacked', width < 440 || width - height * ratio < cardWidth + 12);
    const fittedWidth = Math.min(screen.clientWidth, screen.clientHeight * ratio);
    el('live-picture').style.width = fittedWidth + 'px';
    el('live-picture').style.height = fittedWidth / ratio + 'px';
    el('live-focus-box').hidden = !inspector.open || !detailImage;
  }

  function paintFocus() {
    const canvas = el('focus-image');
    canvas.hidden = !detailImage;
    el('focus-empty').hidden = !!detailImage;
    el('focus-outline').disabled = !detailImage;
    if (!detailImage || !inspector.open) return;
    const width = detailImage.naturalWidth / 2, height = detailImage.naturalHeight;
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    const left = el('focus-outline').checked ? width : 0;
    canvas.getContext('2d').drawImage(detailImage, left, 0, width, height, 0, 0, width, height);
  }

  function showFocus(item, image) {
    detailImage = image;
    inspector.hidden = !item;
    document.querySelector('.live-toolbar').hidden = !item;
    el('focus-frame').textContent = item ? `源帧 ${item.frame}` : '—';
    if (image) {
      el('focus-frame').textContent += ` · 线虫 ${item.focus.worm}`;
      const [x, y, w, h] = item.focus.rect;
      Object.assign(el('live-focus-box').style, {
        left: x*100 + '%', top: y*100 + '%', width: w*100 + '%', height: h*100 + '%'
      });
    }
    layout();
    paintFocus();
  }

  const resize = new ResizeObserver(layout);
  resize.observe(views);
  resize.observe(screen);
  inspector.addEventListener('toggle', () => { layout(); paintFocus(); });
  el('focus-outline').addEventListener('change', paintFocus);

  function controls() {
    el('live-pause').textContent = paused ? '继续播放' : '暂停画面';
    el('live-pause').setAttribute('aria-pressed', String(paused));
    el('live-pause').disabled = editing;
    el('live-next').disabled = editing || !paused || cursor >= newest;
    el('live-latest').disabled = editing || cursor >= newest;
    const done=terminal.has(state?.status);
    const activeStage = state?.stages?.[state.active];
    el('live-stage').hidden = !displayed?.stage || (!!activeStage &&
      AnalysisPresentation.text(displayed.stage) === AnalysisPresentation.stage(activeStage));
    const emptyDone=done && !newest && !displayed;
    document.querySelector('.live-preview').hidden=emptyDone;
    document.body.classList.toggle('no-preview',emptyDone);
    el('live-pause').disabled=editing || !newest;
    el('live-speed').disabled=!newest;
    const playbackStatus = el('live-playback-status');
    const replaying = done && newest > cursor;
    playbackStatus.textContent = replaying ? `剩余 ${newest - cursor} 张预览` :
      paused && !done ? '预览已暂停，后台继续计算' : '';
    playbackStatus.hidden = !playbackStatus.textContent;
    const edit = el('live-edit-mask');
    edit.hidden = !paused || !state?.can_edit_preview_masks;
    edit.disabled = editing || !displayed;
    edit.textContent = editing ? '正在停止计算…' : '修改当前帧 Mask';
    el('live-edit-note').hidden = !paused || !state?.can_edit_preview_masks;
  }
  function reset(next) {
    run = next; cursor = newest = steps = 0; jump = false; generation++;
    displayed = null;
    controller?.abort();
    el('live-picture').hidden = true; el('live-image').removeAttribute('src');
    showFocus(null, null);
    el('live-empty').hidden = false;
    el('preview-retry').hidden = true;
    el('live-frame').textContent = '';
    el('live-stage').textContent = '处理预览';
    el('live-detail').textContent = '';
  }
  async function request(url, type = 'json') {
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw Error('预览暂不可用');
      return await response[type]();
    } finally { clearTimeout(timeout); }
  }
  async function loadImage(url) {
    const blobUrl = URL.createObjectURL(await request(url, 'blob'));
    try {
      const image = new Image();
      image.src = blobUrl;
      await image.decode();
      return image;
    } finally { URL.revokeObjectURL(blobUrl); }
  }

  async function pump() {
    if (disposed) return;
    const version = generation, currentRun = run;
    let delay = 350;
    try {
      if (!currentRun || document.hidden) return;
      const base = '/api/live-preview?run=' + encodeURIComponent(currentRun);
      const batch = await request(base + '&after=' + cursor);
      if (generation !== version || disposed) return;
      newest = batch.latest;
      if (jump) { cursor = Math.max(0, newest-1); jump = false; steps = Math.max(1, steps); delay = 0; return; }
      if (!batch.frames.length || paused) el('preview-retry').hidden = true;
      controls();
      if (paused && steps === 0) return;
      for (const item of batch.frames) {
        if (generation !== version || disposed || document.hidden || (paused && steps === 0) || jump) break;
        const frameUrl = base + '&sequence=' + item.sequence;
        const image = await loadImage(frameUrl);
        let focus = null;
        if (item.focus && generation === version && !disposed) {
          try { focus = await loadImage(frameUrl + '&view=focus'); }
          catch { /* A missing detail tile must not stall the main preview. */ }
        }
        if (generation !== version || disposed || (paused && steps === 0) || jump) break;
        // Commit both decoded views together, including reverse and repeated source frames.
        image.id = 'live-image';
        image.alt = AnalysisPresentation.text(`${item.stage}，源帧 ${item.frame}，${item.detail}`);
        el('live-image').replaceWith(image);
        el('live-picture').hidden = false; el('live-empty').hidden = true;
        el('live-frame').textContent = `源帧 ${item.frame}`;
        el('live-stage').textContent = AnalysisPresentation.text(item.stage || '处理预览');
        el('live-detail').textContent = AnalysisPresentation.text(item.detail);
        el('preview-retry').hidden = true;
        displayed = item;
        showFocus(item, focus);
        cursor = item.sequence; steps = Math.max(0, steps-1); controls();
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        await new Promise(resolve => setTimeout(resolve, 1000 / Number(el('live-speed').value)));
      }
      delay = cursor < newest && !paused ? 0 : 350;
    } catch (error) {
      if (generation === version && !disposed) el('preview-retry').hidden = false;
      delay = 1000;
    } finally { if (!disposed) timer = setTimeout(pump, delay); }
  }
  el('live-pause').onclick = () => { paused = !paused; steps = 0; controls(); };
  el('live-next').onclick = () => { steps = 1; };
  el('live-latest').onclick = () => { jump = true; };
  el('live-edit-mask').onclick = async () => {
    if (!paused || !displayed || editing || !state?.can_edit_preview_masks) return;
    editing = true; steps = 0; jump = false; controls();
    window.WorkspaceLinks?.setPanelOpen(document.querySelector('.live-data-panel'), true);
    const body = JSON.stringify({run, sequence: displayed.sequence}), token = state.token;
    const note = el('live-edit-note');
    note.textContent = `正在停止后台计算，准备源帧 ${displayed.frame} 的掩膜…`;
    try {
      for (let attempt = 0; attempt < 75 && !disposed; attempt++) {
        const response = await fetch('/api/edit-preview-mask', {method:'POST',
          headers:{'Content-Type':'application/json','X-Annotation-Token':token}, body,
          signal:AbortSignal.timeout(12000)});
        const result = await response.json();
        if (!response.ok) throw Error(result.error || '无法打开掩膜修正');
        if (result.annotation_url) { window.location.assign(result.annotation_url); return; }
        if (!result.waiting) throw Error('掩膜修正会话未就绪');
        await new Promise(resolve => setTimeout(resolve, 400));
      }
      throw Error('后台仍在停止，请稍后再次点击修改');
    } catch (error) {
      note.textContent = `打开失败：${error.message}。画面已保留，可重试。`;
    } finally { editing = false; controls(); }
  };
  window.LivePreview = {
    update(data) {
      state = data;
      if (run !== (data.preview_run || null)) reset(data.preview_run || null);
      controls();
    },
    stop() { disposed = true; resize.disconnect(); controller?.abort(); clearTimeout(timer); }
  };
  window.addEventListener('pagehide', () => window.LivePreview.stop());
  pump();
})();

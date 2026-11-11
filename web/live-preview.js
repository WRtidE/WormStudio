'use strict';
(() => {
  const el = id => document.getElementById(id);
  let run = null, cursor = 0, newest = 0, paused = false, steps = 0, jump = false;
  let generation = 0, disposed = false, state = null, timer, controller;
  let displayed = null, editing = false;
  const terminal = new Set(['complete', 'failed', 'cancelled']);
  function controls() {
    el('live-pause').textContent = paused ? '继续播放' : '暂停画面';
    el('live-pause').setAttribute('aria-pressed', String(paused));
    el('live-pause').disabled = editing;
    el('live-next').disabled = editing || !paused || cursor >= newest;
    el('live-latest').disabled = editing || cursor >= newest;
    const done=terminal.has(state?.status);
    const emptyDone=done && !newest && !displayed;
    document.querySelector('.live-preview').hidden=emptyDone;
    document.body.classList.toggle('no-preview',emptyDone);
    el('live-pause').disabled=editing || !newest;
    el('live-speed').disabled=!newest;
    const backend=state?.status==='complete'?'计算已完成':done?'计算已停止':'后台计算继续';
    el('live-playback-status').textContent=paused?`画面已暂停 · ${backend}`:done?`${backend}${newest>cursor?' · 正在回放剩余预览':''}`:'按处理顺序显示 · 阶段切换时源帧可能重复';
    const edit = el('live-edit-mask');
    edit.hidden = !paused || !state?.can_edit_preview_masks;
    edit.disabled = editing || !displayed;
    edit.textContent = editing ? '正在停止计算…' : '修改当前帧 Mask';
    el('live-edit-note').hidden = !paused || !state?.can_edit_preview_masks;
    el('live-backlog').textContent = newest > cursor ? `待播放 ${newest-cursor} 张预览` :
      terminal.has(state?.status) ? '已显示全部已生成画面' : '等待下一帧';
  }
  function reset(next) {
    run = next; cursor = newest = steps = 0; jump = false; generation++;
    displayed = null;
    controller?.abort();
    el('live-image').hidden = true; el('live-image').removeAttribute('src');
    el('live-empty').hidden = false;
    el('live-frame').textContent = '等待处理画面';
    el('live-stage').textContent = '处理预览';
    el('live-detail').textContent = '每帧按实际处理顺序显示，暂停画面不会暂停计算。';
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
      controls();
      if (paused && steps === 0) return;
      for (const item of batch.frames) {
        if (generation !== version || disposed || document.hidden || (paused && steps === 0) || jump) break;
        const blob = await request(base + '&sequence=' + item.sequence, 'blob');
        const url = URL.createObjectURL(blob);
        try {
          const image = new Image(); image.src = url; await image.decode();
          if (generation !== version || disposed || (paused && steps === 0) || jump) break;
          // Keep a single bounded image, and advance only after a successful decode.
          el('live-image').src = url; el('live-image').hidden = false; el('live-empty').hidden = true;
          el('live-frame').textContent = `源帧 ${item.frame} / ${item.total-1}`;
          el('live-stage').textContent = AnalysisPresentation.text(item.stage || '处理预览');
          el('live-detail').textContent = AnalysisPresentation.text(item.detail);
          el('live-image').alt = AnalysisPresentation.text(`${item.stage}，源帧 ${item.frame}，${item.detail}`);
          displayed = item;
          cursor = item.sequence; steps = Math.max(0, steps-1); controls();
          // Give every decoded frame an actual paint opportunity (including 60 Hz screens).
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          await new Promise(resolve => setTimeout(resolve, 1000 / Number(el('live-speed').value)));
        } finally { URL.revokeObjectURL(url); }
      }
      delay = cursor < newest && !paused ? 0 : 350;
    } catch (error) {
      if (generation === version && !disposed) el('live-backlog').textContent = '预览连接中断，正在重试';
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
      el('live-empty').textContent = data.preview_run ?
        (terminal.has(data.status) ? '本次任务未生成预览画面（可能复用了已有结果）。' : '等待第一帧 · 模型加载及批量计算期间会保留上一帧') :
        (terminal.has(data.status) ? '处理已结束，可前往结果页查看。' : '开始处理后，这里会逐帧显示识别画面。');
      controls();
    },
    stop() { disposed = true; controller?.abort(); clearTimeout(timer); }
  };
  window.addEventListener('pagehide', () => window.LivePreview.stop());
  pump();
})();

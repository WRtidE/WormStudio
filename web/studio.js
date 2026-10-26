'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const localFile = window.location.protocol === 'file:';
  const files = [];
  const terminal = ['ready', 'complete', 'failed', 'cancelled'];
  const videoExtensions = /\.(mp4|avi|mov|mkv|m4v|mpg|mpeg|wmv|webm)$/i;
  const imageExtensions = /\.(png|jpe?g|gif|webp|bmp|tiff?)$/i;
  let selectedId = null, analysisId = null, filter = 'all', view = 'preview';
  let progress = null, busy = false, connected = false, initialized = false;
  let currentSample = '', mediaRevision = 0, dragging = 0;
  const frame = $('#analysis-frame');

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name); svg.append(use); return svg;
  }
  const selected = () => files.find(file => file.id === selectedId);
  const canStart = () => connected && progress && terminal.includes(progress.status) && !busy;
  function sizeLabel(size) {
    if (size == null) return '本机文件';
    if (size < 1024) return size + ' B';
    if (size < 1024 ** 2) return (size / 1024).toFixed(1) + ' KB';
    if (size < 1024 ** 3) return (size / 1024 ** 2).toFixed(1) + ' MB';
    return (size / 1024 ** 3).toFixed(2) + ' GB';
  }
  function durationLabel(seconds) {
    if (!Number.isFinite(seconds)) return '—';
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, '0')}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
  }
  function status(message, error = false) {
    $('#status-text').textContent = message;
    $('#status-text').style.color = error ? '#b17051' : '';
  }
  function renderFiles() {
    const query = $('#file-search').value.trim().toLowerCase();
    const visible = files.filter(file => file.name.toLowerCase().includes(query) && (filter === 'all' || (filter === 'video' ? file.kind === 'video' : file.kind !== 'video')));
    const list = $('#file-list'); list.replaceChildren();
    for (const file of visible) {
      const row = document.createElement('button');
      row.className = 'file-row' + (file.id === selectedId ? ' selected' : '') + (file.id === analysisId ? ' current' : '');
      row.setAttribute('aria-pressed', String(file.id === selectedId));
      row.title = file.relativePath || file.name;
      row.append(icon(file.kind === 'video' ? 'video' : file.kind === 'image' ? 'image' : 'file'));
      const label = document.createElement('span'); label.className = 'file-label';
      const name = document.createElement('strong'); name.textContent = file.name;
      const detail = document.createElement('small');
      detail.textContent = file.id === analysisId ? ({preparing:'标注中', running:'正在处理', complete:'分析完成', failed:'处理停止', cancelled:'已中断'}[progress?.status] || '当前分析') : (file.analyzed ? '已分析 · 原始文件' : sizeLabel(file.size));
      label.append(name, detail); row.append(label);
      const dot = document.createElement('span'); dot.className = 'file-state'; row.append(dot);
      row.onclick = () => selectFile(file.id); list.append(row);
    }
    $('#file-total').textContent = files.length;
    $('#library-empty').hidden = view === 'evaluation' || visible.length > 0;
    $('#library-empty p').textContent = files.length ? '没有匹配的文件' : '还没有文件';
    $('#library-empty span').textContent = files.length ? '试试其他关键词或文件类型' : '导入视频，开始你的第一次分析';
  }
  function renderActions() {
    const file = selected();
    $('#start-analysis').disabled = !file || file.kind !== 'video' || !(file.file || file.path) || !canStart();
    $('#start-analysis').firstChild.textContent = busy ? '正在导入…' : file?.analyzed ? '重新分析' : '开始标注';
    $('#start-hint').textContent = localFile ? '双击 start.bat 启动本地服务后，即可标注与分析' : !connected ? '本地服务未连接，请检查启动窗口' : busy ? '正在读取视频，请稍候' : !terminal.includes(progress?.status) ? '当前分析进行中，完成后可开始下一个视频' : !file ? '导入视频后即可开始' : file.kind !== 'video' ? '此文件可预览；分析流程需要视频' : !file.file && !file.path ? '重新分析可在结果页选择“重新标注本视频”' : '进入标注后，先设置区域与实际尺寸';
    $('#path-import').disabled = !canStart();
    $$('#path-form input, #path-form button[type="submit"]').forEach(el => el.disabled = busy);
    $('#current-analysis').hidden = !analysisId;
    const isAnnotation = progress?.status === 'preparing' && analysisId;
    $('[data-view="annotation"]').disabled = !connected || !isAnnotation;
    $('[data-view="progress"]').disabled = !connected || !analysisId || isAnnotation;
    $('[data-view="results"]').disabled = !connected || progress?.status !== 'complete';
    $('[data-view="evaluation"]').disabled = localFile;
    $('#eval-return').textContent = progress?.status === 'complete' ? '返回分析结果' : analysisId ? '返回当前分析' : '返回文件预览';
    $('#session-badge').textContent = localFile ? '文件预览模式' : {ready:'本地工作区',preparing:'正在标注',running:'分析进行中',complete:'分析已完成',failed:'需要检查',cancelled:'已中断'}[progress?.status] || '连接中';
  }
  function renderDetails(file) {
    $('#selected-empty').hidden = !!file; $('#selected-details').hidden = !file;
    if (!file) return;
    $('#selected-name').textContent = file.name;
    $('#selected-type').textContent = (file.name.split('.').pop() || '文件').toUpperCase();
    $('#selected-size').textContent = sizeLabel(file.size);
    $('#selected-resolution').textContent = file.width ? `${file.width} × ${file.height}` : '—';
    $('#selected-duration').textContent = file.kind === 'video' ? durationLabel(file.duration) : '不适用';
    $('#media-resolution').textContent = file.width ? `${file.width} × ${file.height} px` : '原始文件';
    $('#media-size').textContent = sizeLabel(file.size);
  }
  function showView(next) {
    if (localFile && next !== 'preview') return;
    view = next;
    $$('.view-tabs button').forEach(button => {
      const active = button.dataset.view === next;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
    $('#preview-workspace').hidden = next !== 'preview';
    $('#analysis-workspace').hidden = ['preview', 'results', 'evaluation'].includes(next);
    $('#results-workspace').hidden = next !== 'results';
    $('#evaluation-workspace').hidden = next !== 'evaluation';
    $('#evaluation-library').hidden = next !== 'evaluation';
    $('#file-list').hidden = next === 'evaluation';
    $('.file-filter').hidden = next === 'evaluation';
    renderFiles();
    window.EvaluationUI?.setVisible(next === 'evaluation');
    window.EvaluationUI?.filter($('#file-search').value);
    window.StudioResults.setVisible(next === 'results');
    // Stop embedded playback when leaving the processing or annotation view.
    frame.contentDocument?.querySelectorAll('video').forEach(video => video.pause());
    $('#source-video').pause();
    const file = next === 'preview' ? selected() : files.find(item => item.id === analysisId);
    $('#current-name').textContent = file?.name || '工作台';
    $('#tab-note').textContent = next === 'preview' ? (file ? '原始文件预览' : '从一个视频开始') : next === 'annotation' ? '在右侧完成当前步骤' : next === 'results' ? '查看与导出分析结果' : '实时处理状态';
    if (next === 'evaluation') { $('#current-name').textContent = '独立评测标注（可选）'; $('#tab-note').textContent = '用于算法评测 · 日常分析可跳过'; }
    if (['preview', 'results', 'evaluation'].includes(next)) return;
    const route = next === 'annotation' ? '/' : '/progress';
    if (frame.dataset.route !== route) {
      frame.dataset.route = route; frame.src = route;
    }
  }
  function selectFile(id) {
    selectedId = id; renderFiles(); renderDetails(selected()); renderActions(); showView('preview'); preview(selected());
  }
  async function preview(file) {
    const revision = ++mediaRevision;
    $('#welcome').hidden = !!file; $('#media-workspace').hidden = !file;
    const video = $('#source-video'), image = $('#source-image');
    video.pause(); video.removeAttribute('src'); video.load(); image.removeAttribute('src');
    [video, image, $('#source-text'), $('#unavailable-preview')].forEach(el => el.hidden = true);
    if (!file) return;
    $('#media-kind').textContent = file.kind === 'video' ? '视频预览' : file.kind === 'image' ? '图片预览' : '文件内容';
    $('#media-description').textContent = file.analyzed ? '原始文件 · 分析结果见顶部标签' : '原始文件 · 尚未进行处理';
    const unavailable = message => {
      if (revision !== mediaRevision) return;
      video.hidden = image.hidden = true;
      $('#unavailable-message').textContent = message; $('#unavailable-preview').hidden = false;
    };
    const dimensions = (width, height, duration) => {
      if (revision !== mediaRevision) return;
      Object.assign(file, {width, height, duration}); renderDetails(file);
    };
    if (file.kind === 'video' && file.url) {
      video.hidden = false;
      video.onloadedmetadata = () => dimensions(video.videoWidth, video.videoHeight, video.duration);
      video.onerror = () => unavailable('浏览器无法播放此编码，仍可点击右侧“开始标注”读取视频。');
      video.src = file.url;
    } else if (file.kind === 'image' && file.url || file.id === analysisId && file.kind === 'video') {
      image.hidden = false;
      image.onload = () => dimensions(image.naturalWidth, image.naturalHeight, file.duration);
      image.onerror = () => unavailable('此文件暂无浏览器预览，请打开标注或结果页查看。');
      image.src = file.url || '/image.png';
      if (!file.url) $('#media-description').textContent = '当前标注帧 · 视频播放请在结果页查看';
    } else if (file.file && /\.(csv|json|txt)$/i.test(file.name)) {
      try {
        const content = await file.file.slice(0, 200000).text();
        if (revision !== mediaRevision) return;
        $('#source-text').textContent = content + (file.size > 200000 ? '\n\n… 仅预览前 200 KB' : '');
        $('#source-text').hidden = false;
      } catch { unavailable('无法读取此文件，请重新导入。'); }
    } else unavailable('已加入工作区。此文件类型暂不支持预览。');
  }
  function importFiles(incoming) {
    let first = null, added = 0;
    for (const file of incoming) {
      if (!videoExtensions.test(file.name) && !imageExtensions.test(file.name) && !/\.(csv|json|txt)$/i.test(file.name)) continue;
      const signature = `${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`;
      let item = files.find(entry => entry.signature === signature);
      if (!item) {
        const kind = videoExtensions.test(file.name) ? 'video' : imageExtensions.test(file.name) ? 'image' : 'file';
        item = {id:crypto.randomUUID(), signature, name:file.name, relativePath:file.webkitRelativePath, size:file.size, kind, file, url:kind === 'file' ? null : URL.createObjectURL(file)};
        files.push(item); added++;
      }
      if (!first) first = item;
    }
    if (first) selectFile(first.id);
    status(added ? `已加入 ${added} 个文件 · 选择视频后可开始标注` : first ? '文件已在工作区中' : '未发现支持的文件。请选择视频、图片、CSV、JSON 或 TXT。');
    $('#files-input').value = ''; $('#folder-input').value = '';
  }
  async function jsonRequest(route, options) {
    if (localFile) throw Error('请双击 start.bat 启动本地服务，再使用标注与评测功能。');
    const response = await fetch(route, {cache:'no-store', ...options});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || `请求失败（${response.status}）`);
    return data;
  }
  function upload(file) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open('POST', '/api/upload-video?name=' + encodeURIComponent(file.name));
      request.setRequestHeader('X-Annotation-Token', progress.token);
      request.upload.onprogress = event => {
        if (event.lengthComputable) status(`正在导入 ${file.name} · ${Math.round(event.loaded / event.total * 100)}%`);
      };
      request.onerror = () => reject(Error('连接中断，请检查本地运行窗口。'));
      request.onload = () => {
        try {
          const data = JSON.parse(request.responseText);
          if (request.status !== 200) throw Error(data.error || '导入失败');
          resolve(data);
        } catch (error) { reject(error); }
      };
      request.send(file);
    });
  }
  async function start(item, path = null) {
    if (!canStart()) throw Error('请等待当前任务结束后再开始下一个视频。');
    if (item?.size > 8 * 1024 ** 3) throw Error('视频超过 8 GB，请使用右上角“通过路径打开”。');
    busy = true; renderActions(); status('正在读取视频…');
    try {
      const data = path ? await jsonRequest('/api/new-video', {method:'POST', headers:{'Content-Type':'application/json','X-Annotation-Token':progress.token}, body:JSON.stringify({video_path:path})}) : await upload(item.file);
      if (!item) {
        item = {id:crypto.randomUUID(),name:data.video_name || path.split(/[\\/]/).pop(),path,kind:'video'};
        files.push(item);
      }
      selectedId = analysisId = item.id;
      progress = await jsonRequest('/api/progress'); currentSample = progress.sample;
      renderFiles(); renderDetails(item); showView('annotation');
      status('视频已打开 · 在右侧设置分析区域与尺寸标定');
      $('#path-dialog').close();
    } finally { busy = false; renderActions(); }
  }
  async function poll() {
    if (busy || localFile) return;
    try {
      const data = await jsonRequest('/api/progress');
      const previousStatus = progress?.status;
      progress = data; connected = true;
      window.EvaluationUI?.setToken(data.token);
      window.StudioResults.update(data);
      $('#connection-label').textContent = '本地服务已连接';
      if (data.video_name && (!analysisId || data.sample !== currentSample)) {
        currentSample = data.sample;
        // A session opened from inside the embedded result page or after a refresh.
        const item = {id:crypto.randomUUID(),name:data.video_name,kind:'video'};
        files.push(item); analysisId = item.id;
        if (!selectedId) selectedId = item.id;
        renderFiles(); renderDetails(selected());
      }
      if (data.status === 'complete' && analysisId) files.find(file => file.id === analysisId).analyzed = true;
      if (!initialized) {
        initialized = true;
        if (analysisId) {
          showView(data.status === 'preparing' ? 'annotation' : data.status === 'complete' ? 'results' : 'progress');
        }
        status(analysisId ? '已恢复当前分析会话' : '工作台已就绪 · 导入视频即可开始');
      }
      if (previousStatus !== data.status) {
        renderFiles();
        if (data.status === 'complete' && view === 'progress') showView('results');
        if (data.status === 'running' && view === 'annotation') showView('progress');
        if (data.status === 'failed' || data.status === 'cancelled') {
          status(data.error || '处理已停止，请查看处理进度。', true);
          if (view === 'annotation') showView('progress');
        } else if (data.status === 'complete') status('分析完成 · 可查看逐帧指标并导出结果');
      }
      if (data.status === 'preparing') status('正在标注 · 可保存草稿后继续，或取消当前任务');
      if (data.status === 'running') status(`正在处理 ${data.video_name} · 步骤进度 ${data.percent}%`);
      renderActions();
    } catch {
      connected = false; $('#connection-label').textContent = '本地服务未连接';
      status('本地服务连接中断，请检查运行窗口。已导入文件仍可预览。', true); renderActions();
    }
  }
  frame.addEventListener('load', () => {
    if (localFile) return;
    try {
      const doc = frame.contentDocument;
      if (!doc || frame.contentWindow.location.href === 'about:blank') return;
      const style = doc.createElement('link'); style.rel = 'stylesheet'; style.href = '/studio-embed.css'; doc.head.append(style);
      // File drag events do not bubble across the embedded document boundary.
      // Prevent a dropped file from replacing an unsaved annotation page.
      doc.addEventListener('dragover', event => {
        if ([...event.dataTransfer.types].includes('Files')) event.preventDefault();
      });
      doc.addEventListener('drop', event => {
        event.preventDefault(); dragging = 0; $('#drop-overlay').hidden = true;
        if (event.dataTransfer.files.length) importFiles(event.dataTransfer.files);
      });
      const route = frame.contentWindow.location.pathname; frame.dataset.route = route;
      doc.addEventListener('click',event=>{if(event.target.closest('a[href="/studio"]')){event.preventDefault();showView('results');}});
      if (route === '/progress' && view === 'annotation') showView(progress?.status === 'complete' ? 'results' : 'progress');
      if (route === '/' && !['preview','evaluation'].includes(view)) {
        // The legacy result page can open a new annotation in this same frame.
        if (doc.querySelector('#overlay')) showView('annotation');
      }
      poll();
    } catch { status('无法读取分析页面，请刷新工作台重试。', true); }
  });

  $('#files-input').onchange = event => importFiles(event.target.files);
  $('#folder-input').onchange = event => importFiles(event.target.files);
  ['#import-files', '#welcome-import', '#drop-area'].forEach(selector => $(selector).onclick = () => $('#files-input').click());
  $('#import-folder').onclick = () => $('#folder-input').click();
  $('#file-search').oninput = () => { renderFiles(); window.EvaluationUI?.filter($('#file-search').value); };
  $$('[data-filter]').forEach(button => button.onclick = () => {
    filter = button.dataset.filter;
    $$('[data-filter]').forEach(item => { item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', String(item === button)); });
    renderFiles();
  });
  $$('[data-view]').forEach(button => button.onclick = () => {
    if (button.dataset.view === 'preview') { showView('preview'); preview(selected()); }
    else showView(button.dataset.view);
  });
  $('#start-analysis').onclick = () => start(selected(), selected()?.path).catch(error => status(error.message, true));
  $('#current-analysis').onclick = () => {
    selectedId = analysisId; renderFiles();
    showView(progress.status === 'preparing' ? 'annotation' : progress.status === 'complete' ? 'results' : 'progress');
  };
  $('#result-summary').onclick = () => { const panel=$('#result-summary-panel'); panel.open=true; panel.scrollIntoView({block:'start',behavior:'smooth'}); };
  $('#result-run-log').onclick = () => showView('progress');
  $('#eval-return').onclick = () => {
    const target = progress?.status === 'complete' ? 'results' : analysisId ? (progress?.status === 'preparing' ? 'annotation' : 'progress') : 'preview';
    showView(target); if (target === 'preview') preview(selected());
  };
  $('#result-reannotate').onclick = () => {
    if (!progress?.can_reannotate) return;
    showView('progress'); frame.dataset.route = '/review'; frame.src = '/review';
    $('#tab-note').textContent = '选择新的标注起始帧';
  };
  $('#workspace-home').onclick = () => { selectedId = null; renderFiles(); renderDetails(null); renderActions(); showView('preview'); preview(null); };
  $('#path-import').onclick = () => { $('#path-error').hidden = true; $('#path-dialog').showModal(); $('#path-value').focus(); };
  $('#path-form').onsubmit = async event => {
    event.preventDefault(); const path = $('#path-value').value.trim(); if (!path || busy) return;
    $('#path-error').hidden = true;
    try { await start(null, path); }
    catch (error) { $('#path-error').textContent = error.message; $('#path-error').hidden = false; status(error.message, true); }
  };
  $('#open-help').onclick = () => $('#help-dialog').showModal();
  $('#local-file-help').onclick = () => $('#help-dialog').showModal();
  $$('[data-close]').forEach(button => button.onclick = () => $('#' + button.dataset.close).close());
  $('#path-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  const collapse = value => { document.body.classList.toggle('sidebar-collapsed', value); $('#expand-sidebar').hidden = !value; };
  $('#collapse-sidebar').onclick = () => collapse(true); $('#expand-sidebar').onclick = () => collapse(false);
  document.addEventListener('keydown', event => {
    const editable = /INPUT|TEXTAREA|SELECT/.test(event.target.tagName) || event.target.isContentEditable;
    if (event.key.toLowerCase() === 'o' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('#files-input').click(); }
    else if (event.key === '/' && !editable && !document.querySelector('dialog[open]')) { event.preventDefault(); collapse(false); $('#file-search').focus(); }
  });
  document.addEventListener('dragenter', event => {
    if ([...event.dataTransfer.types].includes('Files')) { event.preventDefault(); dragging++; $('#drop-overlay').hidden = false; }
  });
  document.addEventListener('dragover', event => { if ([...event.dataTransfer.types].includes('Files')) event.preventDefault(); });
  document.addEventListener('dragleave', () => { if (--dragging <= 0) { dragging = 0; $('#drop-overlay').hidden = true; } });
  document.addEventListener('drop', event => {
    event.preventDefault(); dragging = 0; $('#drop-overlay').hidden = true;
    if (event.dataTransfer.files.length) importFiles(event.dataTransfer.files);
  });
  window.addEventListener('beforeunload', () => files.forEach(file => { if (file.url) URL.revokeObjectURL(file.url); }));
  renderFiles(); renderActions(); poll();
  if (localFile) {
    $('#local-file-notice').hidden = false;
    $('.brand').href = './studio.html';
    $('#connection-label').textContent = '文件预览 · 本地服务未连接';
    status('界面已加载 · 双击 start.bat，使用自动打开的工作台进行标注与评测');
  }
  // No overlapping polls; request/upload mutations have priority.
  async function schedulePoll() { await poll(); window.setTimeout(schedulePoll, 2500); }
  if (!localFile) window.setTimeout(schedulePoll, 2500);
})();

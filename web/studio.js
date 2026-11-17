'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const localFile = window.location.protocol === 'file:';
  const files = [], folders = [];
  let explorer, organizing=false, folderImport=false, pickingParent=null, analysisItem=null;
  let runs = [], pendingUploads = 0, importQueue = Promise.resolve();
  const terminal = ['ready', 'complete', 'failed', 'cancelled'];
  const videoExtensions = /\.(mp4|avi|mov|mkv|m4v|mpg|mpeg|wmv|webm)$/i;
  const imageExtensions = /\.(png|jpe?g|gif|webp|bmp|tiff?)$/i;
  let setupSession = null;
  let selectedId = null, analysisId = null, filter = 'all', view = 'preview';
  let progress = null, busy = false, connected = false, initialized = false;
  let currentSample = '', mediaRevision = 0, dragging = 0;
  const frame = $('#analysis-frame');
  let stagedIds = [];
  let resultRevision = 0;
  let chamberRequestKey = '', chamberTableKey = '', chamberReports = {}, chamberReadFailed = false;

  let mode='phenotype';
  const channel=window.createChannelWorkspace({request:jsonRequest,getFiles:()=>files,openVideo:id=>selectFile(id),token:async()=>{
    if(!progress?.token)progress=await jsonRequest('/api/progress');
    return progress.token;
  }});
  function switchMode(next){
    if(!['phenotype','channel'].includes(next))return;
    mode=next;document.body.dataset.mode=next;
    const counting=next==='channel';
    for(const button of $$('.task-modes button'))button.setAttribute('aria-pressed',String(button.dataset.mode===next));
    $('#channel-workspace').hidden=!counting;
    $('#channel-name').hidden=!counting;
    $('#mode-crumb').textContent=counting?'通道计数':'表征识别';
    sessionStorage.setItem('worm-task-mode',mode);
    if(counting){
      $('#source-video').pause();
      frame.contentDocument?.querySelectorAll('video').forEach(video=>video.pause());
      window.StudioResults.setVisible(false);
      channel.select(selected());
      if(!selected())$('#channel-name').textContent='工作台';
    }else{
      channel.select(null);
      if(view!=='evaluation' && selectedId!==analysisId)showView('preview');
      else showView(view);
      if(view==='preview')preview(selected());
    }
  }
  for(const b of $$('.task-modes button'))b.onclick=()=>switchMode(b.dataset.mode);

  let historyGroup='', historyItems=[], pickedSample='', groupSignature='', historySignature='';
  let reportsReady=false;
  const resultItems=()=>historyGroup?historyItems:ResultNavigation.group(progress);
  const groupId=()=>historyGroup||progress?.batch?.group_id||'';
  const sourceKey=item=>item.video_path||item.video_name;
  const stateLabels={complete:'已完成',running:'处理中',pending:'待处理',failed:'失败',cancelled:'已中断',removed:'已移出'};

  function viewGroup(id){
    historyGroup=id;historyItems=[];pickedSample='';chamberRequestKey='';chamberTableKey='';
    showView('results');
  }
  function renderHistory(){
    const groups=progress?.batch?.groups||[], signature=JSON.stringify(groups);
    if(signature===historySignature)return;
    historySignature=signature;
    const list=$('#saved-batches');list.replaceChildren();
    if(groups.length){
      const title=document.createElement('strong');title.textContent='历史批次';list.append(title);
    }
    for(const g of groups){
      const button=document.createElement('button');button.className='small-button';
      button.textContent=[g.created?.slice(5,16).replace('T',' '),g.name,g.videos+' 个视频',g.complete+'/'+g.total+' 已完成'].filter(Boolean).join(' · ');
      button.onclick=()=>{if(mode!=='phenotype')switchMode('phenotype');viewGroup(g.id);};list.append(button);
    }
  }
  function metricText(metric) {
    const {value,unit}=SummaryMetrics.display(metric);
    return value==='—' || !unit ? value : value+' '+unit;
  }
  function selectResult(sample){
    pickedSample=sample;renderResultNavigation();
  }
  function renderResultNavigation(){
    renderHistory();
    const pane=$('#results-workspace'), shown=view==='results' && (historyGroup || ResultNavigation.group(progress).length);
    $('#chamber-results').hidden=!shown;
    $('#return-group').hidden=!historyGroup;
    if(!shown){
      pane.classList.remove('group-summary','chambers-only');
      $('#result-switcher').hidden=true;
      if(progress)window.StudioResults.update(progress);
      return;
    }
    const g=groupId();
    const currentItems=ResultNavigation.group(progress);
    const meta=progress?.batch?.groups?.find(x=>x.id===g);
    const signature=g || JSON.stringify(currentItems.map(i=>i.id||i.sample));
    if(signature!==groupSignature){groupSignature=signature;pickedSample='';}
    const requestKey=JSON.stringify([historyGroup,g,historyGroup?meta?.complete:currentItems.filter(i=>i.status==='complete').map(i=>i.sample)]);
    if(chamberRequestKey!==requestKey){
      chamberRequestKey=requestKey;chamberReadFailed=false;reportsReady=false;chamberTableKey='';
      const visible=historyGroup?historyItems:currentItems;
      chamberReports=Object.fromEntries(visible.filter(i=>chamberReports[i.sample]?.worms).map(i=>[i.sample,chamberReports[i.sample]]));
      const query=g?'?group='+encodeURIComponent(g):'';
      jsonRequest('/api/batch-results'+query,{signal:AbortSignal.timeout(20000)}).then(data=>{
        if(requestKey!==chamberRequestKey)return;
        if(historyGroup)historyItems=data.items;
        chamberReports=data.reports;reportsReady=true;
        chamberReadFailed=data.items.some(i=>i.status==='complete' && !data.reports[i.sample]?.worms?.length);
      },()=>{if(requestKey===chamberRequestKey)chamberReadFailed=true;}).finally(()=>{
        if(requestKey===chamberRequestKey){chamberTableKey='';renderResultNavigation();}
      });
    }
    const items=resultItems(), completed=items.filter(i=>i.status==='complete');
    const chosen=completed.find(i=>i.sample===pickedSample) || completed.find(i=>i.sample===progress?.sample) || completed[0];
    if(chosen)pickedSample=chosen.sample;
    const live=chosen && chosen.sample===progress?.sample && progress.status==='complete';
    const report=chosen && (chamberReports[chosen.sample]?.worms?.length?chamberReports[chosen.sample]:live?progress.phenotypes:null);
    pane.classList.toggle('group-summary',!live);
    pane.classList.toggle('chambers-only',!report?.worms?.length);
    if(chosen)$('#current-name').textContent=chosen.name;
    const display=live?progress:report?{status:'complete',sample:chosen.sample,phenotypes:report,video_name:chosen.video_name,summaryOnly:true,group:g,artifacts:report.videos || []}: {status:'ready'};
    window.StudioResults.update(display);
    const index=completed.indexOf(chosen);
    $('#result-switcher').hidden=completed.length<2;
    $('#result-switcher .result-scope-label').textContent='批次内';
    $('#result-video-name').textContent=chosen?.name||'';
    $('#result-position').textContent=index>=0?(index+1)+' / '+completed.length:'';
    for(const [id,offset] of [['result-previous-video',-1],['result-next-video',1]]){
      const target=completed[index+offset],button=$('#'+id);
      button.disabled=!target;button.title=target?.name||'';button.onclick=()=>target&&selectResult(target.sample);
    }
    $('#export-group').disabled=!reportsReady || !completed.length;
    $('#open-result-review').hidden=live||!chosen;
    $('#open-result-review').disabled=!canStart();
    $('#open-result-review').onclick=()=>chosen&&openSavedResult(chosen.sample).catch(e=>status(e.message,true));
    $('#export-group').textContent=new Set(items.map(sourceKey)).size===1?'导出整个视频':'导出整个批次';
    $('#export-video').disabled=!reportsReady || !chosen;
    $('#export-video').hidden=new Set(items.map(sourceKey)).size<2;
    const tableKey=JSON.stringify([items,pickedSample,renderPhenotypes.selected(),chamberReadFailed,reportsReady,!!canStart()]);
    if(chamberTableKey===tableKey)return;
    chamberTableKey=tableKey;
    $('#chamber-results-title').textContent=historyGroup?'历史批次结果':'本次分析结果';
    $('#chamber-count').textContent=items.length?new Set(items.map(sourceKey)).size+' 个视频 · '+completed.length+' / '+items.length+' 项已完成':chamberReadFailed?'读取失败':'正在读取批次…';
    $('#chamber-retry').hidden=!chamberReadFailed;
    const rows=$('#chamber-rows');rows.replaceChildren();
    for(const item of items){
      const data=chamberReports[item.sample] || (item.sample===progress.sample?progress.phenotypes:null);
      for(const worm of data?.worms?.length?data.worms:[null]){
        const row=rows.insertRow();row.setAttribute('aria-current',String(item.sample===pickedSample && (!worm || data.worms.length===1 || renderPhenotypes.selected()===worm.worm_id)));
        const name=document.createElement('th');name.scope='row';
        name.textContent=item.chamber_id==null?item.video_name:'培养槽 '+item.chamber_id;
        if(item.chamber_id!=null){const video=document.createElement('small');video.textContent=item.video_name;name.append(video);}
        if(data?.worms?.length>1)name.append(document.createTextNode(' · 线虫 '+worm.worm_id));
        name.title=item.video_path||item.video_name;row.append(name);
        const state=row.insertCell();state.textContent=item.status==='complete'&&!worm?(chamberReadFailed?'指标未读出':'正在读取指标'):stateLabels[item.status]||item.status;
        if(item.error){const why=document.createElement('details'),summary=document.createElement('summary');summary.textContent='查看原因';why.append(summary,document.createTextNode(item.error));state.append(why);}
        for(const key of SummaryMetrics.coreKeys){const metric=worm&&SummaryMetrics.find(worm,key);row.insertCell().textContent=metric?metricText(metric):'—';}
        const cell=row.insertCell();
        if(worm || (!item.removed&&canStart()&&['failed','cancelled'].includes(item.status))){
          const button=document.createElement('button');button.className='small-button';button.textContent=worm?'查看指标':'重试此项';
          button.onclick=()=>{if(worm){selectResult(item.sample);renderPhenotypes.select(worm.worm_id,true);chamberTableKey='';renderResultNavigation();$('#result-summary-panel').scrollIntoView({block:'nearest'});}else batchAction('retry-one',item.id,g);};
          cell.append(button);
        }
        const videos = item.sample === progress.sample ? progress.artifacts : data?.videos;
        if (item.status === 'complete' && videos?.some(a => a.key === 'video')) {
          const play = document.createElement('button');
          play.className = 'small-button';
          play.textContent = '查看视频';
          play.onclick = () => {
            selectResult(item.sample);
            if (worm) renderPhenotypes.select(worm.worm_id,true);
            const panel = $('#result-video-details');
            panel.open = true;
            panel.scrollIntoView({block:'start'});
            $('#result-canvas').focus({preventScroll:true});
          };
          cell.append(play);
        }
      }
    }
  }
  $('#chamber-retry').onclick=()=>{chamberRequestKey='';chamberTableKey='';renderResultNavigation();};
  $('#return-group').onclick=()=>{viewGroup('');if(!ResultNavigation.group(progress).length&&progress?.status!=='complete')showView(progress?.sample?'progress':'preview');};
  for(const [id,whole] of [['export-video',false],['export-group',true]]){
    $('#'+id).onclick=()=>{
      const all=resultItems(),selected=all.find(i=>i.sample===pickedSample);
      const items=whole?all:all.filter(i=>selected&&sourceKey(i)===sourceKey(selected));
      if(items.length&&reportsReady)SummaryMetrics.download(SummaryMetrics.groupCsv(items,chamberReports),(whole?'批次_'+(groupId()||'结果'):selected.video_name)+'_指标');
    };
  }
  async function openSavedResult(sample) {
    if(!canStart())throw Error('请等待当前任务结束后再切换结果');
    historyGroup='';pickedSample=sample;
    busy=true;
    resultRevision++;
    renderActions();
    try{
      await prepareToLeave();
      if(progress.sample!==sample || progress.status!=='complete'){
        await jsonRequest('/api/open-run',{method:'POST', headers:{'Content-Type':'application/json','X-Annotation-Token':progress.token},body:JSON.stringify({sample})});
      }
      const data=await jsonRequest('/api/progress');
      if(data.sample!==sample || data.status!=='complete')throw Error('结果会话已改变，请重新选择视频');
      progress=data;currentSample=sample;
      const file=files.find(item=>data.video_path && WorkspaceLinks.pathKey(item.path)===WorkspaceLinks.pathKey(data.video_path));
      analysisItem=file || {id:sample,name:data.video_name,kind:'video',path:data.video_path};
      analysisId=analysisItem.id;selectedId=file?.id || null;
      renderDetails(file);window.StudioResults.update(data);showView('results');
      status(`正在查看 ${data.video_name} 的分析结果`);
    }finally{busy=false;renderActions();}
  }
  function displayStaging() {
    const list = $('#staging-list');
    document.body.classList.toggle('has-staged', stagedIds.length > 0);
    list.replaceChildren();
    list.hidden = !stagedIds.length;
    $('#staging-label').textContent = stagedIds.length
      ? `待标定 · ${stagedIds.length} 个视频（按拖入顺序）`
      : '从左侧拖视频到画面，按顺序加入待标定队列';
    stagedIds.forEach((id, index) => {
      const file = files.find(item => item.id === id);
      const row = document.createElement('li');
      row.textContent = `${index+1}. ${file?.name || '文件已移除'}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.setAttribute('aria-label', `移出 ${file?.name || '此文件'}`);
      remove.onclick = () => { if(!busy)setStaging(stagedIds.filter(value => value !== id)); };
      row.append(remove);
      list.append(row);
    });
  }

  function setStaging(ids) {
    stagedIds = ids;
    try { sessionStorage.setItem('worm-staged-videos', JSON.stringify(ids)); } catch {}
    displayStaging();
    renderActions();
  }

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name); svg.append(use); return svg;
  }
  const selected = () => files.find(file=>file.id===selectedId) || (setupSession?.file.id===selectedId?setupSession.file:null);
  const editingSetup = () => setupSession && view==='preview' && selectedId===setupSession.file.id
    && progress?.sample===setupSession.sample && progress.status==='preparing' && !progress.batch?.running;
  const canStart = () => connected && progress && terminal.includes(progress.status) && !progress.batch?.running && !busy && !pendingUploads && !organizing && !folderImport;
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
    if(mode==='channel')channel.select(selected());
    const query = $('#file-search').value.trim().toLowerCase();
    const visible=explorer?.render() || {count:files.length,empty:!files.length};
    const list=$('#file-list');list.classList.toggle('is-empty',visible.empty);
    $('#library-empty').hidden=view==='evaluation'||!visible.empty;
    $('#clear-file-filter').hidden=!files.length&&!folders.length || (!query&&filter==='all');
    $('#library-empty p').textContent=query||filter!=='all'?'没有匹配的文件':'还没有文件';
    $('#library-empty span').textContent=query||filter!=='all'?'试试其他关键词，或清除筛选':'导入文件夹，或点击上方图标新建文件夹';
  }

  let selectionKey='';
  function selectionUpdated(ids){
    const key=JSON.stringify(ids);
    if(key!==selectionKey){selectionKey=key;renderActions();}
  }
  function batchSelection(){
    const videos=(explorer?.selectedIds()||[]).filter(id=>files.some(f=>f.id===id && f.kind==='video'));
    return videos.length>1?videos:stagedIds;
  }
  function renderActions() {
    const file = selected();
    ['#new-folder','#import-folder','#import-files'].forEach(id=>$(id).disabled=organizing||folderImport||pendingUploads>0);
    $('#start-analysis').disabled = !file || file.kind !== 'video' || !(file.file || file.path) || file.available === false || !canStart();
    $('#start-analysis').firstChild.textContent = busy ? '正在导入…' : file?.analyzed ? '重新分析' : '开始标注';
    const selection=batchSelection();
    if(selection.length){
      $('#start-analysis').disabled = !canStart();
      $('#start-analysis').firstChild.textContent = `开始顺序标定（${selection.length}）`;
    }
    $('#start-hint').textContent = localFile ? '打开 WormStudio 桌面应用即可标注与分析' : !connected ? '本地服务未连接，请检查启动窗口' : pendingUploads ? '正在保存导入素材，请稍候' : busy ? '正在读取视频，请稍候' : !terminal.includes(progress?.status) ? '当前分析进行中，完成后可开始下一个视频' : !file ? '导入视频后即可开始' : file.kind !== 'video' ? '此文件可预览；分析流程需要视频' : !file.file && !file.path ? '重新分析可在结果页选择“重新标注本视频”' : '进入标注后，先设置区域与实际尺寸';
    if(editingSetup()){
      $('#start-analysis').disabled=busy||!connected;
      $('#start-analysis').firstChild.textContent='继续标注';
      $('#start-hint').textContent='选择标注方式后继续当前视频。两种方式分别保存草稿。';
    }
    $('#path-import').disabled = !canStart();
    $$('#path-form input, #path-form button[type="submit"]').forEach(el => el.disabled = busy);
    $('#current-analysis').hidden = !analysisId;
    const isAnnotation = progress?.status === 'preparing' && !progress?.batch?.running && analysisId;
    $('[data-view="annotation"]').disabled = !connected || !isAnnotation;
    $('[data-view="progress"]').disabled = !connected || !analysisId || isAnnotation;
    $('[data-view="results"]').disabled = !connected || (progress?.status !== 'complete' && !ResultNavigation.group(progress).length && !historyGroup);
    $('[data-view="evaluation"]').disabled = localFile;
    $('#eval-return').textContent = progress?.status === 'complete' ? '返回分析结果' : analysisId ? '返回当前分析' : '返回文件预览';
    renderLinkedRuns();
    renderBatch();
    renderResultNavigation();
    $('#session-badge').textContent = localFile ? '文件预览模式' : {ready:'本地工作区',preparing:'正在标注',running:'分析进行中',complete:'分析已完成',failed:'需要检查',cancelled:'已中断'}[progress?.status] || '连接中';
  }
  async function batchAction(action,id,group) {
    try {
      progress=await jsonRequest('/api/batch',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':progress.token},body:JSON.stringify({action,id,group})});
      renderActions();
      if(action==='start'||action==='retry-one'){historyGroup='';frame.dataset.route='';showView('progress');status('批处理已启动');}
      if(action==='stop')status('正在停止批处理，待处理任务保留');
    }catch(e){status(e.message,true);}
  }
  function renderBatch() {
    const batch = progress?.batch || {items: [], running: false};
    const items = batch.items.filter(item => item.status !== 'complete');
    const panel = $('#batch-panel');
    if (!items.length && !batch.running) {
      panel.hidden = true;
      panel.open = false;
      return;
    }
    panel.hidden = false;
    const labels = {running: '处理中', pending: '待处理', failed: '失败', cancelled: '已中断'};
    const counts = Object.entries(labels).flatMap(([key, label]) => {
      const count = items.filter(item => item.status === key).length;
      return count ? [count + ' ' + label] : [];
    });
    $('#batch-count').textContent = counts.join(' · ') || '处理中';
    const editable = canStart();
    const start = $('#batch-start'), stop = $('#batch-stop');
    start.hidden = start.disabled = !editable || !items.some(item => item.status === 'pending');
    stop.hidden = stop.disabled = !batch.running;

    const list = $('#batch-list');
    const signature = JSON.stringify([items, editable]);
    if (list.dataset.signature === signature) return;
    list.dataset.signature = signature;
    list.replaceChildren();
    for (const item of items) {
      const row = document.createElement('li');
      const name = document.createElement('span');
      name.className = 'batch-name';
      name.textContent = name.title = item.name;
      const state = document.createElement('span');
      state.className = 'batch-state';
      state.textContent = labels[item.status] || item.status;
      row.append(name, state);
      if (editable) {
        const action = (label, description, operation) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = label;
          button.title = description + '：' + item.name;
          button.setAttribute('aria-label', button.title);
          button.onclick = () => batchAction(operation, item.id);
          row.append(button);
        };
        if (['failed', 'cancelled'].includes(item.status)) action('重试', '重试', 'retry');
        action('×', '移出队列', 'remove');
      }
      if (item.error) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = '查看原因';
        const log = document.createElement('p');
        log.textContent = item.error;
        details.append(summary, log);
        row.append(details);
      }
      list.append(row);
    }
  }
  $('#batch-start').onclick=()=>batchAction('start');
  $('#batch-stop').onclick=()=>batchAction('stop');
  window.addEventListener('message',event=>{
    if(event.origin!==location.origin || event.source!==frame.contentWindow || event.data?.type!=='annotation-queued')return;
    frame.dataset.route='';void poll();
  });
  function renderDetails(file) {
    document.body.classList.toggle('has-selection', !!file);
    $('#selected-empty').hidden = !!file; $('#selected-details').hidden = !file;
    if (!file) return;
    $('#selected-name').textContent = file.name;
    $('#selected-type').textContent = (file.name.split('.').pop() || '文件').toUpperCase();
    $('#selected-size').textContent = sizeLabel(file.size);
    $('#selected-storage').textContent = file.persisting ? '正在保存…' : file.persistError ? '尚未保存，请重新导入' : file.available === false ? '原路径不可用' : file.cached ? '已保存在本机工作区' : file.path ? '已关联原文件路径' : '仅本次预览';
    $('#selected-resolution').textContent = file.width ? `${file.width} × ${file.height}` : '—';
    $('#selected-duration').textContent = file.kind === 'video' ? durationLabel(file.duration) : '不适用';
    $('#media-resolution').textContent = file.width ? `${file.width} × ${file.height} px` : '原始文件';
    $('#media-size').textContent = sizeLabel(file.size);
  }
  function showView(next) {
    if (localFile && next !== 'preview') return;
    view = next;
    if (next === 'progress' && progress?.status === 'running') status('');
    renderResultNavigation();
    document.body.dataset.workspaceView = next;
    $$('[data-view]').forEach(button => {
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
    window.StudioResults.setVisible(mode==='phenotype' && next === 'results');
    $('#focus-canvas').hidden=!['annotation','progress','results'].includes(next);
    // Stop embedded playback when leaving the processing or annotation view.
    frame.contentDocument?.querySelectorAll('video').forEach(video => video.pause());
    $('#source-video').pause();
    const file = next === 'preview' ? selected() : (files.find(item => item.id === analysisId)||analysisItem);
    $('#current-name').textContent = (next==='results' && resultItems().find(i=>i.sample===pickedSample)?.name) || file?.name || '工作台';
    $('#tab-note').textContent = next === 'preview' ? (file ? '原始文件预览' : '从一个视频开始') : next === 'annotation' ? '在右侧完成当前步骤' : next === 'results' ? '查看与导出分析结果' : '实时处理状态';
    if (next === 'evaluation') { $('#current-name').textContent = '独立评测标注（可选）'; $('#tab-note').textContent = '用于算法评测 · 日常分析可跳过'; }
    if (['preview', 'results', 'evaluation'].includes(next)) return;
    const route = next === 'annotation' ? '/' : '/progress';
    if (frame.dataset.route !== route) {
      frame.dataset.route = route; frame.src = route;
    }
  }
  function selectFile(id,{keepSelection=false}={}) {
    if(!keepSelection)explorer.selectOnly(id);
    if(!keepSelection && mode==='phenotype' && ['progress','results'].includes(view) && canStart()){
      const file=files.find(item=>item.id===id);
      const latest=file && WorkspaceLinks.runsForFile(file,runs).find(run=>run.complete);
      if(latest){void openSavedResult(latest.sample).catch(e=>status(e.message,true));return;}
    }
    selectedId = id;
    persistSelection(); renderFiles(); renderDetails(selected()); renderActions();
    if(mode==='phenotype'){showView('preview');preview(selected());}
  }
  async function preview(file) {
    const revision = ++mediaRevision;
    if (view === 'preview') $('#current-name').textContent = file?.name || '工作台';
    $('#welcome').hidden = !!file; $('#media-workspace').hidden = !file;
    const video = $('#source-video'), image = $('#source-image');
    video.pause(); video.removeAttribute('src'); video.load(); image.removeAttribute('src');
    [video, image, $('#source-text'), $('#unavailable-preview')].forEach(el => el.hidden = true);
    if (!file) return;
    $('#media-kind').textContent = file.kind === 'video' ? '视频预览' : file.kind === 'image' ? '图片预览' : '文件内容';
    $('#media-description').textContent = WorkspaceLinks.runsForFile(file,runs).length ? '原始文件 · 右侧可打开此视频的历史结果' : '原始文件 · 尚未进行处理';
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
    } else if ((file.file || file.url) && /\.(csv|json|txt)$/i.test(file.name)) {
      try {
        const response = file.file ? null : await fetch(file.url,{headers:{Range:'bytes=0-199999'}});
        if(response && !response.ok)throw Error('读取失败');
        const content = file.file ? await file.file.slice(0, 200000).text() : await response.text();
        if (revision !== mediaRevision) return;
        $('#source-text').textContent = content + (file.size > 200000 ? '\n\n… 仅预览前 200 KB' : '');
        $('#source-text').hidden = false;
      } catch { unavailable('无法读取此文件，请重新导入。'); }
    } else unavailable('已加入工作区。此文件类型暂不支持预览。');
  }
  async function treeMutation(body,internal=false) {
    if(!internal&&(organizing||folderImport||pendingUploads))throw Error('文件正在保存，请稍后再整理。');
    if(!internal)organizing=true;renderActions();
    try{
      if(!progress?.token)progress=await jsonRequest('/api/progress');
      const data=await jsonRequest('/api/library-tree',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':progress.token},body:JSON.stringify(body)});
      const previous=new Map(files.map(f=>[f.id,f]));
      files.splice(0,files.length,...data.files.map(f=>Object.assign(previous.get(f.id)||{},f)));
      folders.splice(0,folders.length,...data.folders);
      if(selectedId&&!files.some(f=>f.id===selectedId)){selectedId=null;if(view==='preview')preview(null);}
      if(body.op==='remove'&&stagedIds.length){
        const visible=new Set(files.map(f=>f.id));
        setStaging(stagedIds.filter(id=>visible.has(id)));
      }
      if(data.folder&&body.op==='mkdir')explorer.setDestination(data.folder.id);
      renderFiles();renderDetails(selected());
      if(body.op==='remove')status('已从工作区移除 · 原文件、草稿和分析结果保留');
      else if(body.op!=='collapse')status('文件树已保存到本机');
      return data;
    }finally{if(!internal)organizing=false;renderActions();}
  }
  async function importFiles(incoming,parent=explorer.destination,emptyFolders=[]) {
    const importMode=mode;
    if(organizing||folderImport||pendingUploads){status('请等待当前导入完成。',true);return;}
    const videos = [];
    folderImport=true;renderActions();
    try{
      // Reveal the destination so newly imported files are immediately discoverable.
      let ancestor=parent;const seen=new Set();
      while(ancestor&&!seen.has(ancestor)){seen.add(ancestor);const folder=folders.find(f=>f.id===ancestor);if(!folder)break;
        if(folder.collapsed)await treeMutation({op:'collapse',id:folder.id,collapsed:false},true);ancestor=folder.parent;
      }
      const records=[...incoming].map(entry=>entry.file?entry:{file:entry,relativePath:entry.webkitRelativePath||''});
      const paths=new Map();
      const ensure=async parts=>{if(!parts.length)return parent;const key=parts.join('/');if(paths.has(key))return paths.get(key);const data=await treeMutation({op:'ensure-path',parts,parent},true);paths.set(key,data.folder.id);return data.folder.id;};
      for(const parts of emptyFolders)await ensure(parts);
      const prepared=[];
      for(const record of records){const file=record.file;if(!videoExtensions.test(file.name)&&!imageExtensions.test(file.name)&&!/\.(csv|json|txt)$/i.test(file.name))continue;
        const parts=(record.relativePath||'').split('/').filter(Boolean).slice(0,-1);
        const folder=localFile?null:await ensure(parts);prepared.push({...record,parent:folder});
      }
      let first=null,added=0;
      for(const {file,relativePath='',parent:folder} of prepared){
        const signature=`${relativePath||file.name}:${file.size}:${file.lastModified}`;
        let item=files.find(entry=>entry.signature===signature&&(entry.parent||null)===folder);
        if(item?.persistError){item.file=file;queueImport(item);}
        if(!item){const kind=videoExtensions.test(file.name)?'video':imageExtensions.test(file.name)?'image':'file';
          item={id:crypto.randomUUID(),signature,name:file.name,relativePath,size:file.size,kind,file,parent:folder,url:kind==='file'?null:URL.createObjectURL(file)};
          files.push(item);added++;if(!localFile)queueImport(item);
        }
        first ||= item;
        if(item.kind === 'video' && !videos.includes(item)) videos.push(item);
      }
      if(first)selectFile(first.id);else renderFiles();
      status(added?`已加入 ${added} 个文件 · 文件夹层级已保留`:first?'文件已在此文件夹中':'文件夹已添加，未发现支持的视频、图片或数据文件。');
    }catch(e){status('导入失败：'+e.message,true);return;}
    finally{folderImport=false;$('#files-input').value='';$('#folder-input').value='';renderActions();}
    if(localFile || importMode==='channel' || mode==='channel' || videos.length < 2)return;
    await importQueue;
    if(mode==='channel')return;
    const missing=videos.find(item=>item.persistError || !item.path);
    if(missing){status(`未能保存 ${missing.name}，请重新导入这组视频。`,true);return;}
    try {
      await poll();
      if(!canStart()){status('视频已导入。当前任务结束后可重新拖入这组视频开始连续标定。');return;}
      const paths=[...new Set(videos.map(item=>item.path))];
      if(paths.length>1)await start(videos[0], paths[0], paths);
    }catch(e){status('连续标定未启动：'+e.message,true);}
  }
  async function droppedFiles(transfer,parent=explorer.destination){
    dragging=0;$('#drop-overlay').hidden=true;
    const entries=[...transfer.items].map(item=>item.webkitGetAsEntry?.()).filter(Boolean),fallback=[...transfer.files];
    const records=[],directories=[];
    const visit=async(entry,prefix='',depth=0)=>{
      if(depth>64||records.length>10000)throw Error('文件夹层级或文件数量过多，请分批导入。');
      const relative=prefix+entry.name;
      if(entry.isFile){const file=await new Promise((resolve,reject)=>entry.file(resolve,reject));records.push({file,relativePath:relative});}
      else if(entry.isDirectory){directories.push(relative.split('/'));const reader=entry.createReader();while(true){const batch=await new Promise((resolve,reject)=>reader.readEntries(resolve,reject));if(!batch.length)break;for(const child of batch)await visit(child,relative+'/',depth+1);}}
    };
    try{if(entries.length){for(const entry of entries)await visit(entry);await importFiles(records,parent,directories);}else await importFiles(fallback,parent);}
    catch(e){status('无法读取拖入的文件夹：'+e.message,true);}
  }
  async function jsonRequest(route, options) {
    if (localFile) throw Error('请打开 WormStudio 桌面应用进行标注与评测。');
    const response = await fetch(route, {cache:'no-store', ...options});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || `请求失败（${response.status}）`);
    return data;
  }
  function persistSelection() {
    if(localFile || !progress?.token || (selectedId && !selected()?.path))return;
    jsonRequest('/api/library-selection',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':progress.token},body:JSON.stringify({id:selectedId})}).catch(error=>status(error.message,true));
  }
  function queueImport(item) {
    if(item.persisting)return;
    pendingUploads++;item.persisting=true;item.persistError=false;
    importQueue=importQueue.then(async()=>{
      try {
        if(!progress?.token)progress=await jsonRequest('/api/progress');
        const result=await upload(item);
        const oldId=item.id, oldUrl=item.url;
        Object.assign(item,result,{persisting:false,persistError:false});
        explorer.replaceId(oldId,item.id);
        if(selectedId===oldId)selectedId=item.id;
        if(analysisId===oldId)analysisId=item.id;
        if(selectedId===item.id){renderDetails(item);preview(item);persistSelection();}
        if(oldUrl?.startsWith('blob:'))URL.revokeObjectURL(oldUrl);
        status(`已保存 ${item.name} · 刷新或重启后可继续使用`);
      } catch(error) {item.persistError=true;status(`${item.name} 未保存：${error.message}。重新导入可重试。`,true);}
      finally {item.persisting=false;pendingUploads--;renderFiles();renderActions();renderDetails(selected());}
    });
  }
  function upload(item) {
    return new Promise((resolve,reject)=>{
      const request=new XMLHttpRequest();
      const query=new URLSearchParams({name:item.name,relativePath:item.relativePath||'',signature:item.signature||''});if(item.parent)query.set('parent',item.parent);
      request.open('POST','/api/library-upload?'+query);
      request.setRequestHeader('X-Annotation-Token',progress.token);
      request.upload.onprogress=event=>{if(event.lengthComputable)status(`正在保存 ${item.name} · ${Math.round(event.loaded/event.total*100)}%`);};
      request.onerror=()=>reject(Error('连接中断，请检查本地运行窗口'));
      request.onload=()=>{try{const data=JSON.parse(request.responseText);if(request.status!==200)throw Error(data.error||'导入失败');resolve(data);}catch(e){reject(e);}};
      request.send(item.file);
    });
  }
  async function prepareToLeave() {
    if(pendingUploads)throw Error('文件正在保存，请等待导入完成。');
    await channel.flush();
    await frame.contentWindow?.AnnotationDraft?.flush();
  }
  function renderLinkedRuns() {
    const target=$('#selected-runs');target.replaceChildren();
    const related=WorkspaceLinks.runsForFile(selected(),runs);
    $('#selected-history').hidden=!selected()||selected().kind!=='video';
    $('#selected-run-count').textContent=`${related.length} 次`;
    if(!related.length){const note=document.createElement('p');note.textContent='此视频还没有保存的分析记录。';target.append(note);return;}
    for(const run of related){
      const card=document.createElement('article');card.className='linked-run';
      const title=document.createElement('strong');title.textContent=run.sample;
      const meta=document.createElement('p');meta.textContent=`${new Date(run.updated*1000).toLocaleString('zh-CN')} · ${run.complete?'已完成':run.has_metrics?'指标已生成':'尚未完成'}`;
      card.append(title,meta);
      const action=(label,path)=>{const button=document.createElement('button');button.className='small-button';button.textContent=label;button.disabled=!canStart();button.onclick=()=>window.WorkspaceActions.run(path,{sample:run.sample}).catch(e=>status(e.message,true));card.append(button);};
      if(run.has_metrics||run.complete)action('打开此结果','/api/open-run');
      if(!run.complete)action('继续此任务','/api/resume-run');
      target.append(card);
    }
  }
  async function editAnnotationSetup(){
    if(busy)return;
    const active=frame.contentWindow?.AnnotationDraft?.setup();
    if(!active)throw Error('此任务无法更改标注方式');
    busy=true;resultRevision++;
    try{
      await prepareToLeave();
      const current=await jsonRequest('/api/progress');
      if(current.status!=='preparing'||current.sample!==active.sample||current.batch?.running)throw Error('标注会话已改变，请返回当前分析');
      const file=files.find(f=>f.path && current.video_path && WorkspaceLinks.pathKey(f.path)===WorkspaceLinks.pathKey(current.video_path))
        || (analysisItem?.path===current.video_path?analysisItem:null)
        || {id:active.sample,kind:'video',path:current.video_path,name:current.video_name};
      progress=current;currentSample=current.sample;
      setupSession={...active,file};analysisItem=file;selectedId=analysisId=file.id;explorer.selectOnly(file.id);
      persistSelection();renderDetails(file);
      window.WorkspaceLinks.setFocusMode(false);
      showView('preview');void preview(file);
      $('#chamber-mode').checked=active.chamberMode;$('#chamber-mode').focus();
      status('已返回当前视频的标注设置');
    }finally{busy=false;renderActions();}
  }
  async function resumeAnnotationSetup(){
    if(!editingSetup()||busy)return;
    const session=setupSession,chosen=$('#chamber-mode').checked;
    busy=true;resultRevision++;renderActions();
    try{
      if(chosen!==session.chamberMode){
        const current=await jsonRequest('/state');
        if(current.sample!==session.sample || !current.can_edit_mode)throw Error('标注会话已改变，请返回当前分析');
        if(current.chamber_mode!==chosen){
          if(current.token!==session.token)throw Error('标注方式已在另一个窗口更改，请重新打开标注设置');
          await jsonRequest('/api/annotation-mode',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':session.token},body:JSON.stringify({chamber_mode:chosen})});
        }
        frame.dataset.route='';
      }
      setupSession=null;showView('annotation');
    }finally{busy=false;renderActions();}
  }
  window.StudioWorkspace={prepareToLeave,openSavedResult,editAnnotationSetup,updateRuns(data){
    runs=data;
    for(const run of runs){if(run.file&&!files.some(f=>f.id===run.file.id))files.push(run.file);}
    renderFiles();renderLinkedRuns();renderResultNavigation();
  }};
  async function start(item, path = null, sequence = undefined) {
    if (!canStart()) throw Error('请等待当前任务结束后再开始下一个视频。');
    if (!path && item?.size > 8 * 1024 ** 3) throw Error('视频超过 8 GB，请使用右上角“通过路径打开”。');
    await prepareToLeave();
    historyGroup='';pickedSample='';busy = true; renderActions(); status('正在读取视频…');
    try {
      if(!path){const stored=await upload(item);Object.assign(item,stored);path=stored.path;}
      const data = await jsonRequest('/api/new-video', {method:'POST', headers:{'Content-Type':'application/json','X-Annotation-Token':progress.token}, body:JSON.stringify({video_path:path,sequence,edit_setup:true,chamber_mode:$('#chamber-mode').checked})});
      if (!item) {
        item = files.find(f=>f.id===data.file.id) || data.file;
        if(!files.includes(item))files.push(item);
      }
      Object.assign(item,data.file);
      selectedId = analysisId = item.id;analysisItem=item;persistSelection();
      frame.dataset.route="";
      progress = await jsonRequest('/api/progress'); currentSample = progress.sample;
      renderFiles(); renderDetails(item); showView('annotation');
      status('视频已打开 · 在右侧设置分析区域与尺寸标定');
      $('#path-dialog').close();
    } finally { busy = false; renderActions(); }
  }
  async function poll() {
    if (busy || pendingUploads || localFile) return;
    const revision=resultRevision;
    try {
      const data = await jsonRequest('/api/progress');
      if(busy || revision!==resultRevision)return;
      const previousStatus = progress?.status;
      const batchEnded=!!progress?.batch?.running && !data.batch?.running;
      if (!connected && view === 'progress') status('');
      progress = data; connected = true;
      if(setupSession && (data.sample!==setupSession.sample || data.status!=='preparing'))setupSession=null;
      window.EvaluationUI?.setToken(data.token);
      $('#connection-label').textContent = '本地服务已连接';
      if (data.video_name && (!analysisId || data.sample !== currentSample)) {
        currentSample = data.sample;
        // A session opened from inside the embedded result page or after a refresh.
        let item = files.find(f=>data.video_path && WorkspaceLinks.pathKey(f.path)===WorkspaceLinks.pathKey(data.video_path));
        if(!item)item={id:crypto.randomUUID(),name:data.video_name,path:data.video_path,kind:'video'};
        analysisItem=item;
        analysisId = item.id;
        if(view==='annotation' || view==='progress')$('#current-name').textContent=item.name;
        if(mode==='phenotype' && files.includes(item) && (!selectedId || (view==='annotation' && data.status==='preparing')))selectedId=item.id;
        renderFiles(); renderDetails(selected());
      }
      if(data.status==='complete'&&analysisItem)analysisItem.analyzed=true;
      if (!initialized) {
        initialized = true;
        if (analysisId) {
          showView(data.status === 'ready' && !data.batch?.running ? 'preview' : data.status === 'preparing' && !data.batch?.running ? 'annotation' : data.status === 'complete' ? 'results' : 'progress');
        }
        if (view !== 'progress') status(analysisId ? '已恢复当前分析会话' : '工作台已就绪 · 导入视频即可开始');
      }
      if (previousStatus !== data.status || batchEnded) {
        if(data.status==='complete')window.WorkspaceActions?.refresh();
        renderFiles();
        // Once this analysis finishes, show its total metrics. Keep batch work in place.
        const finishedChambers=!data.batch?.running && ['complete','cancelled','failed'].includes(data.status) && ResultNavigation.group(data).some(i=>i.status==='complete');
        if((AnalysisPresentation.totalsReady(data) || finishedChambers) && ['annotation','progress'].includes(view)) {
          showView('results');status(finishedChambers?'已显示本批次结果':'分析完成 · 已显示线虫指标');
        }
        if (data.status === 'ready' && !data.batch?.running && view === 'annotation') { showView('preview'); preview(selected()); status('标注已加入队列，可继续标定下一个视频'); }
        if (data.status === 'running' && view === 'annotation') showView('progress');
        if (data.status === 'failed' || data.status === 'cancelled') {
          status(data.error || '处理已停止，请查看处理进度。', true);
          if (view === 'annotation') showView('progress');
        } else if (data.status === 'complete' && view !== 'progress') status('分析完成 · 可查看逐帧指标并导出结果');
      }
      if (data.status === 'preparing' && !data.batch?.running) status('正在标注 · 草稿自动保存到本机');
      if (data.status === 'running' && view !== 'progress') status(`正在处理 ${data.video_name}`);
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
      const usability=doc.createElement('link');usability.rel='stylesheet';usability.href='/workspace-usability.css';doc.head.append(usability);
      const clean = doc.createElement('link'); clean.rel = 'stylesheet'; clean.href = '/studio-clean.css'; doc.head.append(clean);
      // File drag events do not bubble across the embedded document boundary.
      // Prevent a dropped file from replacing an unsaved annotation page.
      doc.addEventListener('dragover', event => {
        if ([...event.dataTransfer.types].includes('Files')) event.preventDefault();
      });
      doc.addEventListener('drop', event => {
        event.preventDefault(); dragging = 0; $('#drop-overlay').hidden = true;
        if([...event.dataTransfer.types].includes('Files'))void droppedFiles(event.dataTransfer);
      });
      frame.contentWindow.WorkspaceLinks?.setFocusMode(document.body.classList.contains('canvas-focus'));
      const route = frame.contentWindow.location.pathname; frame.dataset.route = route;
      doc.addEventListener('click',event=>{if(event.target.closest('a[href="/studio"]')){event.preventDefault();showView('results');}});
      if (route === '/progress' && view === 'annotation') showView('progress');
      if (route === '/' && !['preview','evaluation'].includes(view)) {
        // The legacy result page can open a new annotation in this same frame.
        if (doc.querySelector('#overlay')) showView('annotation');
      }
      poll();
    } catch { status('无法读取分析页面，请刷新工作台重试。', true); }
  });

  explorer=LibraryTree.create({getState:()=>({files,folders,query:$('#file-search').value,filter,selectedId,analysisId}),mutate:treeMutation,select:selectFile,stageVideos,icon,status,selectionChanged:selectionUpdated,
    isBusy:()=>organizing||folderImport||pendingUploads>0,
    importFiles:parent=>{pickingParent=parent;$('#files-input').click();},importFolder:parent=>{pickingParent=parent;$('#folder-input').click();},dropFiles:droppedFiles});
  $('#files-input').onchange = event => void importFiles(event.target.files,pickingParent);
  $('#folder-input').onchange = event => void importFiles(event.target.files,pickingParent);
  ['#import-files', '#welcome-import', '#drop-area'].forEach(selector => $(selector).onclick = () => {pickingParent=explorer.destination;$('#files-input').click();});
  $('#import-folder').onclick = () => {pickingParent=explorer.destination;$('#folder-input').click();};
  $('#clear-file-filter').onclick=()=>{ $('#file-search').value=''; filter='all'; $$('[data-filter]').forEach(button=>{const active=button.dataset.filter==='all';button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));}); renderFiles(); $('#file-search').focus(); };
  $('#file-search').oninput = () => { renderFiles(); window.EvaluationUI?.filter($('#file-search').value); };
  $$('[data-filter]').forEach(button => button.onclick = () => {
    filter = button.dataset.filter;
    $$('[data-filter]').forEach(item => { item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', String(item === button)); });
    renderFiles();
  });
  $$('[data-view]').forEach(button => button.onclick = () => {
    if(button.dataset.view==='preview' && view==='annotation' && frame.contentWindow?.AnnotationDraft?.setup()){
      editAnnotationSetup().catch(error=>status(error.message,true));return;
    }
    if(button.dataset.view==='annotation' && editingSetup()){resumeAnnotationSetup().catch(error=>status(error.message,true));return;}
    if (button.dataset.view === 'preview') { showView('preview'); preview(selected()); }
    else showView(button.dataset.view);
  });
  $('#start-analysis').onclick = async () => {
    try {
      if(editingSetup()){await resumeAnnotationSetup();return;}
      const ids=batchSelection();
      if(!ids.length){await start(selected(), selected()?.path);return;}
      const staged = ids.map(id => files.find(file => file.id === id));
      if(staged.some(file => !file?.path || file.available === false))throw Error('队列中有不可用的视频，请先将它移出。');
      const paths = [...new Set(staged.map(file => file.path))];
      await start(staged[0], paths[0], paths);
      if(ids===stagedIds)setStaging([]);
    } catch(error) { status(error.message,true); }
  };
  $('#current-analysis').onclick = () => {
    if(editingSetup()){resumeAnnotationSetup().catch(error=>status(error.message,true));return;}
    selectedId=files.some(f=>f.id===analysisId)?analysisId:null;renderFiles();
    showView(progress.status === 'preparing' ? 'annotation' : progress.status === 'complete' ? 'results' : 'progress');
  };
  $('#result-summary').onclick = () => $('#result-summary-panel').scrollIntoView({block:'start',behavior:'smooth'});
  const resultMenus=[...document.querySelectorAll('.result-action-menu')];
  resultMenus.forEach(menu=>menu.addEventListener('toggle',()=>{if(menu.open)resultMenus.forEach(other=>{if(other!==menu)other.open=false;});}));
  document.addEventListener('click',event=>resultMenus.forEach(menu=>{if(!menu.contains(event.target))menu.open=false;}));
  document.addEventListener('keydown',event=>{if(event.key==='Escape')resultMenus.forEach(menu=>{if(menu.open){menu.open=false;menu.querySelector('summary').focus();}});});
  $('#eval-return').onclick = () => {
    const target = progress?.status === 'complete' ? 'results' : analysisId ? (progress?.status === 'preparing' ? 'annotation' : 'progress') : 'preview';
    showView(target); if (target === 'preview') preview(selected());
  };
  $('#result-reannotate').onclick = () => {
    if (!progress?.can_reannotate) return;
    showView('progress'); frame.dataset.route = '/review'; frame.src = '/review';
    $('#tab-note').textContent = '选择新的标注起始帧';
  };
  $('#workspace-home').onclick = () => { explorer.setDestination(null);selectedId = null; persistSelection(); renderFiles(); renderDetails(null); renderActions(); showView('preview'); preview(null); };
  $('#path-import').onclick = () => { $('#path-submit-label').textContent=mode==='channel'?'打开视频':'打开并标注'; $('#path-error').hidden = true; $('#path-dialog').showModal(); $('#path-value').focus(); };
  $('#path-form').onsubmit = async event => {
    event.preventDefault(); const path = $('#path-value').value.trim(); if (!path || busy) return;
    $('#path-error').hidden = true;
    try {
      if(mode==='channel'){
        const key=progress?.token||(await jsonRequest('/api/progress')).token;
        const item=await jsonRequest('/api/library-link',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':key},body:JSON.stringify({path})});
        if(!files.some(f=>f.id===item.id))files.push(item);
        selectFile(item.id);$('#path-dialog').close();
      }else await start(null, path);
    }
    catch (error) { $('#path-error').textContent = error.message; $('#path-error').hidden = false; status(error.message, true); }
  };
  const workspaceMenu=$('#workspace-menu'),menuButton=$('#workspace-options');
  const closeWorkspaceMenu=()=>{workspaceMenu.hidden=true;menuButton.setAttribute('aria-expanded','false');};
  menuButton.onclick=()=>{workspaceMenu.hidden=!workspaceMenu.hidden;menuButton.setAttribute('aria-expanded',String(!workspaceMenu.hidden));if(!workspaceMenu.hidden){const r=menuButton.getBoundingClientRect();workspaceMenu.style.left=Math.max(4,Math.min(r.right-workspaceMenu.offsetWidth,innerWidth-workspaceMenu.offsetWidth-4))+'px';workspaceMenu.style.top=Math.min(r.bottom+4,innerHeight-workspaceMenu.offsetHeight-4)+'px';}};
  workspaceMenu.addEventListener('click',event=>{if(event.target.closest('button'))closeWorkspaceMenu();});
  document.addEventListener('pointerdown',event=>{if(!workspaceMenu.contains(event.target)&&!menuButton.contains(event.target))closeWorkspaceMenu();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')closeWorkspaceMenu();});
  $('#open-help').onclick = () => $('#help-dialog').showModal();
  $('#local-file-help').onclick = () => $('#help-dialog').showModal();
  $$('[data-close]').forEach(button => button.onclick = () => $('#' + button.dataset.close).close());
  $('#path-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  const collapse = value => { document.body.classList.toggle('sidebar-collapsed', value); $('#expand-sidebar').hidden = !value; };
  const layoutStore=window.WorkspaceLinks.layoutStore;
  const rememberSidebar=value=>{collapse(value);layoutStore.write('studio:sidebar',{collapsed:value});};
  $('#collapse-sidebar').onclick = () => rememberSidebar(true); $('#expand-sidebar').onclick = () => rememberSidebar(false);
  const narrowWorkspace = window.matchMedia('(max-width: 760px)');
  const restoreSidebar=()=>collapse(narrowWorkspace.matches || layoutStore.read('studio:sidebar').collapsed===true);
  layoutStore.subscribe(restoreSidebar);restoreSidebar();
  narrowWorkspace.addEventListener('change', restoreSidebar);
  document.addEventListener('keydown', event => {
    if(mode==='channel')return;
    const {key, target} = event;
    if (![' ', 'ArrowLeft', 'ArrowRight'].includes(key) || event.defaultPrevented ||
        event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (view !== 'results' && view !== 'preview') return;
    if (target.isContentEditable || document.querySelector('dialog[open], .result-action-menu[open], #workspace-menu:not([hidden]), #tree-menu:not([hidden])')) return;
    const control = target.closest('input, textarea, select, button, a, summary, [role="tab"]');
    if (control && !control.matches('#result-play, #result-prev, #result-next, #result-frame, .view-tabs [aria-current="page"]')) return;

    if (view === 'results') {
      const action = key === ' ' ? $('#result-play') : $(key === 'ArrowLeft' ? '#result-prev' : '#result-next');
      if (key === ' ' && (!$('#studio-result-video').currentSrc || $('#studio-result-video').error)) return;
      if (action.disabled && $('#result-frame').disabled) return;
      event.preventDefault();
      if (key === ' ' && event.repeat) return;
      if (key !== ' ') $('#studio-result-video').pause();
      action.click();
      return;
    }

    const video = $('#source-video');
    if (video.hidden || video.readyState < 1 || !Number.isFinite(video.duration)) return;
    event.preventDefault();
    if (key === ' ') {
      if (event.repeat) return;
      if (video.paused) video.play().catch(() => status('视频未能播放', true));
      else video.pause();
    } else {
      video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + (key === 'ArrowRight' ? 1 : -1)));
    }
  });
  document.addEventListener('keydown', event => {
    const editable = /INPUT|TEXTAREA|SELECT/.test(event.target.tagName) || event.target.isContentEditable;
    if (event.key.toLowerCase() === 'o' && (event.ctrlKey || event.metaKey)) { event.preventDefault();pickingParent=explorer.destination;$('#files-input').click(); }
    else if (event.key === '/' && !editable && !document.querySelector('dialog[open]')) { event.preventDefault(); collapse(false); $('#file-search').focus(); }
  });
  document.addEventListener('dragenter', event => {
    if ([...event.dataTransfer.types].includes('Files')) { event.preventDefault(); dragging++; $('#drop-overlay').hidden = false; }
  });
  function stageVideos(ids) {
    if(busy){status('请等待当前视频打开后再添加。');return;}
    const videos=ids.map(id=>files.find(item=>item.id===id));
    if(videos.some(file=>!file?.path||file.kind!=='video'||file.available===false)){status('所选视频尚未保存或路径不可用。',true);return;}
    const added=ids.filter(id=>!stagedIds.includes(id));
    if(!added.length){status('所选视频已在待标定队列中');return;}
    setStaging([...stagedIds,...added]);
    if(!selected())selectFile(added[0]);
    status(added.length===1?`${files.find(f=>f.id===added[0]).name} 已加入第 ${stagedIds.length} 位`:`已将 ${added.length} 个视频按文件树顺序加入队列`);
  }
  document.addEventListener('dragover', event => { if ([...event.dataTransfer.types].includes('Files')) event.preventDefault(); });
  document.addEventListener('dragleave', () => { if (--dragging <= 0) { dragging = 0; $('#drop-overlay').hidden = true; } });
  document.addEventListener('drop', event => {
    if(![...event.dataTransfer.types].includes('Files'))return;event.preventDefault(); dragging = 0; $('#drop-overlay').hidden = true;
    void droppedFiles(event.dataTransfer);
  });
  window.addEventListener('beforeunload',event=>{
    if(pendingUploads || frame.contentWindow?.AnnotationDraft?.dirty()){event.preventDefault();event.returnValue='';}
  });
  renderFiles(); renderActions();
  async function restoreLibrary(){
    if(localFile)return;
    try{const data=await jsonRequest('/api/library');files.push(...data.files);folders.push(...(data.folders||[]));selectedId=data.selected;renderFiles();renderDetails(selected());if(selected())preview(selected());}
    catch(error){$('#library-error').hidden=false;$('#library-error').textContent=error.message+'。请检查本机工作区索引后刷新。';status(error.message,true);}
    await poll();
    try {
      const ids = JSON.parse(sessionStorage.getItem('worm-staged-videos') || '[]');
      if(Array.isArray(ids))setStaging(ids.filter(id => files.some(file => file.id === id)));
    } catch {}
    if(stagedIds.length && !selected() && view === 'preview')selectFile(stagedIds[0]);
  }
  switchMode(sessionStorage.getItem('worm-task-mode')||'phenotype');
  window.StudioWorkspace.ready=restoreLibrary();
  if (localFile) {
    $('#local-file-notice').hidden = false;
    $('.brand').href = './studio.html';
    $('#connection-label').textContent = '文件预览 · 本地服务未连接';
    status('界面已加载 · 打开 WormStudio 桌面应用进行标注与评测');
  }
  // No overlapping polls; request/upload mutations have priority.
  async function schedulePoll() { await poll(); window.setTimeout(schedulePoll, 2500); }
  if (!localFile) window.StudioWorkspace.ready.then(()=>window.setTimeout(schedulePoll,2500));
})();

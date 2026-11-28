'use strict';
const el = id => document.getElementById(id);
const api = window.settings;
let latest;
function render(state) {
  latest = state;
  el('current').textContent = state.current;
  el('auto').checked = state.auto;
  el('check').disabled = state.installing || state.checking || state.transfer?.status === 'loading';
  el('check').textContent = state.checking ? '正在检查…' : '检查更新';
  const message = state.error || (!state.checking && state.checked && !state.release ? '当前系统暂无可下载的新版本' : '');
  el('message').textContent = message;
  el('message').hidden = !message;
  el('release').hidden = !state.release;
  if (!state.release) return;
  el('version').textContent = state.release.version;
  el('notes').textContent = state.release.notes;
  const transfer = state.transfer || {};
  const loading = transfer.status === 'loading';
  el('download').disabled = state.installing || loading || state.checking;
  el('download').querySelector('span').textContent = state.installing ? '正在准备更新…' : transfer.status === 'done' ? (state.canInstall ? '重启并更新' : '显示安装包') : loading ? '正在下载…' : '下载新版';
  el('transfer').textContent = transfer.message || (loading && transfer.percent != null ? transfer.percent + '%' : transfer.status === 'done' ? '下载完成' : '');
}
function bind(id, event, action) {
  el(id).addEventListener(event, async () => {
    try { await action(); }
    catch { el('message').hidden = false; el('message').textContent = '操作失败，请重试'; }
  });
}
let gpuAvailable = false;
function checkOnOpen() {
  if (gpuAvailable && !el('general').hidden && el('gpu-status').hidden) void el('gpu').onclick();
}
for (const id of ['general','updates']) {
  el(id+'-tab').onclick = () => {
    for (const pane of ['general','updates']) {
      el(pane).hidden = pane !== id;
      if (pane === id) el(pane+'-tab').setAttribute('aria-current','page');
      else el(pane+'-tab').removeAttribute('aria-current');
    }
    if (id === 'general') checkOnOpen();
  };
}
bind('auto','change', () => api.auto(el('auto').checked));
bind('check','click', () => api.check());
bind('download','click', () => latest?.transfer?.status === 'done' ? (latest.canInstall ? api.install() : api.reveal()) : api.download());
bind('workspace','click', () => api.workspace());
api.gpuAvailable().then(available => {
  gpuAvailable = available;
  el('gpu-row').hidden = !available;
  checkOnOpen();
});
let gpuState;
function renderGpu({state,message,transfer}) {
  gpuState = state;
  el('gpu-status').hidden = false;
  el('gpu-status').textContent = message;
  el('gpu').disabled = state === 'installing';
  el('gpu-transfer').hidden = !transfer;
  if (transfer) {
    const {received,total} = transfer;
    const percent = Math.floor(received/total*100);
    el('gpu-progress').max = total;
    el('gpu-progress').value = received;
    const mib = n => (n/2**20).toLocaleString('zh-CN',{maximumFractionDigits:1});
    el('gpu-bytes').textContent = `${percent}% · ${mib(received)} / ${mib(total)} MiB`;
  }
  const install = el('gpu-install');
  install.hidden = !['available','prepared','online','online-active','workspace','installing'].includes(state);
  install.disabled = state === 'installing';
  install.textContent = ({workspace:'安装匹配的 GPU 加速组件', online:'下载并安装 GPU 加速','online-active':'查看安装窗口',
    installing:transfer ? '正在准备组件…' : '正在安装…'})[state] || '启用离线加速包…';
}
el('gpu').onclick = async () => {
  const button = el('gpu');
  if (button.disabled) return;
  const status = el('gpu-status');
  button.disabled = true;
  button.textContent = '检测中…';
  status.textContent = '正在检查显卡驱动和工作台的 GPU 运算能力…';
  status.hidden = false;
  el('gpu-install').hidden = true;
  try {
    renderGpu(await api.checkGpu());
  } catch {
    renderGpu({state:'error',message:'检测未完成，请稍后重新检测。'});
  } finally {
    button.textContent = '重新检测';
    button.disabled = gpuState === 'installing';
  }
};
el('gpu-install').onclick = async () => {
  const install = el('gpu-install');
  if (install.disabled) return;
  install.disabled = true;
  const source = gpuState === 'workspace';
  if (source) renderGpu({state:'installing',message:'正在准备匹配的 GPU 加速组件…'});
  try {
    const result = await api.openGpu();
    if (result?.state) renderGpu(result);
    else if (gpuState !== 'online-active' && gpuState !== 'restart') el('gpu-status').textContent = result?.online ? '已打开 GPU 组件下载窗口，支持暂停和续传。安装完成后重启生效，当前任务继续使用 CPU。'
      : '已打开离线加速安装窗口。完成启用后重新打开工作台即可使用。';
  } catch {
    renderGpu({state:source ? 'workspace' : gpuState,message:'GPU 安装未能启动，请重试。'});
  } finally {
    install.disabled = gpuState === 'installing';
  }
};
api.onGpuStatus(renderGpu);
api.onOpenUpdates(() => el('updates-tab').click());
api.onChange(render);
api.state().then(render);

api.onGpuReady?.(backend => {
  renderGpu(backend === 'cuda' ? {state:'restart',message:'GPU 加速组件已就绪。完成当前任务后重启 WormStudio，即可使用显卡加速。'}
    : {state:'online',message:'已保留 CPU 环境，可稍后再安装 GPU 加速。'});
});

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
for (const id of ['general','updates']) {
  el(id+'-tab').onclick = () => {
    for (const pane of ['general','updates']) {
      el(pane).hidden = pane !== id;
      if (pane === id) el(pane+'-tab').setAttribute('aria-current','page');
      else el(pane+'-tab').removeAttribute('aria-current');
    }
  };
}
bind('auto','change', () => api.auto(el('auto').checked));
bind('check','click', () => api.check());
bind('download','click', () => latest?.transfer?.status === 'done' ? (latest.canInstall ? api.install() : api.reveal()) : api.download());
bind('workspace','click', () => api.workspace());
api.gpuAvailable().then(available => { el('gpu-row').hidden = !available; });
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
    const result = await api.checkGpu();
    status.textContent = result.message;
    el('gpu-install').hidden = !['available', 'prepared', 'online'].includes(result.state);
    el('gpu-install').textContent = result.state === 'online' ? '下载并安装 GPU 加速' : '启用离线加速包…';
  } catch {
    status.textContent = '检测未完成，请稍后重新检测。';
  } finally {
    button.textContent = '重新检测';
    button.disabled = false;
  }
};
el('gpu-install').onclick = async () => {
  try {
    const result = await api.openGpu();
    el('gpu-status').textContent = result?.online ? '已打开 GPU 组件下载窗口，支持暂停和续传。安装完成后重启生效，当前任务继续使用 CPU。'
      : '已打开离线加速安装窗口。完成启用后重新打开工作台即可使用。';
  } catch {
    el('gpu-status').textContent = '未能打开离线加速安装窗口，请重试。';
  }
};
api.onOpenUpdates(() => el('updates-tab').click());
api.onChange(render);
api.state().then(render);

api.onGpuReady?.(backend => {
  el('gpu-status').hidden = false;
  el('gpu-status').textContent = backend === 'cuda' ? 'GPU 加速组件已就绪。完成当前任务后重启 WormStudio，即可使用显卡加速。' : '已保留 CPU 环境，可稍后再安装 GPU 加速。';
  el('gpu-install').hidden = backend === 'cuda';
});

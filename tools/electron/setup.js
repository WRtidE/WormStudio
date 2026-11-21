'use strict';
const byId = id => document.getElementById(id);
const sizes = bytes => bytes >= 1024**3 ? (bytes/1024**3).toFixed(2)+' GiB' : (bytes/1024**2).toFixed(1)+' MiB';
let latest;
function render(state) {
  latest = state;
  for (const key of ['title','detail']) byId(key).textContent = state[key];
  byId('hardware').textContent = state.hardware || '正在识别…';
  byId('mode').textContent = state.backend ? (state.backend === 'cuda' ? 'GPU 加速' : '标准环境') : '检测中';
  const done = state.phase === 'ready';
  const transferring = ['downloading','paused','error'].includes(state.phase);
  const percent = state.total ? Math.min(100,Math.floor(state.received/state.total*100)) : 0;
  byId('percent').textContent = state.total ? `${percent}%` : '—';
  if (state.total) byId('progress').value = percent; else byId('progress').removeAttribute('value');
  byId('bytes').textContent = state.total ? `${sizes(state.received)} / ${sizes(state.total)}` : '正在检查组件';
  const fragment = document.createDocumentFragment();
  const groups = new Map();
  for (const item of state.items) {
    const name = item.name.split(' · ')[0];
    if (!groups.has(name)) groups.set(name,{name,size:0,done:0,states:[]});
    const group = groups.get(name);
    group.size += item.size; group.done += item.done; group.states.push(item.status);
  }
  for (const item of groups.values()) {
    item.status = item.states.every(s => s === 'cached') ? 'cached' : item.states.every(s => ['done','cached'].includes(s)) ? 'done'
      : item.states.includes('downloading') || item.done ? 'downloading' : 'waiting';
    const li = document.createElement('li');
    li.className = ['done','cached'].includes(item.status) ? 'complete' : item.status === 'downloading' ? 'current' : '';
    const name = document.createElement('span'); name.textContent = item.name;
    const status = document.createElement('span');
    status.textContent = ({done:'✓ 已就绪',cached:'✓ 已复用',waiting:'待下载'})[item.status] || `${sizes(item.done)} / ${sizes(item.size)}`;
    li.append(name,status);fragment.append(li);
  }
  byId('items').replaceChildren(fragment);
  byId('error').hidden = !state.error;byId('error').textContent = state.error || '';
  const action = byId('action');
  action.disabled = !transferring && !done;
  action.textContent = ({downloading:'暂停',paused:'继续下载',error:'重试',ready:'开始使用',detecting:'检测中…',installing:'安装中…',verifying:'验证中…'})[state.phase];
  byId('cpu').hidden = state.backend !== 'cuda' || !['error','paused'].includes(state.phase);
}
byId('action').addEventListener('click', () => window.setup.action(latest.phase === 'downloading' ? 'pause' : latest.phase === 'ready' ? 'open' : 'retry'));
byId('cpu').addEventListener('click', () => window.setup.action('cpu'));
window.setup.changed(render);
window.setup.state().then(render);

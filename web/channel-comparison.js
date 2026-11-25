'use strict';

window.createChannelComparison = function ({request, post, getFiles, openRun}) {
  const panel = document.getElementById('count-comparison');
  const table = document.getElementById('channel-matrix'), viewport = table.parentElement;
  const count = document.getElementById('matrix-count'), toggle = document.getElementById('matrix-removed');
  const message = document.getElementById('matrix-state');
  const byName = new Intl.Collator('zh-CN', {numeric:true});
  let entries = [], showRemoved = false, revision = 0;

  function render() {
    const removed = entries.filter(entry => entry.removed);
    if (!removed.length) showRemoved = false;
    const rows = showRemoved ? removed : entries.filter(entry => !entry.removed);
    count.textContent = (entries.length - removed.length) + ' 组';
    toggle.hidden = !removed.length;
    toggle.textContent = showRemoved ? '返回对照表' : '已移除（' + removed.length + '）';
    document.getElementById('matrix-hint').textContent = showRemoved
      ? '这些结果已从对照表移除。点击「恢复」可重新加入。'
      : '每个视频展示最近一次完成的计数。点击通道次数可查看越线记录。';
    const focusedRow = document.activeElement.closest('#channel-matrix .matrix-action')?.closest('tr').sectionRowIndex;
    table.replaceChildren();
    viewport.hidden = !rows.length;
    message.textContent = rows.length ? '' : removed.length ? '对照表为空，可在「已移除」中恢复数据。' : '暂无已完成的通道计数。';
    if (!rows.length) {
      if (focusedRow != null) toggle.focus();
      return;
    }

    const channels = Math.max(...rows.map(entry => entry.counts.length));
    table.style.setProperty('--matrix-channels', channels);
    const cols = document.createElement('colgroup');
    cols.append(document.createElement('col'), document.createElement('col'));
    table.append(cols);
    const head = table.createTHead().insertRow();
    for (const title of ['视频', '总次数', ...Array.from({length:channels}, (_,i) => String(i+1).padStart(2,'0'))]) {
      const th = document.createElement('th');
      th.scope = 'col'; th.textContent = title;
      if (head.cells.length >= 2) th.setAttribute('aria-label', '通道 ' + title);
      head.append(th);
    }
    const body = table.createTBody();
    for (const entry of rows) {
      const tr = body.insertRow(), name = document.createElement('th');
      name.scope = 'row';
      const label = document.createElement('span'), action = document.createElement('button');
      label.className = 'matrix-name'; label.textContent = entry.name; label.title = entry.path || entry.name;
      action.type = 'button'; action.className = showRemoved ? 'matrix-action restore' : 'matrix-action';
      action.textContent = showRemoved ? '恢复' : '移除';
      action.title = showRemoved ? '恢复到对照表' : '从对照表移除，保留计数记录';
      action.setAttribute('aria-label', action.title + ' · ' + entry.name);
      action.onclick = async () => {
        action.disabled = true;
        try {
          await post(entry.file, entry.removed ? 'restore-comparison' : 'remove-comparison', {run:entry.run});
          await refresh();
        } catch (error) {
          message.textContent = error.message;
        } finally { action.disabled = false; }
      };
      const identity = document.createElement('div');
      identity.className = 'matrix-identity'; identity.append(label, action); name.append(identity); tr.append(name);
      tr.insertCell().textContent = entry.counts.reduce((total, n) => total+n, 0);
      for (let i=0; i<channels; i++) {
        const cell = tr.insertCell();
        if (i >= entry.counts.length) { cell.textContent = '—'; cell.title = '此视频没有该通道'; continue; }
        const n = entry.counts[i], link = document.createElement('button');
        link.type = 'button'; link.className = n ? 'matrix-number has-count' : 'matrix-number';
        link.textContent = n;
        link.setAttribute('aria-label', entry.name + ' 通道 ' + (i+1) + '，' + n + ' 次');
        link.onclick = () => openRun(entry, i+1);
        cell.append(link);
      }
    }
    if (focusedRow != null) body.rows[Math.min(focusedRow, rows.length-1)].querySelector('.matrix-action').focus({preventScroll:true});
  }

  async function refresh() {
    if (!panel.open) return;
    const version = ++revision;
    if (!entries.length) message.textContent = '正在读取通道结果…';
    try {
      const records = await request('/api/channel/comparison?include_removed=1');
      if (version !== revision) return;
      const library = new Map(getFiles().map(file => [file.id, file]));
      entries = records.filter(row => library.has(row.file)).map(row => {
        const source = library.get(row.file);
        return {...row, name:source.name, path:source.path};
      }).sort((a,b) => byName.compare(a.name,b.name));
      render();
    } catch (error) {
      if (version === revision) message.textContent = error.message;
    }
  }
  toggle.onclick = () => { showRemoved = !showRemoved; render(); };
  panel.addEventListener('toggle', refresh);
  return refresh;
};

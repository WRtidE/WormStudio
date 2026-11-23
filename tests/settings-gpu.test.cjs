'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('GPU check shows progress and the result inside Settings without launching the installer', async () => {
  const elements = new Map();
  function getElementById(id) {
    if (!elements.has(id)) elements.set(id, {hidden:true, addEventListener() {}});
    return elements.get(id);
  }
  let finish, fail, ready, checks = 0, installs = 0;
  const settings = {
    state:async () => ({}), gpuAvailable:async () => true,
    onOpenUpdates() {}, onChange() {}, onGpuReady(callback) { ready = callback; },
    checkGpu() { checks++; return new Promise((resolve, reject) => { finish = resolve; fail = reject; }); },
    openGpu:async () => { installs++; return {online:true}; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../tools/electron/settings.js'), 'utf8'), {
    document:{getElementById}, window:{settings}
  });
  const button = getElementById('gpu');
  const status = getElementById('gpu-status');
  const install = getElementById('gpu-install');
  const first = button.onclick();
  await button.onclick();
  assert.equal(checks, 1);
  assert.equal(status.hidden, false);
  assert.equal(button.disabled, true);
  finish({state:'ready', message:'RTX 已通过计算检测'});
  await first;
  assert.equal(status.textContent, 'RTX 已通过计算检测');
  assert.equal(install.hidden, true);
  const next = button.onclick();
  finish({state:'available', message:'当前使用 CPU'});
  await next;
  assert.equal(install.hidden, false);
  const online = button.onclick();
  finish({state:'online', message:'下次启动准备 GPU'});
  await online;
  assert.equal(install.hidden,false);
  assert.equal(install.textContent,'下载并安装 GPU 加速');
  const retry = button.onclick();
  fail(new Error('timeout'));
  await retry;
  assert.equal(install.hidden, true);
  assert.equal(button.disabled, false);
  assert.match(status.textContent, /重新检测/);
  assert.equal(installs, 0);
  await install.onclick();
  assert.equal(installs, 1);
  assert.match(status.textContent, /支持暂停和续传/);
  ready('cuda');
  assert.match(status.textContent, /重启 WormStudio/);
  assert.equal(install.hidden, true);
});

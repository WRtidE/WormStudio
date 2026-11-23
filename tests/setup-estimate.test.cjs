'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('setup estimate distinguishes transfer, pause, stall and installation', () => {
  let now = 0;
  const estimate = {};
  const context = vm.createContext({document:{getElementById:() => estimate},
    performance:{now:() => now}, setInterval:() => {}});
  const source = fs.readFileSync(path.join(__dirname, '../tools/electron/setup.js'), 'utf8');
  vm.runInContext(source.split('function render(state)')[0], context);
  const render = state => {
    context.state = state;
    vm.runInContext('latest = state; renderEstimate();', context);
    return estimate.textContent;
  };
  const state = {phase:'downloading', total:300*1024**2, received:0, bytesPerSecond:1024**2};
  assert.match(render(state), /5 分钟/);
  assert.match(estimate.textContent, /不含安装和验证/);
  assert.match(render({...state, bytesPerSecond:0}), /正在估算/);
  assert.match(render({...state, phase:'paused'}), /暂停/);
  now = 11000;
  assert.match(render(state), /正在估算/);
  assert.match(render({...state, phase:'installing'}), /设备性能/);
  assert.equal(render({...state, phase:'ready'}), '');
});

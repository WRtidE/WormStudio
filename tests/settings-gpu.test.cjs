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
  let finish, fail, ready, gpuStatus, installAction, checks = 0, installs = 0;
  const settings = {
    state:async () => ({}), gpuAvailable:async () => true,
    onOpenUpdates() {}, onChange() {}, onGpuReady(callback) { ready = callback; },
    onGpuStatus(callback) { gpuStatus = callback; },
    checkGpu() { checks++; return new Promise((resolve, reject) => { finish = resolve; fail = reject; }); },
    openGpu:async () => { installs++; return installAction ? installAction() : {online:true}; },
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
  gpuStatus({state:'workspace',message:'检测到显卡，当前使用 CPU'});
  assert.match(install.textContent,/安装匹配的 GPU/);
  installAction = () => new Promise(resolve => { finish = resolve; });
  const installing = install.onclick();
  await install.onclick();
  assert.equal(installs, 2);
  assert.equal(install.disabled,true);
  gpuStatus({state:'installing',message:'正在下载 torch',transfer:{received:2**20,total:4*2**20}});
  assert.equal(getElementById('gpu-transfer').hidden,false);
  assert.equal(getElementById('gpu-progress').value,2**20);
  assert.match(getElementById('gpu-bytes').textContent,/25%.*1 \/ 4 MiB/);
  gpuStatus({state:'installing',message:'正在验证显卡运算'});
  assert.equal(getElementById('gpu-transfer').hidden,true);
  assert.match(status.textContent,/正在验证/);
  finish({state:'workspace',message:'下载中断，可重试'});
  await installing;
  assert.equal(install.hidden,false);
  assert.equal(install.disabled,false);
  assert.match(status.textContent,/可重试/);
});

const {workspaceGpu, gpuOffer} = require('../src/platforms/online.cjs');
test('CPU venv and online layouts can install matching CUDA; unavailable hardware and ready installations keep their status', async () => {
  const gpu = async () => ({backend:'cuda',name:'RTX'});
  for (const mode of ['online','workspace']) {
    assert.equal((await gpuOffer('incompatible','离线包不匹配',mode,gpu)).state,mode);
    assert.equal((await gpuOffer('available','CPU',mode,gpu)).state,mode);
    assert.equal((await gpuOffer('available','CPU',mode,async () => ({backend:'cpu',detail:'驱动不支持'}))).state,'cpu');
  }
  for (const state of ['cpu','ready','retry']) {
    assert.equal((await gpuOffer(state,'已有检测结果','workspace',assert.fail)).state,state);
  }
  assert.equal((await gpuOffer('incompatible','不兼容',null,assert.fail)).state,'incompatible');
});

test('a checkout publishes only a verified CUDA environment for its current lockfile', async t => {
  const os = require('node:os');
  const root = fs.mkdtempSync(path.join(os.tmpdir(),'worm-gpu-'));
  t.after(() => fs.rmSync(root,{recursive:true,force:true}));
  for (const name of ['pyproject.toml','uv.lock','.python-version']) fs.writeFileSync(path.join(root,name),'fixture');
  const cpu = path.join(root,'.venv','Scripts','python.exe');
  const withWheels = (assets, install) => install(root,path.join(root,'requirements.txt'));
  let valid = false, generated;
  const fake = {run:async (uv,args,options) => {
    assert.equal(options.cwd,root);
    if (uv === cpu) return {stdout:'[]'};
    if (args[0] !== 'sync') return;
    assert.deepEqual(args,['sync','--locked','--extra','cuda','--managed-python']);
    generated = path.join(options.env.UV_PROJECT_ENVIRONMENT,'Scripts','python.exe');
    assert.notEqual(generated,cpu);
    fs.mkdirSync(path.dirname(generated),{recursive:true});
    fs.writeFileSync(generated,'test');
  },verify:async (python,backend) => {
    assert.equal(python,generated);
    assert.equal(backend,'cuda');
    if (!valid) throw Error('probe failed');
  }};
  const setup = workspaceGpu(root,cpu,fake);
  const signal = new AbortController().signal;
  await assert.rejects(setup.install(signal,()=>{},withWheels),/probe failed/);
  assert.equal(setup.readyPython(),null);
  valid = true;
  await setup.install(signal,()=>{},withWheels);
  assert.equal(workspaceGpu(root,cpu).readyPython(),generated);
  fs.writeFileSync(path.join(root,'uv.lock'),'updated');
  assert.equal(workspaceGpu(root,cpu).readyPython(),null);
});

test('workspace GPU progress counts transferred bytes and reuses a verified release wheel', async t => {
  const os = require('node:os');
  const {createHash} = require('node:crypto');
  const {withWorkspaceWheels} = require('../tools/electron/component-download.cjs');
  const root = fs.mkdtempSync(path.join(os.tmpdir(),'worm-gpu-bytes-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const shared = path.join(root,'release');
  fs.mkdirSync(shared);
  const bytes = Buffer.from('abcdefgh');
  const wheel = (label,data) => ({label,size:data.length,filename:label+'.whl',requirement:label+'==1.0',
    sha256:createHash('sha256').update(data).digest('hex'),url:'https://example.test/'+label});
  const assets = [wheel('torch',bytes),wheel('torchvision',bytes.subarray(0,2))];
  fs.writeFileSync(path.join(shared,assets[1].sha256),bytes.subarray(0,2));
  const progress = [], total = 10;
  await withWorkspaceWheels(assets,{directory:path.join(root,'downloads'),shared,signal:new AbortController().signal,
    fetch:async url => {
      assert.equal(url,assets[0].url);
      return new Response(new ReadableStream({async start(controller) {
        await new Promise(r=>setTimeout(r,180));
        controller.enqueue(bytes.subarray(0,3));
        await new Promise(r=>setTimeout(r,180));
        controller.enqueue(bytes.subarray(3)); controller.close();
      }}),{headers:{'Content-Length':String(bytes.length)}});
    },progress:(_message,state)=>progress.push(state)},async (links,requirements) => {
      assert.deepEqual(fs.readFileSync(path.join(links,'torch.whl')),bytes);
      assert.match(fs.readFileSync(requirements,'utf8'),/torchvision==1.0 --hash=sha256:/);
    });
  assert.ok(progress.some(p=>p.received===3 && p.total===total));
  assert.deepEqual(progress.at(-1),{received:total,total});
});

'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {EventEmitter} = require('node:events');
const {pathToFileURL} = require('node:url');
const {download} = require('../tools/electron/component-download.cjs');
const {Setup, runtimeId, validateManifest} = require('../tools/electron/online-setup.cjs');
const {detect} = require('../src/platforms/online.cjs');
const {showSetup} = require('../tools/electron/setup-window.cjs');

function asset(text, name='component') {
  const bytes = Buffer.from(text);
  return {label:name, url:'https://downloads.example.org/'+name, size:bytes.length, unpackedSize:bytes.length,
    sha256:createHash('sha256').update(bytes).digest('hex')};
}
async function folder(t) {
  const directory = await fs.promises.mkdtemp(path.join(os.tmpdir(),'worm-online-'));
  t.after(() => fs.promises.rm(directory,{recursive:true,force:true}));
  return directory;
}
test('partial downloads resume from persisted bytes, are hash checked and work offline afterwards', async t => {
  const directory = await folder(t), bytes = Buffer.from('an interrupted component'), record = asset(bytes);
  const partial = path.join(directory,record.sha256+'.partial');
  await fs.promises.writeFile(partial,bytes.subarray(0,8));
  const fetch = async (_url,options) => {
    assert.equal(options.headers.Range,'bytes=8-');
    return new Response(bytes.subarray(8),{status:206,headers:{'content-range':`bytes 8-${bytes.length-1}/${bytes.length}`}});
  };
  const file = await download(record,directory,{fetch});
  assert.deepEqual(await fs.promises.readFile(file),bytes);
  await download(record,directory,{fetch:() => assert.fail('verified component must not use network')});
});
test('pause during a transfer preserves bytes and the next attempt resumes', async t => {
  const directory = await folder(t), bytes = Buffer.from('0123456789abcdef'), record = asset(bytes);
  const abort = new AbortController();
  const first = new ReadableStream({start(controller) {controller.enqueue(bytes.subarray(0,5));controller.enqueue(bytes.subarray(5));controller.close();}});
  await assert.rejects(download(record,directory,{fetch:async () => new Response(first),signal:abort.signal,
    progress:done => {if (done>=5) abort.abort();}}));
  const saved = await fs.promises.stat(path.join(directory,record.sha256+'.partial'));
  assert.equal(saved.size,5);
  await download(record,directory,{fetch:async (_url,options) => {
    assert.equal(options.headers.Range,'bytes=5-');
    return new Response(bytes.subarray(5),{status:206,headers:{'content-range':'bytes 5-15/16'}});
  }});
});
test('server ignoring Range starts a fresh file instead of appending', async t => {
  const directory = await folder(t), record = asset('correct contents');
  await fs.promises.writeFile(path.join(directory,record.sha256+'.partial'),'wrong');
  const file = await download(record,directory,{fetch:async () => new Response('correct contents')});
  assert.equal(await fs.promises.readFile(file,'utf8'),'correct contents');
});
test('wrong ranges, lengths and checksums never become installed components', async t => {
  const directory = await folder(t), record = asset('abcd');
  const partial = path.join(directory,record.sha256+'.partial');
  await fs.promises.writeFile(partial,'a');
  await assert.rejects(download(record,directory,{fetch:async () => new Response('bcd',{status:206,headers:{'content-range':'bytes 0-2/4'}})}),/续传范围/);
  assert.equal(await fs.promises.readFile(partial,'utf8'),'a');
  await assert.rejects(download(record,directory,{fetch:async () => new Response('abcd',{headers:{'content-length':'99'}})}),/长度/);
  await assert.rejects(download(record,directory,{fetch:async () => new Response('xxxx')}),/校验失败/);
  assert.equal(fs.existsSync(partial),false);
  assert.equal(fs.existsSync(path.join(directory,record.sha256)),false);
});
test('a completed partial is verified without requesting an unsatisfiable range', async t => {
  const directory = await folder(t), record = asset('abcd');
  await fs.promises.writeFile(path.join(directory,record.sha256+'.partial'),'abcd');
  await download(record,directory,{fetch:() => assert.fail('no request expected')});
});
test('cancellation during cache verification preserves the partial without activating it', async t => {
  const directory = await folder(t), record = asset('abcd');
  const partial = path.join(directory,record.sha256+'.partial');
  await fs.promises.writeFile(partial,'abcd');
  const abort = new AbortController(), createReadStream = fs.createReadStream;
  t.mock.method(fs,'createReadStream',(...args) => {
    const stream = createReadStream(...args);
    stream.once('data',() => abort.abort());
    return stream;
  });
  await assert.rejects(download(record,directory,{signal:abort.signal,fetch:() => assert.fail('no request expected')}),{name:'AbortError'});
  assert.equal(fs.existsSync(path.join(directory,record.sha256)),false);
  assert.equal(await fs.promises.readFile(partial,'utf8'),'abcd');
});
test('a truncated connection saves progress and does not publish an incomplete file', async t => {
  const directory = await folder(t), record = asset('abcdef');
  await assert.rejects(download(record,directory,{fetch:async () => new Response('abc')}),/连接中断/);
  assert.equal(await fs.promises.readFile(path.join(directory,record.sha256+'.partial'),'utf8'),'abc');
  assert.equal(fs.existsSync(path.join(directory,record.sha256)),false);
});
test('an idle network request times out and leaves a retryable error', async t => {
  const directory = await folder(t), record = asset('abcdef');
  await assert.rejects(download(record,directory,{idleMs:10,fetch:(_url,{signal}) => new Promise((resolve,reject) => {
    signal.addEventListener('abort',() => reject(signal.reason),{once:true});
  })}),/超时/);
});
test('mirror failure resumes the same verified bytes from a fallback source', async t => {
  const directory = await folder(t), record = asset('abcdef');
  record.fallbackUrls = ['https://fallback.example.org/component'];
  const requests = [], switches = [];
  const file = await download(record,directory,{onRetry:(...urls)=>switches.push(urls),fetch:async(url,options)=>{
    requests.push([url,options.headers.Range]);
    if(url===record.url)return new Response('abc');
    assert.equal(options.headers.Range,'bytes=3-');
    return new Response('def',{status:206,headers:{'content-range':'bytes 3-5/6'}});
  }});
  assert.equal(await fs.promises.readFile(file,'utf8'),'abcdef');
  assert.equal(requests.length,2);
  assert.deepEqual(switches,[[record.url,record.fallbackUrls[0]]]);
});
test('corrupt mirror data is discarded before the fallback is accepted', async t => {
  const directory = await folder(t), record = asset('abcd');
  record.fallbackUrls = ['https://fallback.example.org/component'];
  const file = await download(record,directory,{fetch:async(url,options)=>{
    if(url===record.url)return new Response('xxxx');
    assert.equal(options.headers.Range,undefined);
    return new Response('abcd');
  }});
  assert.equal(await fs.promises.readFile(file,'utf8'),'abcd');
});
test('pausing or a disk error does not start another mirror request', async t => {
  const directory = await folder(t), record = asset('abcd');
  record.fallbackUrls = ['https://fallback.example.org/component'];
  const abort = new AbortController();let requests=0;
  await assert.rejects(download(record,directory,{signal:abort.signal,fetch:async()=>{
    requests++;abort.abort();throw abort.signal.reason;
  }}));
  assert.equal(requests,1);
  t.mock.method(fs.promises,'open',async()=>{throw Object.assign(Error('disk full'),{code:'ENOSPC'});});
  requests=0;
  await assert.rejects(download(record,directory,{fetch:async()=>{requests++;return new Response('abcd');}}),{code:'ENOSPC'});
  assert.equal(requests,1);
});
test('hardware detection selects compatible NVIDIA, old drivers use CPU, Mac uses native environment', async () => {
  assert.equal((await detect('win32-x64',async () => ({stdout:'NVIDIA RTX 4070, 596.21, 8.9\n'}))).backend,'cuda');
  assert.equal((await detect('win32-x64',async () => ({stdout:'NVIDIA GTX 1080, 512.00, 6.1\n'}))).backend,'cpu');
  assert.equal((await detect('win32-x64',async () => {throw Object.assign(Error('missing'),{code:'ENOENT'});})).backend,'cpu');
  assert.equal((await detect('darwin-arm64')).backend,'cpu');
  await assert.rejects(detect('win32-x64',async () => {throw Object.assign(Error('bad invocation'),{code:'ERR_INVALID_ARG_TYPE'});}),/bad invocation/);
});

function fixture() {
  const contents = new Map([['common','base'],['cpu','cpu runtime'],['cuda','cuda runtime'],['model','model weights']]);
  const manifest = {schema:1,target:'win32-x64',common:[asset(contents.get('common'),'common')],
    backends:{cpu:[asset(contents.get('cpu'),'cpu')],cuda:[asset(contents.get('cuda'),'cuda')]},
    model:{...asset(contents.get('model'),'model'),filename:'sam2_hiera_large.pt'}};
  let requests = [], extracts = 0, probes = 0, failProbe = false;
  const platform = {host:() => ({target:'win32-x64',python:'python/python.exe'}),detect:async () => ({name:'Test NVIDIA',backend:'cuda',detail:'Detected'}),
    extract:async (_file,dest) => {extracts++; await fs.promises.mkdir(path.join(dest,'python'),{recursive:true});await fs.promises.writeFile(path.join(dest,'python/python.exe'),'runtime');},
    probe:async () => {probes++;if (failProbe) throw Error('GPU probe failed');}};
  const fetch = async url => {const name = new URL(url).pathname.slice(1);requests.push(name);return new Response(contents.get(name));};
  return {manifest,platform,fetch,get requests() {return requests;},get extracts() {return extracts;},get probes() {return probes;},
    clear:() => {requests=[];},fail:() => {failProbe=true;}};
}
test('automatic GPU setup downloads one backend, atomically activates and reuses components across app versions', async t => {
  const directory = await folder(t), f = fixture();
  const setup = new Setup({...f,directory});
  const result = await setup.start();
  assert.equal(result.backend,'cuda');
  assert.deepEqual(f.requests,['common','cuda','model']);
  assert.equal(f.probes,1);
  assert.ok(fs.existsSync(path.join(result.runtime,'ready.json')));
  f.clear();
  const update = new Setup({...f,directory,manifest:{...f.manifest,appVersion:'9.0.0'}});
  assert.deepEqual(await update.start(),result);
  assert.deepEqual(f.requests,[]);
  assert.equal(f.extracts,2);
  assert.equal(update.state.reused,true);
});
test('mirrored GPU wheels install after the common interpreter and reuse across source changes', async t => {
  const directory = await folder(t), f = fixture();let wheels=0;
  f.manifest.backends.cuda[0].format='wheel';
  f.platform.extractWheel=async(file,destination)=>{
    assert.ok(fs.existsSync(path.join(destination,'python/python.exe')));
    assert.equal(await fs.promises.readFile(file,'utf8'),'cuda runtime');wheels++;
  };
  const setup=new Setup({...f,directory});
  const result=await setup.start();assert.ok(result,setup.state.error);
  assert.equal(wheels,1);assert.equal(f.extracts,1);assert.equal(f.probes,1);
  f.clear();f.manifest.backends.cuda[0].url='https://new.example.org/cuda';
  f.manifest.backends.cuda[0].fallbackUrls=['https://fallback.example.org/cuda'];
  const update=new Setup({...f,directory});
  assert.deepEqual(await update.start(),result);assert.deepEqual(f.requests,[]);
  f.manifest.backends.cuda[0].fallbackUrls=['http://insecure.example.org/cuda'];
  assert.throws(()=>validateManifest(f.manifest,'win32-x64'),/组件清单/);
  delete f.manifest.backends.cuda[0].fallbackUrls;
  f.manifest.common[0].format='wheel';
  assert.throws(()=>validateManifest(f.manifest,'win32-x64'),/格式/);
});
test('failed GPU validation does not activate; CPU retry reuses common/model downloads and persists choice', async t => {
  const directory = await folder(t), f = fixture();f.fail();
  const setup = new Setup({...f,directory});
  assert.equal(await setup.start(),null);
  assert.equal(setup.state.phase,'error');
  assert.equal(fs.existsSync(path.join(directory,'runtimes',runtimeId(f.manifest,'cuda').slice(0,24))),false);
  assert.deepEqual(await fs.promises.readdir(path.join(directory,'runtimes')),[]);
  f.platform.probe = async () => {};
  f.clear();
  const result = await setup.start(true);
  assert.equal(result.backend,'cpu');
  assert.deepEqual(f.requests,['cpu']);
  f.clear();
  assert.equal((await new Setup({...f,directory}).start()).backend,'cpu');
  assert.deepEqual(f.requests,[]);
});
test('model and environment revisions are independent and invalid manifests reject before network', () => {
  const {manifest} = fixture();
  assert.equal(runtimeId(manifest,'cpu'), runtimeId({...manifest,model:asset('new model')},'cpu'));
  assert.notEqual(runtimeId(manifest,'cpu'),runtimeId(manifest,'cuda'));
  assert.throws(() => validateManifest({...manifest,target:'darwin-arm64'},'win32-x64'),/不匹配/);
  assert.throws(() => validateManifest({...manifest,model:{...manifest.model,filename:'../escape.pt'}},'win32-x64'));
  assert.throws(() => validateManifest({...manifest,common:[{...manifest.common[0],url:'http://insecure.test/a'}]},'win32-x64'));
  assert.throws(() => validateManifest({...manifest,common:[{...manifest.common[0],unpackedSize:undefined}]},'win32-x64'),/解压大小/);
  assert.throws(() => validateManifest({...manifest,model:{...manifest.model,label:null}},'win32-x64'),/清单无效/);
});
test('a failed new environment leaves the previously verified environment intact', async t => {
  const directory = await folder(t), f = fixture();
  const setup = new Setup({...f,directory});
  const previous = await setup.start(true);
  assert.ok(previous,setup.state.error);
  await fs.promises.rm(path.join(directory,'prefer-cpu'));
  f.fail();
  assert.equal(await new Setup({...f,directory}).start(),null);
  assert.equal(JSON.parse(await fs.promises.readFile(path.join(previous.runtime,'ready.json'))).backend,'cpu');
  assert.ok(fs.existsSync(path.join(previous.runtime,'python/python.exe')));
});

test('a failed retry clears the previous launch result while retaining its verified files', async t => {
  const directory = await folder(t), f = fixture(), setup = new Setup({...f,directory});
  const previous = await setup.start(true);
  await fs.promises.rm(path.join(directory,'prefer-cpu'));
  f.fail();
  assert.equal(await setup.start(),null);
  assert.equal(setup.result,null);
  assert.equal(setup.state.phase,'error');
  assert.ok(fs.existsSync(path.join(previous.runtime,'ready.json')));
});

test('stopping when native validation finishes does not activate the staged environment', async t => {
  const directory = await folder(t), f = fixture(), setup = new Setup({...f,directory});
  f.platform.probe = async () => {setup.stop();};
  assert.equal(await setup.start(),null);
  assert.equal(setup.result,null);
  assert.equal(setup.state.phase,'paused');
  assert.deepEqual(await fs.promises.readdir(path.join(directory,'runtimes')),[]);
});

test('cleanup failure reports both errors without rejecting the background setup task', async t => {
  const directory = await folder(t), f = fixture(); f.fail();
  const rm = fs.promises.rm;
  const mock = t.mock.method(fs.promises,'rm',async (file,options) => {
    if (path.basename(file).startsWith('.preparing-')) throw Object.assign(Error('directory is busy'),{code:'EPERM'});
    return rm(file,options);
  });
  const setup = new Setup({...f,directory});
  try {
    assert.equal(await setup.start(),null);
    assert.equal(setup.state.phase,'error');
    assert.match(setup.state.error,/GPU probe failed/);
    assert.match(setup.state.error,/清理失败.*directory is busy/);
    assert.match(await fs.promises.readFile(path.join(directory,'setup-error.log'),'utf8'),/GPU probe failed[\s\S]*directory is busy/);
  } finally {mock.mock.restore();}
});

function electronFixture() {
  const handlers = new Map();
  class BrowserWindow extends EventEmitter {
    constructor() {
      super(); this.destroyed = false;
      this.webContents = Object.assign(new EventEmitter(),{
        mainFrame:{}, send() {}, setWindowOpenHandler() {}
      });
    }
    isDestroyed() {return this.destroyed;}
    async loadFile(file) {this.webContents.mainFrame.url = pathToFileURL(file).href;}
    show() {}
    destroy() {this.destroyed = true;this.emit('closed');}
  }
  return {BrowserWindow,ipcMain:{handle:(name,fn) => handlers.set(name,fn),removeHandler:name => handlers.delete(name)},handlers};
}

test('manual launch failure cancels automatic launch and rejects premature or foreign IPC requests', async t => {
  const directory = await folder(t), f = fixture(), electron = electronFixture();
  t.mock.timers.enable({apis:['setTimeout']});
  let launches = 0;
  const {window,setup} = await showSetup({...f,directory,electron,onReady:async () => {launches++;throw Error('backend unavailable');}});
  const event = {sender:window.webContents,senderFrame:window.webContents.mainFrame};
  const action = electron.handlers.get('setup:action');
  assert.throws(() => action(event,'open'),/无效的准备操作/);
  await setup.running;
  assert.equal(setup.state.phase,'ready');
  assert.throws(() => action({...event,sender:{}},'open'),/无效的准备请求/);
  assert.throws(() => action({...event,senderFrame:{url:event.senderFrame.url}},'open'),/无效的准备请求/);
  await action(event,'open');
  assert.equal(setup.state.phase,'error');
  t.mock.timers.tick(1500);
  assert.equal(launches,1);
  assert.throws(() => action(event,'open'),/无效的准备操作/);
  window.destroy();
  assert.equal(electron.handlers.size,0);
});

test('closing the setup window cancels pending automatic launch', async t => {
  const directory = await folder(t), f = fixture(), electron = electronFixture();
  t.mock.timers.enable({apis:['setTimeout']});
  const {window,setup} = await showSetup({...f,directory,electron,onReady:() => assert.fail('closed window must not launch')});
  await setup.running;
  assert.equal(setup.state.phase,'ready');
  window.destroy();
  t.mock.timers.tick(1500);
  assert.equal(electron.handlers.size,0);
});

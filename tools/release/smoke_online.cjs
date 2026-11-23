'use strict';
// Integration harness: real release components over a loopback HTTP transport.
// Production manifests/HTTPS policy are unchanged; only fetch is injected here.
// node tools/release/smoke_online.cjs DISTRIBUTION PROFILE [cpu|gpu] [COMPONENT_DIRECTORY ...]
// Additional directories contain reused components or the original GPU wheels.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {Transform} = require('node:stream');
const {pipeline} = require('node:stream/promises');
const assert = require('node:assert/strict');
const {Setup} = require('../electron/online-setup.cjs');
const platform = require('../../src/platforms/online.cjs');
const [distribution, profile, ...extra] = process.argv.slice(2).map((v,i) => i<2 ? path.resolve(v) : v);
const mode = extra.includes('gpu') ? 'gpu' : 'cpu';
const manifest = JSON.parse(fs.readFileSync(path.join(distribution,'WormStudio/resources/analysis/online-manifest.json'),'utf8'));
const requests = [];
let paused = false, ui, server;
const directory = path.join(profile,'components');
async function main() {
  fs.mkdirSync(profile,{recursive:true});
  const roots = [path.join(distribution,'components'), ...extra.filter(v=>!['cpu','gpu'].includes(v)).map(v=>path.resolve(v))];
  const assets = [...manifest.common,...Object.values(manifest.backends).flat(),manifest.model];
  const files = new Map(assets.map(asset=>{
    const name = decodeURIComponent(new URL(asset.url).pathname.split('/').pop());
    const file = roots.map(root=>path.join(root,name)).find(file=>fs.existsSync(file));
    if(!file)throw Error('Missing local component; pass its component directory: '+name);
    return [name,file];
  }));
  server = http.createServer(async (req,res) => {
    const name = decodeURIComponent(new URL(req.url,'http://localhost').pathname.slice(1));
    if (path.basename(name) !== name) { res.writeHead(400).end(); return; }
    const file = files.get(name);
    if(!file){res.writeHead(404).end();return;}
    const size = (await fs.promises.stat(file)).size;
    const range = req.headers.range;
    const start = range ? Number(/^bytes=(\d+)-$/.exec(range)[1]) : 0;
    requests.push({name,start});
    fs.writeFileSync(path.join(profile,'requests.json'),JSON.stringify(requests,null,2));
    res.writeHead(range?206:200,{'content-length':size-start, ...(range?{'content-range':`bytes ${start}-${size-1}/${size}`}:{})});
    const throttle = new Transform({transform(chunk,_encoding,next) {
      if (!paused && requests.length===1) setTimeout(() => next(null,chunk),20);
      else next(null,chunk);
    }});
    try { await pipeline(fs.createReadStream(file,{start}),throttle,res); }
    catch (error) { if (!['ERR_STREAM_PREMATURE_CLOSE','ECONNRESET'].includes(error.code)) throw error; }
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const fetchLocal = (url,options) => fetch(`http://127.0.0.1:${server.address().port}/${new URL(url).pathname.split('/').pop()}`, options);
  if (process.versions.electron) {
    const electron = require('electron');
    electron.app.setPath('userData',profile);
    await electron.app.whenReady();
    const {showSetup} = require('../electron/setup-window.cjs');
    ui = await showSetup({electron,manifest,directory,platform,gpu:mode==='gpu',fetch:fetchLocal,autoOpen:false,
      onReady:async result => {fs.writeFileSync(path.join(profile,'result.json'),JSON.stringify(result,null,2));}});
    const timer = setInterval(() => {
      if (!paused && ui.setup.state.phase==='downloading' && ui.setup.state.items.some(item => item.status==='downloading' && item.done>=8*1024**2)) {
        paused=true;ui.setup.pause();
      }
      fs.writeFileSync(path.join(profile,'state.json'),JSON.stringify(ui.setup.state));
      if (ui.setup.result) fs.writeFileSync(path.join(profile,'result.json'),JSON.stringify(ui.setup.result));
    },250);
    electron.app.on('window-all-closed', () => {clearInterval(timer);server.close();void ui.setup.stop()?.finally(() => electron.app.quit());if (!ui.setup.running) electron.app.quit();});
    return;
  }
  const setup = new Setup({manifest,directory,platform,gpu:mode==='gpu',fetch:fetchLocal,notify:state => {
    if (!paused && state.phase==='downloading' && state.items.some(item => item.status==='downloading' && item.done>=8*1024**2)) {paused=true;setup.pause();}
  }});
  let result = await setup.start(mode==='cpu');
  if (!result && setup.state.phase==='paused') result = await setup.start(mode==='cpu');
  assert.ok(result, setup.state.error);
  if (requests.length) assert.ok(requests.some(r => r.start>0),'expected actual Range request after interruption');
  fs.writeFileSync(path.join(profile,'result.json'),JSON.stringify(result,null,2));
  const offline = new Setup({manifest,directory,platform,fetch:() => assert.fail('ready launch used network')});
  assert.deepEqual(await offline.start(),result);
  fs.writeFileSync(path.join(profile,'smoke.json'),JSON.stringify({result,requests,offlineReuse:true,resumed:requests.some(r=>r.start>0)},null,2));
  console.log('Verified '+(requests.some(r=>r.start>0) ? 'resumed HTTP downloads' : 'cached components')+', extracted runtime, native operations and offline reuse.');
  server.close();
}
main().catch(error => {console.error(error);server?.close();process.exitCode=1;});

'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {Updates, findUpdate} = require('../tools/electron/updates.cjs');
const bytes = Buffer.from('offline installer');
const asset = {name:'WormStudio-Mac-arm64.zip', browser_download_url:'https://example.org/app.zip',
  size:bytes.length, digest:'sha256:'+createHash('sha256').update(bytes).digest('hex')};
const release = {tag_name:'v1.10.0', body:'更新说明', assets:[asset]};

test('restart installation requires a verified ZIP and serializes install requests', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(),'worm-install-'));
  t.after(() => fs.rmSync(dir, {recursive:true,force:true}));
  let finish, calls = 0;
  const updates = new Updates({directory:dir, downloads:dir, platform:'win32', arch:'x64', notify(){},
    installer:async record => { calls++; assert.equal(record.archive, 'verified.zip'); await new Promise(resolve => { finish = resolve; }); }});
  updates.cache.release = {...asset, name:'WormStudio-Windows-x64.zip', version:'1.10.0'};
  updates.downloaded = 'verified.zip';
  updates.transfer = {status:'done'};
  assert.equal(updates.state().canInstall, true);
  const running = updates.install();
  assert.equal(updates.state().installing, true);
  await updates.install();
  assert.equal(calls, 1);
  finish(); await running;
  updates.cache.release.digest = null;
  assert.equal(updates.state().canInstall, false);
  await updates.install();
  assert.equal(calls, 1);
});
test('selects the highest stable newer installer for this OS and architecture', () => {
  const list = [release, {...release, tag_name:'v1.9.0'}, {...release, tag_name:'v2.0.0', prerelease:true},
    {...release, tag_name:'v2.1.0', assets:[{...asset, name:'source.zip'}]}];
  assert.equal(findUpdate(list,'darwin','arm64').version, '1.10.0');
  assert.equal(findUpdate(list,'win32','x64'), null);
  assert.equal(findUpdate([{...release, tag_name:'v1.0.0'}],'darwin','arm64'), null);
});
test('daily auto check, persistent opt-out, manual refresh and offline last known release', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(),'worm-update-'));
  t.after(() => fs.rmSync(dir, {recursive:true,force:true}));
  let calls = 0, failure = false;
  const options = {directory:dir,downloads:dir,platform:'darwin',arch:'arm64',notify(){},
    fetch:async () => { calls++; return failure ? new Response(null,{status:404}) : Response.json([release]); }};
  const updates = new Updates(options);
  await updates.startup(); await new Updates(options).startup();
  assert.equal(calls,1);
  updates.setAuto(false);
  const next = new Updates(options);
  assert.equal(next.state().auto,false);
  failure = true;
  await next.check();
  assert.equal(calls,2); assert.match(next.state().error,/404/);
  assert.equal(next.state().release.notes,'更新说明');
});
test('saves complete verified bytes, avoids overwrites and discards corrupted downloads', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(),'worm-download-'));
  t.after(() => fs.rmSync(dir, {recursive:true,force:true}));
  const app = new Updates({directory:dir,downloads:dir,platform:'darwin',arch:'arm64',notify(){},
    fetch:async url => url===asset.browser_download_url ? new Response(bytes) : Response.json([release])});
  await app.check(); await app.download(); await app.download();
  assert.deepEqual(fs.readFileSync(app.downloaded),bytes);
  assert.equal(path.basename(app.downloaded),'WormStudio-Mac-arm64 (1).zip');
  app.fetch = async () => new Response(Buffer.alloc(bytes.length));
  await app.download();
  assert.equal(app.state().transfer.status,'error');
  assert.equal(fs.readdirSync(dir).filter(n => n.endsWith('.part') || n.includes('(2)')).length,0);
});

test('preserves existing downloads on volumes without hard links', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'worm-no-links-'));
  t.after(() => fs.rmSync(dir, {recursive:true, force:true}));
  const existing = path.join(dir, asset.name);
  fs.writeFileSync(existing, 'previous download');
  t.mock.method(fs.promises, 'link', async () => {
    throw Object.assign(new Error('Hard links unavailable'), {code:'EPERM'});
  });
  const app = new Updates({directory:dir, downloads:dir, platform:'darwin', arch:'arm64', notify(){},
    fetch:async url => url === asset.browser_download_url ? new Response(bytes) : Response.json([release])});
  await app.check();
  await app.download();
  assert.equal(app.state().transfer.status, 'done');
  assert.equal(fs.readFileSync(existing, 'utf8'), 'previous download');
  assert.equal(path.basename(app.downloaded), 'WormStudio-Mac-arm64 (1).zip');
  assert.deepEqual(fs.readFileSync(app.downloaded), bytes);
});

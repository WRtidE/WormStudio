'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {createHash, randomUUID} = require('node:crypto');
const {Transform, Readable} = require('node:stream');
const {pipeline} = require('node:stream/promises');
const {version: current} = require('./package.json');
const github = 'https://api.github.com/repos/WRtidE/WormStudio/releases';
const day = 24 * 60 * 60 * 1000;

// Public releases use stable three-part version numbers, optionally prefixed with v.
function versionParts(value) {
  const match = /^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(value);
  return match && match.slice(1).map(Number);
}
function compare(a, b) {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] - b[i];
  return 0;
}
function packageName(name, platform, arch) {
  const match = /^WormStudio[-_](Mac|macOS|Windows|win32)[-_](arm64|x64|universal)(?:[-_]v?\d+\.\d+\.\d+)?\.(zip|exe|dmg)$/i.exec(name);
  if (!match) return false;
  const [,os,cpu,ext] = match.map(v => v.toLowerCase());
  return platform === 'darwin' ? ['mac','macos'].includes(os) && [arch,'universal'].includes(cpu) && ['zip','dmg'].includes(ext)
    : platform === 'win32' && ['windows','win32'].includes(os) && cpu === arch && ['zip','exe'].includes(ext);
}
function findUpdate(releases, platform, arch) {
  if (!Array.isArray(releases)) throw Error('更新信息格式错误');
  let best;
  for (const r of releases) {
    const parts = versionParts(r.tag_name);
    if (r.draft || r.prerelease || !parts || compare(parts, versionParts(current)) <= 0) continue;
    if (best && compare(parts, versionParts(best.version)) <= 0) continue;
    const asset = (r.assets || []).find(a => packageName(a.name, platform, arch));
    if (!asset) continue;
    const url = new URL(asset.browser_download_url);
    if (url.protocol !== 'https:' || url.username || url.password) throw Error('安装包下载链接无效');
    best = {version: parts.join('.'), notes: String(r.body || '').trim(), name: asset.name,
      url: url.href, size: asset.size, digest: asset.digest || null};
  }
  return best || null;
}

class Updates {
  constructor({directory, downloads, fetch, notify, feed = github, platform = process.platform, arch = process.arch}) {
    Object.assign(this, {downloads, fetch, notify, feed, platform, arch});
    this.file = path.join(directory, 'software-updates.json');
    this.cache = {auto: true, checked: 0, attempted: 0, release: null};
    try {
      const saved = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      if (typeof saved.auto === 'boolean') this.cache.auto = saved.auto;
      if (saved.install === current && saved.platform === platform && saved.arch === arch && saved.feed === feed) {
        Object.assign(this.cache, {checked: saved.checked, attempted: saved.attempted, release: saved.release, error:saved.error});
      }
    } catch { /* First launch or unreadable settings: check again. */ }
    this.checking = false;
    this.error = this.cache.error || '';
    this.transfer = null;
  }
  state() {
    const {auto, checked, release} = this.cache;
    return {current, auto, checked, checking: this.checking, error: this.error,
      release: release && {version: release.version, notes: release.notes}, transfer: this.transfer};
  }
  save() {
    const tmp = this.file + '.' + randomUUID();
    try {
      fs.mkdirSync(path.dirname(this.file), {recursive:true});
      fs.writeFileSync(tmp, JSON.stringify({...this.cache, error:this.error, feed:this.feed, install:current, platform:this.platform, arch:this.arch}));
      fs.renameSync(tmp, this.file);
    } finally { fs.rmSync(tmp, {force:true}); }
  }
  setAuto(value) {
    if (typeof value !== 'boolean') throw Error('检查更新设置无效');
    this.cache.auto = value;
    this.save();
    this.notify();
  }
  startup() {
    if (this.cache.auto && Date.now() - (this.cache.attempted || 0) >= day) return this.check();
  }
  async check() {
    if (this.checking || this.transfer?.status === 'loading') return;
    this.checking = true;
    this.error = '';
    this.notify();
    try {
      this.cache.attempted = Date.now();
      if (new URL(this.feed).protocol !== 'https:') throw Error('更新源地址无效');
      const response = await this.fetch(this.feed, {signal: AbortSignal.timeout(15000), cache:'no-store',
        headers: {Accept:'application/json'}});
      if (response.status === 404) throw Error('更新源尚未公开或地址不存在（HTTP 404）');
      if (!response.ok) throw Error('更新源暂不可用（HTTP '+response.status+'）');
      const release = findUpdate(await response.json(), this.platform, this.arch);
      if (this.cache.release?.url !== release?.url || this.cache.release?.version !== release?.version) this.transfer = null;
      Object.assign(this.cache, {release, checked:Date.now()});
    } catch (err) {
      this.error = /^更新|^安装包/.test(err.message) ? err.message : '检查失败，请检查网络后重试';
    } finally {
      this.checking = false;
      try { this.save(); } catch { this.error = '更新设置保存失败'; }
      this.notify();
    }
  }
  async download() {
    const release = this.cache.release;
    if (!release || this.checking || this.transfer?.status === 'loading') return;
    this.transfer = {status:'loading', percent:null};
    this.notify();
    const temp = path.join(this.downloads, '.WormStudio-' + randomUUID() + '.part');
    try {
      if (!packageName(release.name, this.platform, this.arch) || new URL(release.url).protocol !== 'https:') throw Error('无效的安装包');
      await fs.promises.mkdir(this.downloads, {recursive:true});
      const abort = new AbortController();
      let timer;
      const reset = () => { clearTimeout(timer); timer = setTimeout(() => abort.abort(), 60000); };
      const hash = createHash('sha256');
      let received = 0, last = 0;
      try {
        reset();
        const response = await this.fetch(release.url, {signal:abort.signal});
        if (!response.ok || !response.body) throw Error('下载请求失败');
        const total = release.size || Number(response.headers.get('content-length'));
        const meter = new Transform({transform:(chunk, encoding, next) => {
          received += chunk.length;
          hash.update(chunk);
          reset();
          if (total && Date.now() - last > 300) {
            last = Date.now();
            this.transfer.percent = Math.min(99, Math.floor(received * 100 / total));
            this.notify();
          }
          next(null, chunk);
        }});
        await pipeline(Readable.fromWeb(response.body), meter, fs.createWriteStream(temp, {flags:'wx'}));
        if (!received || (total && received !== total)) throw Error('安装包下载不完整');
      } finally { clearTimeout(timer); }
      if (release.digest && release.digest !== 'sha256:' + hash.digest('hex')) throw Error('安装包校验失败');
      const {name, ext} = path.parse(release.name);
      let destination;
      for (let i = 0; ; i++) {
        destination = path.join(this.downloads, name + (i ? ' (' + i + ')' : '') + ext);
        try { await fs.promises.link(temp, destination); break; }
        catch (err) { if (err.code !== 'EEXIST') throw err; }
      }
      this.downloaded = destination;
      this.transfer = {status:'done'};
    } catch (err) {
      this.transfer = {status:'error', message: /^安装包/.test(err.message) ? err.message : '下载失败，请检查网络和磁盘空间后重试'};
    } finally {
      await fs.promises.rm(temp, {force:true}).catch(() => {});
      this.notify();
    }
  }
}
module.exports = {Updates, findUpdate};

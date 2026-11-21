'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {createHash, randomUUID} = require('node:crypto');
const {download, validateAsset} = require('./component-download.cjs');

function validateManifest(manifest, target) {
  if (manifest.schema !== 1 || manifest.target !== target || !Array.isArray(manifest.common) || !manifest.common.length ||
      !Array.isArray(manifest.backends?.cpu) || !manifest.backends.cpu.length || !/^[\w.-]+\.pt$/.test(manifest.model?.filename)) {
    throw Error('安装器与当前电脑不匹配，或组件清单损坏');
  }
  validateAsset(manifest.model);
  for (const assets of [manifest.common, ...Object.values(manifest.backends)]) {
    if (!Array.isArray(assets) || !assets.length) throw Error('运行组件清单不完整');
    for (const asset of assets) {
      validateAsset(asset);
      if (!Number.isSafeInteger(asset.unpackedSize) || asset.unpackedSize <= 0) throw Error('组件解压大小无效');
    }
  }
}
function runtimeId(manifest, backend) {
  return createHash('sha256').update(JSON.stringify([manifest.target,
    ...manifest.common.map(a => a.sha256), ...manifest.backends[backend].map(a => a.sha256)])).digest('hex');
}
class Setup {
  constructor({manifest, directory, platform, fetch, notify = () => {}}) {
    validateManifest(manifest, platform.host().target);
    Object.assign(this, {manifest, directory, platform, fetch, notify});
    this.state = {phase:'detecting', title:'正在检测设备', detail:'自动选择合适的运行环境。', items:[], received:0, total:0};
  }
  update(values) { Object.assign(this.state, values); this.notify(this.state); }
  pause() { if (this.state.phase === 'downloading') this.abort?.abort(); }
  stop() { this.abort?.abort(); return this.running; }
  async start(cpu = false) {
    if (this.running) return this.running;
    this.running = this.prepare(cpu).finally(() => { this.running = null; });
    return this.running;
  }
  async prepare(cpu) {
    this.abort = new AbortController();
    this.result = null;
    const signal = this.abort.signal;
    let stage;
    try {
      this.update({phase:'detecting', title:'正在检测设备', detail:'自动选择合适的运行环境。',
        error:'', hardware:'', backend:'', reused:false, received:0, total:0, items:[]});
      const preference = path.join(this.directory, 'prefer-cpu');
      if (cpu) { await fs.promises.mkdir(this.directory, {recursive:true}); await fs.promises.writeFile(preference, 'cpu'); }
      const hardware = await this.platform.detect(this.manifest.target);
      const backend = (cpu || fs.existsSync(preference) || !this.manifest.backends[hardware.backend]) ? 'cpu' : hardware.backend;
      this.update({hardware:hardware.name, backend, detail:backend === 'cpu' && hardware.backend === 'cuda' ? '已选择 CPU 环境' : hardware.detail});
      const id = runtimeId(this.manifest, backend);
      // Keep native DLL paths below Windows' loader limit, including temp paths.
      const runtime = path.join(this.directory, 'runtimes', id.slice(0,24));
      const marker = path.join(runtime, 'ready.json');
      const model = path.join(this.directory, 'downloads', this.manifest.model.sha256);
      let ready = false;
      try { ready = JSON.parse(await fs.promises.readFile(marker, 'utf8')).id === id && fs.existsSync(path.join(runtime,this.platform.host().python)); }
      catch (error) { if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error; }
      const assets = [...this.manifest.common, ...this.manifest.backends[backend]];
      const needed = [...(ready ? [] : assets), this.manifest.model];
      const items = needed.map(a => ({name:a.label, size:a.size, done:0, status:'waiting'}));
      this.update({phase:'downloading', title:ready ? '正在检查组件' : '正在下载组件',
        detail:ready ? '检查本机组件，完成后即可使用。' : '首次下载，支持断点续传。', items, total:needed.reduce((n,a) => n+a.size,0), reused:ready});
      const files = [];
      let completed = 0, last = 0;
      for (const [index, asset] of needed.entries()) {
        signal.throwIfAborted();
        items[index].status = 'downloading';
        const file = await download(asset, path.join(this.directory,'downloads'), {fetch:this.fetch, signal,
          progress:(done,cached) => {
            Object.assign(items[index], {done, status:cached ? 'cached' : 'downloading'});
            if (cached || Date.now()-last > 150) { last = Date.now(); this.update({received:completed+done, items}); }
          }});
        files.push(file);
        completed += asset.size;
        if (items[index].status !== 'cached') items[index].status = 'done';
        this.update({received:completed, items});
      }
      signal.throwIfAborted();
      if (!ready) {
        const required = assets.reduce((n,a) => n+a.unpackedSize,0);
        const disk = await fs.promises.statfs(this.directory);
        if (disk.bavail*disk.bsize < required+128*1024**2) throw Error('磁盘空间不足，请释放空间后重试；已下载的组件会保留');
        stage = path.join(this.directory,'runtimes',`.preparing-${randomUUID().slice(0,12)}`);
        await fs.promises.mkdir(stage, {recursive:true});
        this.update({phase:'installing', title:'正在安装组件', detail:'正在解压，请稍候。'});
        for (const file of files.slice(0, assets.length)) { signal.throwIfAborted(); await this.platform.extract(file, stage, signal); }
        this.update({phase:'verifying', title:'正在验证环境', detail:backend === 'cuda' ? '检查 GPU 运算是否正常。' : '检查运行环境是否正常。'});
        await this.platform.probe(stage, backend, signal);
        signal.throwIfAborted();
        await fs.promises.writeFile(path.join(stage,'ready.json'), JSON.stringify({id, backend}));
        signal.throwIfAborted();
        // An incomplete previous directory is retained for diagnosis, never activated.
        if (fs.existsSync(runtime)) await fs.promises.rename(runtime, runtime+'.incomplete-'+randomUUID().slice(0,8));
        await fs.promises.rename(stage, runtime); stage = null;
      }
      signal.throwIfAborted();
      this.result = {runtime, model, backend};
      this.update({phase:'ready', title:'准备完成', detail:ready ? '已复用本机环境和模型。' : '环境和模型已就绪。'});
      return this.result;
    } catch (error) {
      let cleanupError;
      if (stage) {
        try { await fs.promises.rm(stage, {recursive:true, force:true}); }
        catch (failure) { cleanupError = failure; }
      }
      const paused = signal.aborted && !cleanupError;
      const cleanupDetail = cleanupError ? '\n临时环境清理失败：'+cleanupError.message : '';
      if (!paused) {
        await fs.promises.mkdir(this.directory,{recursive:true})
          .then(() => fs.promises.writeFile(path.join(this.directory,'setup-error.log'), String(error.stack || error)+'\n'+(error.cause?.stderr || '')+cleanupDetail))
          .catch(() => {}); // A full/read-only disk must still show the original error.
      }
      this.update({phase:paused ? 'paused' : 'error', title:paused ? '下载已暂停' : '准备未完成',
        detail:paused ? '进度已保存，可随时继续。' : '已下载的组件会保留，请重试。',
        error:paused ? '' : (String(error.message)+cleanupDetail).slice(0,800)});
      return null;
    }
  }
}
module.exports = {Setup, validateManifest, runtimeId};

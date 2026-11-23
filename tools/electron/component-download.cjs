'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {createHash} = require('node:crypto');
class SourceError extends Error {}

function validUrl(value) {
  const url = new URL(value);
  return url.protocol === 'https:' && !url.username && !url.password;
}

async function hashFile(file, signal) {
  const hash = createHash('sha256');
  for await (const chunk of fs.createReadStream(file, {signal})) hash.update(chunk);
  signal?.throwIfAborted();
  return hash;
}
async function fileSize(file) {
  try { return (await fs.promises.stat(file)).size; }
  catch (error) { if (error.code === 'ENOENT') return 0; throw error; }
}
function validateAsset(asset) {
  if (!validUrl(asset.url) ||
      (asset.fallbackUrls !== undefined && (!Array.isArray(asset.fallbackUrls) || !asset.fallbackUrls.every(validUrl))) ||
      !/^[a-f0-9]{64}$/.test(asset.sha256) || !Number.isSafeInteger(asset.size) || asset.size <= 0 ||
      typeof asset.label !== 'string' || !asset.label.trim()) {
    throw Error('组件清单无效，请重新下载安装器');
  }
}

// Assets are validated at the manifest boundary. App versions share this
// directory; every cached file is verified against its immutable content hash.
async function download(asset, directory, options) {
  const sources = [asset.url, ...(asset.fallbackUrls || [])];
  for (const [index, url] of sources.entries()) {
    try { return await transfer(asset, directory, options, url); }
    catch (error) {
      if (options.signal?.aborted || !(error instanceof SourceError) || index === sources.length - 1) throw error;
      options.onRetry?.(url, sources[index + 1]);
    }
  }
}

async function transfer(asset, directory, {fetch, signal, progress = () => {}, idleMs = 60000}, url) {
  signal?.throwIfAborted();
  await fs.promises.mkdir(directory, {recursive:true});
  const target = path.join(directory, asset.sha256);
  const partial = target + '.partial';
  if (await fileSize(target) === asset.size && (await hashFile(target, signal)).digest('hex') === asset.sha256) {
    progress(asset.size, true);
    return target;
  }
  await fs.promises.rm(target, {force:true});
  let offset = await fileSize(partial);
  if (offset > asset.size) { await fs.promises.rm(partial); offset = 0; }
  if (offset === asset.size) {
    if ((await hashFile(partial, signal)).digest('hex') === asset.sha256) {
      await fs.promises.rename(partial, target);
      progress(offset, true);
      return target;
    }
    await fs.promises.rm(partial);
    offset = 0;
  }
  // Only the saved prefix needs a disk read. New bytes are hashed as they arrive.
  let hash = offset ? await hashFile(partial, signal) : createHash('sha256');
  const abort = new AbortController();
  const combined = AbortSignal.any([signal, abort.signal].filter(Boolean));
  let timer;
  const heartbeat = () => { clearTimeout(timer); timer = setTimeout(() => abort.abort(Error('网络连接超时，请重试继续下载')), idleMs); };
  let out, response, reader;
  const sourceError = error => new SourceError(combined.aborted ? combined.reason.message : String(error.message || error), {cause:error});
  try {
    combined.throwIfAborted();
    heartbeat();
    try {
      response = await fetch(url, {signal:combined, credentials:'omit',
        headers:{'Accept-Encoding':'identity', ...(offset ? {Range:`bytes=${offset}-`} : {})}});
    } catch (error) { throw sourceError(error); }
    if (!response.ok || !response.body) throw new SourceError(`下载失败（HTTP ${response.status}），请重试`);
    if (response.status === 206) {
      const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('content-range') || '');
      if (!match || Number(match[1]) !== offset || Number(match[2]) !== asset.size - 1 || Number(match[3]) !== asset.size) {
        throw new SourceError('服务器返回的续传范围不正确，请重试');
      }
    } else if (response.status === 200) {
      // Servers that ignore Range require a fresh file, never append their full body.
      offset = 0;
      hash = createHash('sha256');
    } else throw new SourceError('服务器返回了不支持的下载响应');
    const length = response.headers.get('content-length');
    if (length !== null && Number(length) !== asset.size - offset) throw new SourceError('组件长度与清单不符');
    out = await fs.promises.open(partial, offset ? 'a' : 'w');
    progress(offset, false);
    reader = response.body.getReader();
    while (true) {
      let part;
      try { part = await reader.read(); }
      catch (error) { throw sourceError(error); }
      if (part.done) break;
      const chunk = part.value;
      combined.throwIfAborted();
      if (offset + chunk.length > asset.size) throw new SourceError('组件长度超过清单限制');
      let written = 0;
      while (written < chunk.length) written += (await out.write(chunk, written, chunk.length - written)).bytesWritten;
      hash.update(chunk);
      offset += chunk.length;
      heartbeat();
      progress(offset, false);
    }
    clearTimeout(timer);
    if (offset !== asset.size) throw new SourceError('连接中断，已保存下载进度，请重试');
    await out.close(); out = null;
    combined.throwIfAborted();
    if (hash.digest('hex') !== asset.sha256) {
      await fs.promises.rm(partial);
      throw new SourceError('组件校验失败，请重试重新下载该组件');
    }
    await fs.promises.rename(partial, target);
    return target;
  } finally {
    clearTimeout(timer);
    abort.abort();
    if (out) await out.close();
    reader?.releaseLock();
    if (response?.body && !response.body.locked) await response.body.cancel().catch(() => {});
  }
}
module.exports = {download, validateAsset};

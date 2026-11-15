'use strict';
const {app, BrowserWindow, dialog, net} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {Readable, Transform} = require('node:stream');
const {pipeline} = require('node:stream/promises');
const {execFileSync} = require('node:child_process');

const payload = path.join(process.resourcesPath, 'analysis');
const base = app.getPath('userData');
const workspace = path.join(base, 'workspace');
const windows = process.platform === 'win32';
const python = path.join(payload, 'python', windows ? 'python.exe' : 'bin/python3.11');
const models = require(path.join(payload, 'code/docs/models.json'));
const cache = path.join(base, 'models');
const tap = path.join(cache, models.tapnext.filename);
const expectedBytes = 2532282370;
const viewer = process.env.WORM_STUDIO_VIEW_URL;
let splash;

function status(text) {
  if (!splash || splash.isDestroyed()) return;
  splash.webContents.executeJavaScript('document.querySelector("p").textContent = '+JSON.stringify(text)).catch(() => {});
}

async function verified(file) {
  if (!fs.existsSync(file) || fs.statSync(file).size !== expectedBytes) return false;
  const hash = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest('hex') === models.tapnext.sha256;
}

async function prepareModel() {
  fs.mkdirSync(cache, {recursive: true});
  const partial = tap + '.partial';
  const marker = tap + '.verified.json';
  const fingerprint = () => {
    const {size, mtimeMs} = fs.statSync(tap);
    return JSON.stringify([models.tapnext.sha256, size, mtimeMs]);
  };
  if (fs.existsSync(tap)) {
    if (fs.existsSync(marker) && fs.readFileSync(marker, 'utf8') === fingerprint()) return;
    status('正在校验已保存的 TAPNext++ 模型…');
    if (await verified(tap)) { fs.writeFileSync(marker, fingerprint()); return; }
  }
  for (;;) {
    try {
      let offset = fs.existsSync(partial) ? fs.statSync(partial).size : 0;
      if (offset > expectedBytes) { fs.unlinkSync(partial); offset = 0; }
      if (offset < expectedBytes) {
        status('正在连接模型下载服务器…');
        const headers = offset ? {Range: 'bytes='+offset+'-'} : {};
        const response = await net.fetch(models.tapnext.url, {headers, credentials:'omit', cache:'no-store'});
        const range = response.headers.get('content-range');
        if (response.status === 200) offset = 0;
        else if (response.status !== 206 || range !== 'bytes '+offset+'-'+(expectedBytes-1)+'/'+expectedBytes)
          throw Error('服务器未返回完整模型（HTTP '+response.status+'）');
        let received = offset, last = 0;
        const progress = new Transform({transform(chunk, encoding, done) {
          received += chunk.length;
          if (received > expectedBytes) return done(Error('下载数据超过模型大小'));
          if (Date.now() - last > 500) {
            status('首次下载 TAPNext++ 模型：'+Math.floor(100*received/expectedBytes)+'%（共 2.53 GB）。完成后可离线使用。');
            last = Date.now();
          }
          done(null, chunk);
        }});
        await pipeline(Readable.fromWeb(response.body), progress,
          fs.createWriteStream(partial, {flags: offset ? 'a' : 'w'}));
      }
      status('正在校验模型完整性…');
      if (!await verified(partial)) {
        if (fs.statSync(partial).size >= expectedBytes) fs.unlinkSync(partial);
        throw Error('模型不完整或 SHA-256 校验失败');
      }
      fs.renameSync(partial, tap);
      fs.writeFileSync(marker, fingerprint());
      return;
    } catch (error) {
      const {response} = await dialog.showMessageBox(splash, {type:'warning', message:'TAPNext++ 模型准备失败',
        detail:error.message, buttons:['重试下载','选择已有的模型文件','退出'], cancelId:2});
      if (response === 2) { app.quit(); return false; }
      if (response === 1) {
        const {filePaths} = await dialog.showOpenDialog(splash, {properties:['openFile'], filters:[{name:'TAPNext++', extensions:['pt']}]});
        if (filePaths.length) {
          status('正在校验所选模型…');
          if (await verified(filePaths[0])) {
            if (path.resolve(filePaths[0]) !== path.resolve(partial)) await fs.promises.copyFile(filePaths[0], partial);
          } else await dialog.showMessageBox(splash, {message:'所选文件与发布版 TAPNext++ 权重不匹配。'});
        }
      }
    }
  }
}

async function launch() {
  Object.assign(process.env, {
    WORM_STUDIO_WORKSPACE:workspace, WORM_STUDIO_PYTHON:python,
    WORM_STUDIO_RESOURCES:payload, WORM_STUDIO_ELECTRON:process.execPath,
    WORMSTUDIO_SINGLE_ROOT:path.join(workspace,'packages','single-worm-methods'),
    WORMSTUDIO_TAPNEXT_CHECKPOINT:tap,
    WORMSTUDIO_SAM2_CHECKPOINT:path.join(payload,'models',models.sam2.filename),
    PYTHONPATH:path.join(payload,'sam2'), PYTHONUTF8:'1', PYTHONNOUSERSITE:'1', PYTHONDONTWRITEBYTECODE:'1',
    MPLBACKEND:'Agg',
  });
  delete process.env.PYTHONHOME;
  if (!viewer) {
    splash = new BrowserWindow({title:'WormStudio', width:540,height:210,autoHideMenuBar:true,
      webPreferences:{sandbox:true, contextIsolation:true, nodeIntegration:false}});
    await splash.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent('<meta charset="utf-8"><style>body{font:16px system-ui;padding:18px;color:#183a32}h2{margin:0 0 14px}</style><h2>WormStudio</h2><p>正在准备线虫分析工作台…</p>'));
    splash.on('closed', () => { if (splash) app.quit(); });
    fs.cpSync(path.join(payload,'code'), workspace, {recursive:true});
    if (await prepareModel() === false) return;
    status('正在启动分析工作台…');
    execFileSync(python, ['-c','from pathlib import Path; import sys; sys.path.insert(0,"src"); from workspace_library import register; register(Path.cwd(), Path("examples/practice.avi"), auto=True)'],
      {cwd:workspace,windowsHide:true});
    app.once('browser-window-created', (_,win) => win.once('ready-to-show', () => {
      const startup = splash; splash = null; startup.close();
    }));
  }
  require(path.join(workspace,'tools/electron/main.cjs'));
}

if (viewer || app.requestSingleInstanceLock()) app.whenReady().then(launch).catch(error => {
  dialog.showErrorBox('WormStudio 启动失败', String(error.stack || error)); app.quit();
});
else app.quit();

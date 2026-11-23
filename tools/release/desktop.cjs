'use strict';
const electron = require('electron');
const {app, BrowserWindow, dialog} = electron;
const fs = require('node:fs');
const path = require('node:path');
const execFileAsync = require('node:util').promisify(require('node:child_process').execFile);

const payload = path.join(process.resourcesPath, 'analysis');
const base = app.getPath('userData');
const workspace = path.join(base, 'workspace');
const windows = process.platform === 'win32';
let runtime = payload;
let python = path.join(payload, 'python', windows ? 'python.exe' : 'bin/python3.11');
const models = require(path.join(payload, 'code/docs/models.json'));
const viewer = process.env.WORM_STUDIO_VIEW_URL;
let splash;
let setupController;
let checkpoint = path.join(payload,'models',models.sam2.filename);

async function prepareOnline() {
  const code = path.join(payload,'code');
  const {showSetup} = require(path.join(code,'tools/electron/setup-window.cjs'));
  const platform = require(path.join(code,'src/platforms/online.cjs'));
  setupController = await showSetup({electron, platform,
    manifest:require(path.join(payload,'online-manifest.json')), directory:path.join(base,'components'),
    onReady:async (prepared, window) => {
      runtime = prepared.runtime;
      python = path.join(runtime, platform.host().python);
      checkpoint = prepared.model;
      process.env.WORMSTUDIO_DEVICE = prepared.backend === 'cuda' ? 'cuda' : 'auto';
      process.env.WORM_STUDIO_ONLINE = '1';
      splash = window;
      await launch(true);
    }});
  app.on('window-all-closed', () => { if (!splash) app.quit(); });
  let stopped = false;
  app.on('before-quit', event => {
    if (!stopped && setupController.setup.running) {
      event.preventDefault();
      stopped = true;
      void setupController.setup.stop().finally(() => app.quit());
    }
  });
}

function status(text) {
  if (!splash || splash.isDestroyed()) return;
  splash.webContents.executeJavaScript('(document.querySelector("#detail") || document.querySelector("p")).textContent = '+JSON.stringify(text)).catch(() => {});
}

async function launch(prepared = false) {
  if (!viewer && !prepared && fs.existsSync(path.join(payload,'online-manifest.json'))) return prepareOnline();
  if (viewer && process.env.WORM_STUDIO_ONLINE === '1') {
    python = process.env.WORM_STUDIO_PYTHON;
    runtime = process.env.WORM_STUDIO_RESOURCES;
    checkpoint = process.env.WORMSTUDIO_SAM2_CHECKPOINT;
  }
  Object.assign(process.env, {
    WORM_STUDIO_WORKSPACE:workspace, WORM_STUDIO_PYTHON:python,
    WORM_STUDIO_RESOURCES:runtime, WORM_STUDIO_ELECTRON:process.execPath,
    WORMSTUDIO_SAM2_CHECKPOINT:checkpoint,
    PYTHONPATH:path.join(runtime,'sam2'), PYTHONUTF8:'1', PYTHONNOUSERSITE:'1', PYTHONDONTWRITEBYTECODE:'1',
    MPLBACKEND:'Agg',
  });
  delete process.env.PYTHONHOME;
  if (!viewer) {
    if (!splash) {
      splash = new BrowserWindow({title:'WormStudio', width:540,height:210,autoHideMenuBar:true,
      icon:path.join(payload,'code',windows?'tools/electron/WormStudio.ico':'web/wormstudio-icon.png'),
      webPreferences:{sandbox:true, contextIsolation:true, nodeIntegration:false}});
    await splash.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent('<meta charset="utf-8"><style>body{font:16px system-ui;padding:18px;color:#183a32}h2{margin:0 0 14px}</style><h2>WormStudio</h2><p>正在准备线虫分析工作台…</p>'));
      splash.on('closed', () => { if (splash) app.quit(); });
    }
    await fs.promises.cp(path.join(payload,'code'), workspace, {recursive:true});
    status('正在启动分析工作台…');
    await execFileAsync(python, ['-c','from pathlib import Path; import sys; sys.path.insert(0,"src"); from workspace_library import register; register(Path.cwd(), Path("examples/practice.avi"), auto=True)'],
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

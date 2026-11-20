'use strict';
const {app, BrowserWindow, dialog} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');

const payload = path.join(process.resourcesPath, 'analysis');
const base = app.getPath('userData');
const workspace = path.join(base, 'workspace');
const windows = process.platform === 'win32';
const python = path.join(payload, 'python', windows ? 'python.exe' : 'bin/python3.11');
const models = require(path.join(payload, 'code/docs/models.json'));
const viewer = process.env.WORM_STUDIO_VIEW_URL;
let splash;

function status(text) {
  if (!splash || splash.isDestroyed()) return;
  splash.webContents.executeJavaScript('document.querySelector("p").textContent = '+JSON.stringify(text)).catch(() => {});
}

async function launch() {
  Object.assign(process.env, {
    WORM_STUDIO_WORKSPACE:workspace, WORM_STUDIO_PYTHON:python,
    WORM_STUDIO_RESOURCES:payload, WORM_STUDIO_ELECTRON:process.execPath,
    WORMSTUDIO_SAM2_CHECKPOINT:path.join(payload,'models',models.sam2.filename),
    PYTHONPATH:path.join(payload,'sam2'), PYTHONUTF8:'1', PYTHONNOUSERSITE:'1', PYTHONDONTWRITEBYTECODE:'1',
    MPLBACKEND:'Agg',
  });
  delete process.env.PYTHONHOME;
  if (!viewer) {
    splash = new BrowserWindow({title:'WormStudio', width:540,height:210,autoHideMenuBar:true,
      icon:path.join(payload,'code',windows?'tools/electron/WormStudio.ico':'web/wormstudio-icon.png'),
      webPreferences:{sandbox:true, contextIsolation:true, nodeIntegration:false}});
    await splash.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent('<meta charset="utf-8"><style>body{font:16px system-ui;padding:18px;color:#183a32}h2{margin:0 0 14px}</style><h2>WormStudio</h2><p>正在准备线虫分析工作台…</p>'));
    splash.on('closed', () => { if (splash) app.quit(); });
    fs.cpSync(path.join(payload,'code'), workspace, {recursive:true});
    status('正在启动分析工作台…');
    execFileSync(python, ['-c','from pathlib import Path; import sys; sys.path.insert(0,"src"); from app.workspace_library import register; register(Path.cwd(), Path("examples/practice.avi"), auto=True)'],
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

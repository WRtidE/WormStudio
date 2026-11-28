'use strict';
const {app, BrowserWindow, Menu, dialog, shell, ipcMain, net} = require('electron');
const {pathToFileURL} = require('node:url');
const {Updates} = require('./updates.cjs');
const appUpdate = require('../../src/platforms/app_update.cjs');
const gpuPlatform = require('../../src/platforms/online.cjs');
const {DesktopShortcut} = require('../../src/platforms/desktop_shortcut.cjs');
const {execFile, spawn} = require('node:child_process');
const execFileAsync = require('node:util').promisify(execFile);
const {createWriteStream, mkdirSync} = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const {existsSync, readFileSync} = require('node:fs');
const localPath = path.join(__dirname, 'local.json');
const local = existsSync(localPath) ? JSON.parse(readFileSync(localPath, 'utf8')) : {};
const workspace = process.env.WORM_STUDIO_WORKSPACE || local.workspace || path.resolve(__dirname, '../..');
const configuredPython = process.env.WORM_STUDIO_PYTHON || local.python;
const onlineGpu = process.env.WORM_STUDIO_ONLINE === '1';
const sourceGpu = process.platform === 'win32' && process.arch === 'x64' && configuredPython && !app.isPackaged && !onlineGpu
  ? gpuPlatform.workspaceGpu(workspace, configuredPython) : null;
const python = sourceGpu?.readyPython() || configuredPython;
const viewer = process.env.WORM_STUDIO_VIEW_URL;
if (viewer && !/^http:\/\/127\.0\.0\.1:\d+\/(?:progress)?$/.test(viewer)) throw Error('无效的本地页面地址');

const title = 'WormStudio · 线虫分析';
const windowIcon = process.platform === 'win32' ? path.join(__dirname, 'WormStudio.ico') : path.join(workspace, 'web/wormstudio-icon.png');
if (process.platform === 'win32') {
  app.setAppUserModelId('org.wormstudio.desktop');
  app.on('browser-window-created', (_event, window) => {
    window.setIcon(windowIcon);
    window.setAppDetails({appId:'org.wormstudio.desktop', appIconPath:windowIcon, appIconIndex:0});
  });
}
const logPath = path.join(workspace, 'logs', 'electron.log');
let win, backend, origin, closing = false, closePending = false;

let settingsWindow, updates, pendingUpdate;
const settingsPage = path.join(__dirname, 'settings.html');

function openSettings() {
  if (settingsWindow) {
    if (settingsWindow.isMinimized()) settingsWindow.restore();
    settingsWindow.focus();
    settingsWindow.webContents.send('settings:open-updates');
    return;
  }
  settingsWindow = new BrowserWindow({
    title:'WormStudio 设置', icon:windowIcon, width:760, height:500, minWidth:590, minHeight:360,
    parent:win, show:false, backgroundColor:'#ffffff', autoHideMenuBar:true,
    webPreferences:{preload:path.join(__dirname,'settings-preload.cjs'), sandbox:true, contextIsolation:true, nodeIntegration:false}
  });
  settingsWindow.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  settingsWindow.webContents.on('will-navigate', event => event.preventDefault());
  settingsWindow.once('ready-to-show', () => settingsWindow?.show());
  settingsWindow.once('closed', () => { settingsWindow = null; });
  settingsWindow.loadFile(settingsPage);
}
function initUpdates() {
  const notify = () => {
    const state = updates.state();
    if (win && !win.isDestroyed()) win.webContents.send('settings:badge', Boolean(state.release));
    if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.webContents.send('updates:changed', state);
  };
  updates = new Updates({directory:app.getPath('userData'), downloads:app.getPath('downloads'),
    fetch:(url, options) => net.fetch(url, {...options, credentials:'omit'}), notify,
    feed:process.env.WORM_STUDIO_UPDATE_FEED || require('./package.json').updateFeed,
    installer:!viewer && appUpdate.supported(process.execPath, process.resourcesPath) ? closeWindow : null});
  function handle(channel, permitted, callback) {
    ipcMain.handle(channel, (event, ...args) => {
      if (!event.senderFrame || event.senderFrame !== event.sender.mainFrame || !permitted(event)) throw Error('无效的设置请求');
      return callback(...args);
    });
  }
  const fromStudio = event => event.sender === win?.webContents && event.senderFrame.url === origin+'/studio';
  const fromSettings = event => event.sender === settingsWindow?.webContents && event.senderFrame.url === pathToFileURL(settingsPage).href;
  handle('settings:open', fromStudio, openSettings);
  handle('settings:badge', fromStudio, () => Boolean(updates.state().release));
  handle('updates:state', fromSettings, () => updates.state());
  handle('updates:check', fromSettings, () => updates.check());
  handle('updates:auto', fromSettings, value => updates.setAuto(value));
  handle('updates:download', fromSettings, () => updates.download());
  handle('updates:install', fromSettings, () => updates.install());
  handle('updates:reveal', fromSettings, () => { if (updates.downloaded) shell.showItemInFolder(updates.downloaded); });
  handle('updates:workspace', fromSettings, () => shell.openPath(workspace));
  const gpuHelper = path.join(workspace, 'tools', 'release', 'windows_gpu.py');
  const gpuSupported = () => process.platform === 'win32' && Boolean(python);
  const bundledPython = path.join(process.resourcesPath, 'analysis', 'python', 'python.exe');
  const canInstallGpu = () => gpuSupported() && path.resolve(python).toLowerCase() === bundledPython.toLowerCase();
  const gpuAbort = new AbortController();
  let gpuCheck, gpuInstallation, sourceInstall, installStatus, offered, gpuPrepared = false, stoppingGpu = false;
  const restart = {state:'restart', message:'GPU 加速组件已就绪。完成当前任务后重启 WormStudio，即可使用显卡加速。'};
  function publishGpuStatus(state, message, transfer) {
    installStatus = {state,message,transfer};
    if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.webContents.send('settings:gpu-status', installStatus);
  }
  app.on('before-quit', event => {
    if ((!gpuInstallation && !sourceInstall) || stoppingGpu) return;
    event.preventDefault();
    stoppingGpu = true;
    gpuAbort.abort();
    const stopped = sourceInstall || gpuInstallation.then(controller => controller.setup.stop());
    void stopped.catch(() => {}).finally(() => app.quit());
  });
  app.once('will-quit', () => gpuAbort.abort());
  handle('settings:gpu-available', fromSettings, gpuSupported);
  handle('settings:gpu-check', fromSettings, () => {
    if (!gpuSupported()) throw Error('当前平台无法执行 NVIDIA 检测');
    if (gpuPrepared) return restart;
    if ((sourceInstall || gpuInstallation) && installStatus) return installStatus;
    if (!gpuCheck) {
      offered = null;
      gpuCheck = execFileAsync(python, ['-B', '-X', 'utf8', gpuHelper, '--status-json'], {
        cwd:workspace, windowsHide:true, timeout:90000, signal:gpuAbort.signal,
      }).then(async ({stdout}) => {
        const [state, message] = JSON.parse(stdout.trim());
        if (typeof message !== 'string') throw Error('无法读取 GPU 检测结果');
        const mode = onlineGpu ? 'online' : sourceGpu ? 'workspace' : null;
        const result = await gpuPlatform.gpuOffer(state, message, mode);
        if (!mode && !canInstallGpu() && ['available','prepared'].includes(result.state)) {
          return {state:'cpu',message:'当前运行环境使用 CPU。Windows 在线版支持在设置中安装 GPU 组件。'};
        }
        offered = result.state;
        return result;
      }).finally(() => { gpuCheck = null; });
    }
    return gpuCheck;
  });
  handle('settings:gpu-open', fromSettings, () => {
    if (sourceInstall) return sourceInstall;
    if (gpuPrepared) return restart;
    if (offered === 'workspace') {
      publishGpuStatus('installing','正在准备 GPU 加速组件，完成后将验证显卡。可继续使用工作台。');
      const progress = (message,transfer) => publishGpuStatus('installing',message,transfer);
      const {withWorkspaceWheels} = require('./component-download.cjs');
      const options = {directory:path.join(app.getPath('userData'),'workspace-gpu-downloads'),
        shared:path.join(app.getPath('appData'),'WormStudio','components','downloads'),
        fetch:(url,options) => net.fetch(url,{...options,credentials:'omit'}),signal:gpuAbort.signal,progress};
      sourceInstall = sourceGpu.install(gpuAbort.signal, progress,
        (assets,install) => withWorkspaceWheels(assets,options,install)).then(() => {
        gpuPrepared = true;
        publishGpuStatus(restart.state, restart.message);
        return restart;
      }).catch(error => {
        publishGpuStatus('workspace',error.message);
        return installStatus;
      }).finally(() => { sourceInstall = null; });
      return sourceInstall;
    }
    if (offered === 'online') {
      if (!gpuInstallation) {
        const {showSetup} = require('./setup-window.cjs');
        gpuInstallation = showSetup({electron:require('electron'), gpu:true,
          platform:gpuPlatform,
          manifest:require(path.join(process.resourcesPath, 'analysis/online-manifest.json')),
          directory:path.join(app.getPath('userData'), 'components'),
          onProgress:state => {
            const transfer = state.total && ['downloading','paused','error'].includes(state.phase)
              ? {received:state.received,total:state.total} : undefined;
            publishGpuStatus('online-active',state.error || state.title,transfer);
          },
          onReady:async (result, window) => {
            gpuPrepared = result.backend === 'cuda';
            if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.webContents.send('settings:gpu-ready', result.backend);
            window.close();
          }}).then(controller => {
            controller.window.once('closed', () => {
              if (!gpuPrepared) publishGpuStatus('online','可继续安装 GPU 加速组件，已下载的进度会保留。');
              void controller.setup.stop().finally(() => { gpuInstallation = null; });
            });
            return controller;
          }).catch(error => { gpuInstallation = null; throw error; });
      }
      return gpuInstallation.then(({window}) => { if (!window.isDestroyed()) window.focus(); return {online:true}; });
    }
    if (!canInstallGpu() || !['available','prepared'].includes(offered)) throw Error('请重新检测当前环境的 GPU 加速组件。');
    return new Promise((resolve, reject) => {
      const child = spawn(python, ['-I', '-B', gpuHelper], {cwd:workspace, detached:true, windowsHide:true, stdio:'ignore'});
      child.once('error', reject);
      child.once('spawn', () => { child.unref(); resolve(); });
    });
  });
  if (!viewer) void updates.startup();
}

function signalBackend(signal) {
  if (!backend?.pid) return false;
  try {
    process.kill(-backend.pid, signal);
    return true;
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
    return false;
  }
}

async function closeWindow(release = null) {
  if (closePending || closing) return;
  closePending = true;
  try {
    const owner = release && settingsWindow ? settingsWindow : win;
    const choice = await dialog.showMessageBox(owner, {
      type: 'question', message: release ? '重启并更新 WormStudio？' : '退出 WormStudio？', detail: '退出会停止当前正在运行的分析。',
      buttons: ['继续使用', release ? '重启并更新' : '退出'], defaultId: 0, cancelId: 0
    });
    if (choice.response !== 1) return;
    if (origin && !win.webContents.isCrashed()) {
      try {
        await win.webContents.executeJavaScript('window.StudioWorkspace?.prepareToLeave() ?? window.AnnotationDraft?.flush()');
      } catch (error) {
        const result = await dialog.showMessageBox(release && settingsWindow ? settingsWindow : win, {
          type: 'warning', message: '当前修改可能尚未保存', detail: error.message,
          buttons: ['返回工作台', '仍然退出'], defaultId: 0, cancelId: 0
        });
        if (result.response !== 1) return;
      }
    }
    // Let Chromium run the page's remaining beforeunload checks.
    if (release) {
      const update = await appUpdate.prepare({...release, executable:process.execPath, directory:app.getPath('userData')});
      // Keep Electron alive until Chromium accepts the close and the helper has started.
      const closed = new Promise(resolve => { pendingUpdate = resolve; });
      closing = true;
      try {
        win.close();
        if (await closed) await update.start();
        else await update.discard();
      } catch (error) {
        closing = !win;
        await update.discard();
        if (win) throw error;
        dialog.showErrorBox('无法启动更新', error.message);
      } finally {
        pendingUpdate = null;
        if (!win) app.quit();
      }
      return;
    }
    closing = true;
    win.close();
  } finally {
    closePending = false;
  }
}

function launch() {
  if (process.platform === 'darwin') app.dock.setIcon(path.join(workspace, 'web/wormstudio-icon-macos.png'));
  mkdirSync(path.dirname(logPath), {recursive: true});
  mkdirSync(path.join(workspace, 'outputs'), {recursive: true});
  const log = createWriteStream(logPath, {flags: 'a'});
  log.write('\n'+new Date().toISOString()+' Electron '+process.versions.electron+'\n');
  win = new BrowserWindow({
    title, icon:windowIcon, width: 1280, height: 840, minWidth: 800, minHeight: 560, show: false,
    backgroundColor: '#f6f8f7',
    webPreferences: {preload:path.join(__dirname,'workspace-preload.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false}
  });
  win.on('page-title-updated', event => event.preventDefault());
  if (process.platform === 'win32') {
    win.once('show', () => {
      const handle = win.getNativeWindowHandle().toString('hex');
      log.write(`WormStudio window shown: pid=${process.pid} hwnd_le=${handle}\n`);
    });
  }
  win.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
  win.webContents.on('will-navigate', (event, url) => {
    if (new URL(url).origin !== origin) event.preventDefault();
  });
  win.on('close', event => {
    if (!closing) { event.preventDefault(); void closeWindow(); }
  });
  win.webContents.on('will-prevent-unload', event => {
    const answer = dialog.showMessageBoxSync(pendingUpdate && settingsWindow ? settingsWindow : win, {
      type: 'warning', message: '仍有未保存的修改，确定离开？',
      buttons: ['留下', '离开'], defaultId: 0, cancelId: 0
    });
    if (answer === 1) event.preventDefault();
    else { closing = false; pendingUpdate?.(false); }
  });
  win.on('closed', () => {
    win = null;
    pendingUpdate?.(true);
  });
  initUpdates();
  const supportsDesktopShortcut = process.platform === 'win32' && !viewer;
  async function desktopShortcut(firstUse) {
    try {
      const shortcut = new DesktopShortcut({shell, desktop:app.getPath('desktop'), profile:app.getPath('userData'),
        packaged:app.isPackaged, executable:process.execPath, workspace, icon:windowIcon});
      if (firstUse) {
        await shortcut.offer(async () => {
          const {response} = await dialog.showMessageBox(win, {
            type:'question', title:'WormStudio', message:'在桌面创建 WormStudio 快捷方式？',
            detail:'以后可以直接从桌面打开工作台。也可以稍后在 WormStudio 菜单中创建。',
            buttons:['创建快捷方式', '跳过'], defaultId:0, cancelId:1
          });
          return response === 0;
        });
      } else {
        shell.showItemInFolder(shortcut.create());
      }
    } catch (error) {
      if (win && !closing) await dialog.showMessageBox(win, {
        type:'warning', title:'WormStudio', message:'桌面快捷方式设置未完成', detail:error.message
      });
    }
  }
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label: 'WormStudio', submenu: [
      {label:'设置…', accelerator:'CmdOrCtrl+,', click:openSettings},
      ...(supportsDesktopShortcut ? [{label:'创建桌面快捷方式', click:() => desktopShortcut(false)}] : []),
      {type:'separator'}, {role: 'hide'}, {role: 'quit', label: '退出 WormStudio'}]},
    {label: '编辑', submenu: [{role: 'undo'}, {role: 'redo'}, {type: 'separator'},
      {role: 'cut'}, {role: 'copy'}, {role: 'paste'}, {role: 'selectAll'}]},
    {label: '工作区', submenu: [
      {label: '打开结果目录', click: () => shell.openPath(path.join(workspace, 'outputs'))},
      {label: '打开工作目录', click: () => shell.openPath(workspace)}
    ]},
    {role: 'windowMenu'}
  ]));
  if (viewer) {
    origin = new URL(viewer).origin;
    process.stdin.resume();
    process.stdin.on('end', () => { closing = true; win?.destroy(); });
    win.loadURL(viewer).then(() => win?.show(), fail);
    return;
  }
  backend = spawn(python, ['-u', '-c', 'import studio; studio.main(open_browser=False)'], {
    cwd: workspace, detached: process.platform !== 'win32', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: {...process.env, WORM_STUDIO_PYTHON: python, PYTHONUTF8: '1', MPLBACKEND: 'Agg',
      ...(process.platform === 'darwin' ? {
        PYTORCH_ENABLE_MPS_FALLBACK: process.env.PYTORCH_ENABLE_MPS_FALLBACK || '1',
        PYTORCH_MPS_HIGH_WATERMARK_RATIO: process.env.PYTORCH_MPS_HIGH_WATERMARK_RATIO || '0.8',
        PYTORCH_MPS_LOW_WATERMARK_RATIO: process.env.PYTORCH_MPS_LOW_WATERMARK_RATIO || '0.65'
      } : {})}
  });
  function fail(error) {
    if (closing) return;
    closing = true;
    log.write('Desktop startup or backend error: '+error.message+'\n');
    dialog.showErrorBox('工作台未能继续运行', error.message+'\n日志：'+logPath);
    app.quit();
  }
  backend.once('error', fail);
  backend.once('exit', (code, signal) => {
    if (closing) return;
    if (code === 0 && origin) { closing = true; app.quit(); }
    else fail(new Error('Python 后端已停止：'+(signal ?? code)));
  });
  backend.stderr.pipe(log, {end: false});
  readline.createInterface({input: backend.stdout}).on('line', line => {
    log.write(line+'\n');
    const match = /^Worm Studio: (http:\/\/127\.0\.0\.1:\d+\/studio)$/.exec(line);
    if (!match || origin || closing) return;
    origin = new URL(match[1]).origin;
    win.loadURL(match[1]).then(() => {
      if (!win || closing) return;
      win.show();
      if (supportsDesktopShortcut) void desktopShortcut(true);
    }, fail);
  });
}

if (viewer || app.requestSingleInstanceLock()) {
  app.on('second-instance', () => { if (win) { win.restore(); win.focus(); } });
  app.on('window-all-closed', () => { closing = true; if (!pendingUpdate) app.quit(); });
  // A pipeline stage and its encoders belong to the same process group.
  let stopped = false;
  app.on('will-quit', event => {
    if (stopped || !backend?.pid) return;
    if (process.platform === 'win32') {
      if (backend.exitCode !== null || backend.signalCode !== null) return;
      event.preventDefault();
      stopped = true;
      const killer = spawn('taskkill.exe', ['/pid', String(backend.pid), '/t', '/f'], {windowsHide: true});
      killer.once('close', () => app.quit());
      killer.once('error', () => { backend.kill(); app.quit(); });
      return;
    }
    if (!signalBackend('SIGTERM')) return;
    event.preventDefault();
    const deadline = Date.now()+3000;
    const timer = setInterval(() => {
      if (signalBackend(0) && Date.now()<deadline) return;
      signalBackend('SIGKILL');
      clearInterval(timer); stopped = true; app.quit();
    }, 80);
  });
  app.whenReady().then(launch).catch(error => {
    dialog.showErrorBox('工作台启动失败', error.message);
    closing = true; app.quit();
  });
} else app.quit();

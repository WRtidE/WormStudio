'use strict';
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {Setup} = require('./online-setup.cjs');

async function showSetup({electron, manifest, directory, platform, onReady, fetch, autoOpen = true, gpu = false}) {
  const {BrowserWindow, ipcMain, net} = electron;
  const page = path.join(__dirname,'setup.html');
  const window = new BrowserWindow({title:gpu ? 'WormStudio · 安装 GPU 加速' : 'WormStudio · 首次准备', width:600,height:480,minWidth:520,minHeight:480,useContentSize:true,
    icon:path.join(__dirname,'../../web/wormstudio-icon.png'),
    show:false,autoHideMenuBar:true,backgroundColor:'#fbfcfa',
    webPreferences:{preload:path.join(__dirname,'setup-preload.cjs'),sandbox:true,contextIsolation:true,nodeIntegration:false}});
  let shown = false;
  function reveal() {
    if (shown || window.isDestroyed()) return;
    shown = true;
    window.show();
  }
  let opening = false, readyTimer;
  const open = async () => {
    if (opening || setup.state.phase !== 'ready' || window.isDestroyed()) return;
    clearTimeout(readyTimer);
    opening = true;
    try { await onReady(setup.result, window); }
    catch (error) { opening = false; setup.update({phase:'error',title:'工作台启动失败',error:error.message}); }
  };
  const fetchAsset = fetch || ((url, options) => net.fetch(url, options));
  const setup = new Setup({manifest, directory, platform, gpu,
    fetch:(url, options) => {
      // A ready runtime may still need a replacement for a missing or damaged model.
      reveal();
      return fetchAsset(url, options);
    },
    notify:state => {
      clearTimeout(readyTimer);
      if (window.isDestroyed()) return;
      if ((state.phase === 'downloading' && !state.reused) || state.phase === 'error') reveal();
      window.webContents.send('setup:changed',state);
      if (state.phase === 'ready' && autoOpen) readyTimer = setTimeout(() => void open(), state.reused ? 0 : 1500);
    }});
  if (gpu) setup.state.readyAction = '完成';
  const handle = (channel, callback) => ipcMain.handle(channel, (event,...args) => {
    if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url !== pathToFileURL(page).href) throw Error('无效的准备请求');
    return callback(...args);
  });
  handle('setup:state', () => setup.state);
  handle('setup:action', action => {
    if (action === 'pause') setup.pause();
    else if (action === 'open' && setup.state.phase === 'ready') void open();
    else if (['retry','cpu'].includes(action) && ['paused','error'].includes(setup.state.phase)) void setup.start(action === 'cpu');
    else throw Error('无效的准备操作');
  });
  window.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.once('closed', () => { clearTimeout(readyTimer); void setup.stop(); ipcMain.removeHandler('setup:state'); ipcMain.removeHandler('setup:action'); });
  await window.loadFile(page);
  if (gpu || !autoOpen) reveal();
  void setup.start();
  return {window, setup};
}
module.exports = {showSetup};

'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('setup', {
  state:() => ipcRenderer.invoke('setup:state'),
  action:value => ipcRenderer.invoke('setup:action', value),
  changed:callback => ipcRenderer.on('setup:changed', (_event,state) => callback(state)),
});

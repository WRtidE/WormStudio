'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('settings', {
  onOpenUpdates: callback => ipcRenderer.on('settings:open-updates', () => callback()),
  state: () => ipcRenderer.invoke('updates:state'),
  check: () => ipcRenderer.invoke('updates:check'),
  auto: enabled => ipcRenderer.invoke('updates:auto', enabled),
  download: () => ipcRenderer.invoke('updates:download'),
  install: () => ipcRenderer.invoke('updates:install'),
  reveal: () => ipcRenderer.invoke('updates:reveal'),
  workspace: () => ipcRenderer.invoke('updates:workspace'),
  gpuAvailable: () => ipcRenderer.invoke('settings:gpu-available'),
  checkGpu: () => ipcRenderer.invoke('settings:gpu-check'),
  openGpu: () => ipcRenderer.invoke('settings:gpu-open'),
  onGpuReady: callback => ipcRenderer.on('settings:gpu-ready', (_event, backend) => callback(backend)),
  onChange: callback => ipcRenderer.on('updates:changed', (_event, state) => callback(state))
});

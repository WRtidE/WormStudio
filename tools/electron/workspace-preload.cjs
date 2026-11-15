'use strict';
const {ipcRenderer} = require('electron');
window.addEventListener('DOMContentLoaded', () => {
  const entry = document.getElementById('open-settings');
  if (!entry || !process.isMainFrame || location.pathname !== '/studio') return;
  entry.hidden = false;
  entry.onclick = () => ipcRenderer.invoke('settings:open');
  function badge(available) {
    entry.classList.toggle('has-update', available);
    entry.setAttribute('aria-label', available ? '设置，有新版本' : '设置');
  }
  ipcRenderer.on('settings:badge', (_event, available) => badge(available));
  ipcRenderer.invoke('settings:badge').then(badge);
});

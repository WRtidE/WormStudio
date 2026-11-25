'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

class DesktopShortcut {
  constructor({shell, desktop, profile, packaged, executable, workspace, icon}) {
    this.shell = shell;
    this.desktop = desktop;
    this.marker = path.join(profile, packaged ? 'desktop-shortcut.seen' : 'desktop-shortcut-source.seen');
    const target = packaged ? executable : path.join(workspace, 'start.bat');
    this.options = {target, cwd:path.dirname(target), icon:packaged ? executable : icon,
      iconIndex:0, description:'打开 WormStudio 工作台', appUserModelId:'org.wormstudio.desktop'};
  }

  location() {
    for (let number = 1; ; number++) {
      const name = number === 1 ? 'WormStudio' : `WormStudio (${number})`;
      const file = path.join(this.desktop, name+'.lnk');
      if (!fs.existsSync(file)) return {file, existing:false};
      let link;
      try {
        link = this.shell.readShortcutLink(file);
      } catch {
        // An unreadable shortcut or a different file with this name belongs to its owner.
        continue;
      }
      if (!link.args && path.normalize(link.target).toLowerCase() === this.options.target.toLowerCase()) {
        return {file, existing:true};
      }
    }
  }

  create() {
    const {file, existing} = this.location();
    if (existing) return file;
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'wormstudio-shortcut-'));
    const link = path.join(temp, 'WormStudio.lnk');
    try {
      if (!this.shell.writeShortcutLink(link, 'create', this.options)) throw Error('Windows 未能生成快捷方式。');
      fs.copyFileSync(link, file, fs.constants.COPYFILE_EXCL);
    } finally {
      fs.rmSync(temp, {recursive:true, force:true});
    }
    return file;
  }

  async offer(ask) {
    if (fs.existsSync(this.marker)) return;
    if (!this.location().existing && await ask()) this.create();
    fs.writeFileSync(this.marker, '');
  }
}

module.exports = {DesktopShortcut};

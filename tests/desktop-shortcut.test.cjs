'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {DesktopShortcut} = require('../src/platforms/desktop_shortcut.cjs');

function fixture(t, packaged = true) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'shortcut-test-'));
  t.after(() => fs.rmSync(profile, {recursive:true, force:true}));
  const desktop = path.join(profile, '重定向桌面');
  fs.mkdirSync(desktop);
  const shell = {
    writeShortcutLink(file, operation, options) {
      assert.equal(operation, 'create');
      fs.writeFileSync(file, JSON.stringify(options));
      return true;
    },
    readShortcutLink:file => JSON.parse(fs.readFileSync(file, 'utf8'))
  };
  const executable = path.join(profile, '程序', 'WormStudio.exe');
  const workspace = path.join(profile, '源码 工作区');
  const icon = path.join(workspace, 'tools', 'electron', 'WormStudio.ico');
  const options = {shell, desktop, profile, packaged, executable, workspace, icon};
  return {shortcut:new DesktopShortcut(options), options, file:path.join(desktop, 'WormStudio.lnk')};
}

test('skipping is remembered across launches and a shortcut can be created later', async t => {
  const {shortcut, options, file} = fixture(t);
  await shortcut.offer(() => false);
  assert.equal(fs.existsSync(file), false);
  const reopened = new DesktopShortcut(options);
  await reopened.offer(() => assert.fail('prompt repeated'));
  assert.equal(reopened.create(), file);
  const link = options.shell.readShortcutLink(file);
  assert.equal(link.target, options.executable);
  assert.equal(link.cwd, path.dirname(options.executable));
  assert.equal(link.icon, options.executable);
  assert.equal(reopened.create(), file);
  assert.deepEqual(fs.readdirSync(options.desktop), ['WormStudio.lnk']);
});

test('source launch preserves a conflicting file and links to the workspace launcher', async t => {
  const {shortcut, options, file} = fixture(t, false);
  fs.writeFileSync(file, 'existing user file');
  await shortcut.offer(() => true);
  assert.equal(fs.readFileSync(file, 'utf8'), 'existing user file');
  const alternate = path.join(options.desktop, 'WormStudio (2).lnk');
  const link = options.shell.readShortcutLink(alternate);
  assert.equal(link.target, path.join(options.workspace, 'start.bat'));
  assert.equal(link.cwd, options.workspace);
  assert.equal(link.icon, options.icon);
  assert.equal(shortcut.create(), alternate);
  fs.unlinkSync(alternate);
  await new DesktopShortcut(options).offer(() => assert.fail('prompt repeated after deletion'));
  assert.equal(fs.existsSync(alternate), false);
});

test('a matching shortcut skips the prompt and a failed creation can be retried', async t => {
  const {shortcut, options, file} = fixture(t);
  const writer = t.mock.method(options.shell, 'writeShortcutLink', () => false);
  await assert.rejects(shortcut.offer(() => true), /Windows/);
  assert.equal(fs.existsSync(file), false);
  writer.mock.restore();
  shortcut.create();
  await shortcut.offer(() => assert.fail('already on the desktop'));
});

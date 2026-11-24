'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID, createHash} = require('node:crypto');
const {execFile} = require('node:child_process');
const execute = require('node:util').promisify(execFile);

function supported(executable, resources, platform = process.platform) {
  return platform === 'win32' && path.basename(executable).toLowerCase() === 'wormstudio.exe' &&
    path.resolve(resources) === path.join(path.dirname(executable), 'resources');
}

async function prepare({archive, digest, version, executable, directory}) {
  if (!/^sha256:[a-f0-9]{64}$/.test(digest || '')) throw Error('安装包缺少可信校验值，请手动安装');
  const hash = createHash('sha256');
  for await (const chunk of fs.createReadStream(archive)) hash.update(chunk);
  if ('sha256:'+hash.digest('hex') !== digest) throw Error('安装包校验失败，请重新下载');
  const target = path.dirname(path.resolve(executable));
  const id = randomUUID();
  const cache = path.join(path.dirname(target), '.'+path.basename(target)+'.updates');
  const stage = path.join(cache, 'stage-'+id);
  const backup = path.join(cache, 'previous-'+id);
  const job = path.join(directory, 'updates', id);
  await fs.promises.mkdir(job, {recursive:true});
  const script = path.join(job, 'apply.ps1');
  await fs.promises.copyFile(path.join(__dirname, 'app_update.ps1'), script);
  const plan = path.join(job, 'plan.json');
  await fs.promises.writeFile(plan, JSON.stringify({archive, version, target, stage, backup, parentPid:process.pid,
    log:path.join(job, 'result.log')}));
  const powershell = path.join(process.env.SystemRoot, 'System32/WindowsPowerShell/v1.0/powershell.exe');
  const args = ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script, '-Plan', plan];
  await execute(powershell, [...args, '-Prepare'], {windowsHide:true, timeout:600000, maxBuffer:1024*1024});
  const discard = () => fs.promises.rm(stage, {recursive:true, force:true});
  const start = () => {
    const env = {...process.env};
    for (const key of Object.keys(env)) if (key.startsWith('WORM_STUDIO_') || key.startsWith('WORMSTUDIO_') ||
      ['ELECTRON_RUN_AS_NODE','PYTHONPATH','PYTHONHOME'].includes(key)) delete env[key];
    // A detached Windows PowerShell may skip -File. Let a short-lived
    // PowerShell launch the independent helper and acknowledge its creation.
    return execute(powershell, [...args, '-Launch'], {cwd:job, env, windowsHide:true});
  };
  return {start, discard};
}
module.exports = {supported, prepare};

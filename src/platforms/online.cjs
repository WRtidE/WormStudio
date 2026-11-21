'use strict';
const path = require('node:path');
const {execFile} = require('node:child_process');
const execute = require('node:util').promisify(execFile);

function host(platform = process.platform, arch = process.arch) {
  const target = `${platform}-${arch}`;
  if (!['win32-x64', 'darwin-arm64'].includes(target)) throw Error('在线版目前支持 Windows x64 和 Apple Silicon Mac');
  return {target, python: platform === 'win32' ? 'python/python.exe' : 'python/bin/python3.11',
    tar: platform === 'win32' ? path.join(process.env.SystemRoot, 'System32', 'tar.exe') : '/usr/bin/tar'};
}
async function detect(target, run = execute) {
  if (target === 'darwin-arm64') return {backend:'cpu', name:'Apple Silicon', detail:'使用适配 Apple Silicon 的运行环境，启动后检测 Metal 加速'};
  try {
    const executable = path.join(process.env.SystemRoot || 'C:/Windows', 'System32', 'nvidia-smi.exe');
    const {stdout} = await run(executable, ['--query-gpu=name,driver_version,compute_cap', '--format=csv,noheader,nounits'], {windowsHide:true, timeout:15000});
    const cards = stdout.trim().split(/\r?\n/).map(line => line.split(',').map(v => v.trim()));
    // Conservative full CUDA 12.8 driver support; older drivers can still use CPU.
    const supported = cards.find(([,driver,cap]) => Number(driver) >= 572.61 && Number(cap) >= 5 && Number(cap) < 13);
    if (supported) return {backend:'cuda', name:supported[0], detail:'已检测到 NVIDIA 显卡，将自动准备 GPU 加速组件'};
    return {backend:'cpu', name:cards[0]?.[0] || 'CPU', detail:'当前驱动或显卡不在自动加速支持范围，使用 CPU 环境'};
  } catch (error) {
    if (!['ENOENT','EACCES','EPERM'].includes(error.code) && !(Number.isInteger(error.code) && error.code > 0) && !error.killed) throw error;
    return {backend:'cpu', name:'CPU', detail:'未检测到可用的 NVIDIA 驱动，自动选择 CPU 环境'};
  }
}
async function extract(archive, destination, signal) {
  await execute(host().tar, ['-xf', archive, '-C', destination], {windowsHide:true, signal, timeout:600000, maxBuffer:1024*1024});
}
async function probe(runtime, backend, signal) {
  const script = `import json,sys,torch,torchvision,numpy,cv2,scipy,matplotlib,sam2
device='cuda' if sys.argv[1]=='cuda' else 'cpu'
if device=='cuda' and not torch.cuda.is_available(): raise RuntimeError('GPU 运算不可用，请更新驱动或选择 CPU')
x=torch.ones((8,8),device=device); assert torch.isfinite(x@x).all()
boxes=torch.tensor([[0.,0.,2.,2.],[0.,0.,1.,1.]],device=device)
assert torchvision.ops.nms(boxes,torch.tensor([.9,.5],device=device),.5).numel()==2
print(json.dumps({'device':device,'torch':torch.__version__}))`;
  const env = {...process.env, PYTHONPATH:path.join(runtime,'sam2'), PYTHONNOUSERSITE:'1', PYTHONUTF8:'1', PYTHONDONTWRITEBYTECODE:'1'};
  delete env.PYTHONHOME;
  try {
    return await execute(path.join(runtime, host().python), ['-B', '-c', script, backend], {env, windowsHide:true, signal, timeout:120000, maxBuffer:1024*1024});
  } catch (error) {
    const reason = (error.stderr || error.message).trim().split(/\r?\n/).at(-1);
    throw Error('运行环境验证失败：'+reason, {cause:error});
  }
}
module.exports = {host, detect, extract, probe};

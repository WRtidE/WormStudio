'use strict';
const path = require('node:path');
const fs = require('node:fs');
const {createHash} = require('node:crypto');
const os = require('node:os');
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
async function extractWheel(archive, destination, signal) {
  const env = {...process.env, PYTHONNOUSERSITE:'1', PYTHONUTF8:'1', PYTHONDONTWRITEBYTECODE:'1'};
  delete env.PYTHONHOME;
  delete env.PYTHONPATH;
  const installer = path.resolve(__dirname, '../../tools/release/windows_gpu.py');
  await execute(path.join(destination, host().python), ['-I', '-B', installer, '--extract-runtime-wheel',
    archive, path.join(destination, 'python/Lib/site-packages')],
    {env, windowsHide:true, signal, timeout:600000, maxBuffer:1024*1024});
}
async function probe(runtime, backend, signal) {
  return probePython(path.join(runtime, host().python), backend, signal, path.join(runtime,'sam2'));
}

async function probePython(python, backend, signal, sam2) {
  const script = `import json,sys,torch,torchvision,numpy,cv2,scipy,matplotlib,sam2
device='cuda' if sys.argv[1]=='cuda' else 'cpu'
if device=='cuda' and not torch.cuda.is_available(): raise RuntimeError('GPU 运算不可用，请更新驱动或选择 CPU')
x=torch.ones((8,8),device=device); assert torch.isfinite(x@x).all()
boxes=torch.tensor([[0.,0.,2.,2.],[0.,0.,1.,1.]],device=device)
assert torchvision.ops.nms(boxes,torch.tensor([.9,.5],device=device),.5).numel()==2
print(json.dumps({'device':device,'torch':torch.__version__}))`;
  const env = {...process.env, PYTHONNOUSERSITE:'1', PYTHONUTF8:'1', PYTHONDONTWRITEBYTECODE:'1'};
  delete env.PYTHONHOME;
  if (sam2) env.PYTHONPATH = sam2;
  else delete env.PYTHONPATH;
  try {
    return await execute(python, ['-B', '-c', script, backend], {env, windowsHide:true, signal, timeout:120000, maxBuffer:1024*1024});
  } catch (error) {
    const reason = (error.stderr || error.message).trim().split(/\r?\n/).at(-1);
    throw Error('运行环境验证失败：'+reason, {cause:error});
  }
}

// A checkout owns its uv environment. Keep the running CPU interpreter available
// while building the locked CUDA environment, including across failed retries.
function workspaceGpu(workspace, basePython, {run = execute, verify = probePython} = {}) {
  const hash = createHash('sha256').update(path.resolve(basePython).toLowerCase());
  for (const name of ['pyproject.toml','uv.lock','.python-version']) {
    const file = path.join(workspace,name);
    if (!fs.existsSync(file)) return null;
    hash.update(fs.readFileSync(file));
  }
  const directory = path.join(workspace,'.venv-gpu',hash.digest('hex').slice(0,16));
  const python = path.join(directory,'Scripts','python.exe');
  const marker = path.join(directory,'wormstudio-cuda-ready');
  const readyPython = () => fs.existsSync(marker) && fs.existsSync(python) ? python : null;
  async function install(signal, progress, withWheels) {
    if (readyPython()) return;
    await fs.promises.rm(marker,{force:true});
    const localUv = path.join(os.homedir(),'.local','bin','uv.exe');
    const uv = process.env.UV || (fs.existsSync(localUv) ? localUv : 'uv.exe');
    const env = {...process.env, UV_PROJECT_ENVIRONMENT:directory};
    delete env.VIRTUAL_ENV;
    const options = {cwd:workspace, env, signal, windowsHide:true, maxBuffer:4*1024*1024};
    const helper = path.join(workspace,'tools/release/windows_gpu.py');
    const {stdout} = await run(basePython,['-I','-B',helper,'--workspace-gpu-assets',workspace],options);
    try {
      await withWheels(JSON.parse(stdout),async (links,requirements) => {
        progress('正在安装已校验的 GPU 组件…');
        if (!fs.existsSync(python)) await run(uv,['venv','--allow-existing','--managed-python',directory],options);
        // Install as indexed distributions so the subsequent locked sync can
        // reuse them and still resolve and check all other project dependencies.
        await run(uv,['pip','install','--python',python,'--no-cache','--no-deps','--no-index','--find-links',links,
          '--require-hashes','-r',requirements],options);
      });
      await run(uv,['sync','--locked','--extra','cuda','--managed-python'],options);
    } catch (error) {
      if (error.code === 'ENOENT' && error.path === uv) throw Error('找不到项目的 uv 安装器。请使用 Windows 在线发行版安装 GPU 组件。');
      const detail = String(error.stderr || error.message).trim().slice(-600);
      throw Error('GPU 组件安装未完成，可重试。'+detail, {cause:error});
    }
    progress('正在验证显卡矩阵运算和图像算子…');
    await verify(python,'cuda',signal);
    signal.throwIfAborted();
    await fs.promises.writeFile(marker,'cuda');
  }
  return {readyPython, install};
}
async function gpuOffer(state, message, mode, hardware = detect) {
  // The legacy probe reports "incompatible" for a CPU venv or online layout
  // that lacks the portable offline kit's metadata in its expected location.
  if (!mode || !['available','prepared','incompatible'].includes(state)) return {state,message};
  const device = await hardware('win32-x64');
  if (device.backend !== 'cuda') return {state:'cpu',message:device.detail};
  return {state:mode, message:`已检测到 ${device.name}，当前使用 CPU。可安装与此版本匹配的 GPU 加速组件。`};
}
module.exports = {host, detect, extract, extractWheel, probe, workspaceGpu, gpuOffer};

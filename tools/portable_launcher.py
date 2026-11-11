"""Copied to launch.py at the root of a native portable distribution."""
import datetime
import os
from pathlib import Path
import subprocess
import sys


def runtime_paths(root, platform=None):
    runtime = Path(root) / 'runtime'
    if (platform or sys.platform) == 'win32':
        return runtime/'python.exe', [runtime, runtime/'Library/bin', runtime/'Scripts'], runtime/'Scripts/conda-unpack-script.py'
    return runtime/'bin/python', [runtime/'bin'], runtime/'bin/conda-unpack'


def main(root=None, mode='auto', check_only=False):
    root = Path(root or Path(__file__).resolve().parent).resolve()
    app = root/'app'
    python, search, unpack = runtime_paths(root)
    if not python.is_file():
        raise RuntimeError('运行环境缺失。请使用适合本机系统和芯片的完整软件包。')
    env = os.environ.copy()
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               WORM_STUDIO_PYTHON=str(python), WORM_STUDIO_RESOURCES=str(app/'resources'),
               WORMSTUDIO_SINGLE_ROOT=str(app/'packages/single-worm-methods'),
               WORMSTUDIO_TAPNEXT_CHECKPOINT=str(app/'models/tapnextpp_ckpt.pt'),
               WORMSTUDIO_SAM2_CHECKPOINT=str(app/'models/sam2_hiera_large.pt'),
               PYTHONPATH=str(app/'resources/sam2'), WORMSTUDIO_DEVICE=mode)
    env['PATH'] = os.pathsep.join([*map(str, search), env.get('PATH', '')])
    if mode == 'mps':
        env['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
    logs = root/'logs'
    logs.mkdir(exist_ok=True)
    log = logs/(datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.log')
    with log.open('w', encoding='utf-8') as handle:
        marker = root/'runtime/.unpacked'
        if not marker.exists() and unpack.exists():
            try:
                subprocess.run([str(python), str(unpack)], check=True, env=env,
                               stdout=handle, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError as exc:
                handle.flush()
                print('首次启动的环境修复失败，详细原因：\n'+log.read_text(encoding='utf-8', errors='replace')[-4000:])
                print('请将此日志交给维护人员：'+str(log))
                return exc.returncode
            marker.write_text(str(root/'runtime'), encoding='utf-8')
        print('正在检查环境。CPU 分析速度较慢，请保留此窗口。', flush=True)
        scripts = ['tools/check_environment.py']
        if not check_only:
            scripts.append('studio.py')
        for script in scripts:
            with subprocess.Popen([str(python), script], cwd=app, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  encoding='utf-8', errors='replace') as proc:
                for line in proc.stdout:
                    print(line, end='', flush=True)
                    handle.write(line)
                    handle.flush()
                code = proc.wait()
            if code:
                print('启动或运行失败，日志：'+str(log))
                return code
    return 0


if __name__ == '__main__':
    raise SystemExit(main(mode=sys.argv[1] if len(sys.argv)>1 else 'auto',
                          check_only='--check' in sys.argv[2:]))

"""Exercise the Windows batch and Electron entry points in an isolated source export."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen


def main():
    if sys.platform != 'win32':
        raise SystemExit('This smoke test requires a native Windows runner.')
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / '线虫 Windows'
        subprocess.run([sys.executable, repo / 'tools/export_source.py', '--output', workspace], check=True)
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages',
                        workspace / '.venv'], check=True)
        env = dict(os.environ, CI='1', WORMSTUDIO_DEVICE='cpu')
        for key in ('VIRTUAL_ENV', 'CONDA_PREFIX', 'WORM_STUDIO_PYTHON', 'WORM_STUDIO_ELECTRON'):
            env.pop(key, None)
        batch = [os.environ['ComSpec'], '/d', '/c', 'start.bat']
        subprocess.run([*batch, '--help'], cwd=workspace, env=env, check=True, timeout=120)
        (workspace / 'config/python_path.txt').write_text(
            str(workspace / '.venv/Scripts/python.exe'), encoding='utf-8-sig')
        with subprocess.Popen(batch, cwd=workspace, env=env, stdin=subprocess.DEVNULL) as desktop:
            try:
                log = workspace / 'logs/electron.log'
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    if desktop.poll() is not None:
                        raise RuntimeError(f'Desktop exited before startup: {desktop.returncode}')
                    if log.exists():
                        match = re.search(r'^Worm Studio: (http://127\.0\.0\.1:\d+)/studio$',
                                          log.read_text(encoding='utf-8', errors='replace'), re.MULTILINE)
                        if match:
                            break
                    time.sleep(.5)
                else:
                    raise TimeoutError('No desktop backend URL in the startup log')
                origin = match.group(1)
                with urlopen(origin + '/studio', timeout=10) as response:
                    if response.status != 200:
                        raise RuntimeError('Studio page did not load')
                with urlopen(origin + '/api/progress', timeout=10) as response:
                    ready = json.load(response)
                if ready['status'] != 'ready':
                    raise RuntimeError(f'Unexpected workspace status: {ready["status"]}')
                # Electron shows the native window after loadURL('/studio') completes.
                electron, = (workspace / '.electron-runtime').glob('*/win32-x64/electron.exe')
                window_check = (
                    "$end = (Get-Date).AddSeconds(30); "
                    "do { if (Get-Process electron -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.Path -eq $env:WORM_SMOKE_EXE -and $_.MainWindowHandle -ne 0 "
                    "-and $_.MainWindowTitle -like 'WormStudio*' }) { exit 0 }; "
                    "Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $end); exit 1"
                )
                subprocess.run(['powershell.exe', '-NoProfile', '-Command', window_check],
                               env=dict(env, WORM_SMOKE_EXE=str(electron)), check=True, timeout=40)
                close = Request(origin + '/close', data=b'{}',
                                headers={'X-Annotation-Token': ready['token']})
                with urlopen(close, timeout=10) as response:
                    if not json.load(response)['closed']:
                        raise RuntimeError('Workspace did not close')
                if desktop.wait(timeout=30):
                    raise RuntimeError('Desktop exited with a failure')
            finally:
                if desktop.poll() is None:
                    subprocess.run(['taskkill.exe', '/pid', str(desktop.pid), '/t', '/f'], check=False)
    print('Windows batch, Electron backend, workspace HTTP and shutdown passed.')


if __name__ == '__main__':
    main()

"""Exercise the Windows batch and Electron entry points in an isolated source export."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen


@contextmanager
def smoke_directory():
    directory = tempfile.TemporaryDirectory()
    try:
        yield Path(directory.name).resolve()
    finally:
        failure = sys.exc_info()[1]
        deadline = time.monotonic() + 10
        while True:
            try:
                directory.cleanup()
                break
            except PermissionError as exc:
                if time.monotonic() >= deadline:
                    if failure is None:
                        raise
                    failure.add_note(f'Temporary workspace cleanup also failed: {exc}')
                    break
                time.sleep(.25)


def wait_for_window(desktop, log):
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    deadline = time.monotonic() + 30
    observed = 'Electron has not reported a visible window'
    while time.monotonic() < deadline:
        contents = log.read_text(encoding='utf-8', errors='replace')
        if desktop.poll() is not None:
            break
        match = re.search(r'^WormStudio window shown: pid=(\d+) hwnd_le=([0-9a-f]+)$', contents, re.M)
        if match:
            hwnd = int.from_bytes(bytes.fromhex(match[2]), 'little')
            owner = wintypes.DWORD()
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            user32.GetWindowTextW(hwnd, title, len(title))
            visible = bool(user32.IsWindowVisible(hwnd))
            if visible and owner.value == int(match[1]) and title.value.startswith('WormStudio'):
                return
            observed = f'handle={hwnd} owner={owner.value} visible={visible} title={title.value!r}'
        time.sleep(.25)
    raise RuntimeError(f'Window check failed: {observed}; desktop exit={desktop.poll()}\n{contents[-3000:]}')


def close_workspace(origin, token):
    request = Request(origin + '/close', data=b'{}', headers={'X-Annotation-Token': token})
    with urlopen(request, timeout=10) as response:
        if not json.load(response)['closed']:
            raise RuntimeError('Workspace did not close')


def main():
    if sys.platform != 'win32':
        raise SystemExit('This smoke test requires a native Windows runner.')
    repo = Path(__file__).resolve().parents[1]
    with smoke_directory() as tmp:
        workspace = tmp / '线虫 Windows'
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
            ready = None
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
                wait_for_window(desktop, log)
                close_workspace(origin, ready['token'])
                if desktop.wait(timeout=30):
                    raise RuntimeError('Desktop exited with a failure')
            finally:
                if desktop.poll() is None:
                    if ready and ready.get('token'):
                        try:
                            close_workspace(origin, ready['token'])
                            desktop.wait(timeout=10)
                        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                            print(f'Graceful shutdown failed: {exc}', file=sys.stderr)
                    if desktop.poll() is None:
                        subprocess.run(['taskkill.exe', '/pid', str(desktop.pid), '/t', '/f'], check=False)
                        desktop.wait(timeout=15)
    print('Windows batch, Electron backend, workspace HTTP and shutdown passed.')


if __name__ == '__main__':
    main()

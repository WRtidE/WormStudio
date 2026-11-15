"""Run the shared desktop shell with this Python environment and workspace."""
import atexit
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '45.3.0'
HASHES = {
    'darwin-arm64': 'b0c7cbb70f98ff72beffa98b4c449a7d92d8b51d3d25817cfcdcb6ee2d61711a',
    'darwin-x64': '17b39cede4bd26ec9a125ffa405f324e9eb6c5c2ae2aa8a6060c310d518134a1',
    'linux-arm64': 'a79c1e639f4a02d5039eca296144d74c87da3018410e7d86c421efdc2c7df64c',
    'linux-x64': '4930ece46f988e5874df77e943d98a10655d3da9e268e66ed86c1250bdca089c',
    'win32-arm64': 'cfe6a3e7a756e4b70ecc32f89c40a0823cdd446941976acff252128cfebd73a7',
    'win32-x64': 'b43b532e712a4323f4224627b73808a039c9769fdf7ba1dbc3cb49c1dbfa350b',
}
BIN = {'darwin': 'Electron.app/Contents/MacOS/Electron', 'win32': 'electron.exe', 'linux': 'electron'}


def verified_archive(archive, target):
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != HASHES[target]:
            raise ValueError(f'Electron {VERSION} {target} 下载文件校验失败')


def runtime_dir():
    machine = platform.machine().lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64'}.get(machine, machine)
    target = f'{sys.platform}-{arch}'
    if target not in HASHES:
        raise RuntimeError(f'当前没有配置 {target} 桌面运行时')
    dest = ROOT/'.electron-runtime'/VERSION/target
    if (dest/BIN[sys.platform]).is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        archive = Path(tmp)/'download.zip'
        url = f'https://github.com/electron/electron/releases/download/v{VERSION}/electron-v{VERSION}-{target}.zip'
        print(f'首次启动，正在准备 Electron {VERSION}（{target}）…', flush=True)
        with urlopen(url, timeout=90) as stream, archive.open('wb') as out:
            shutil.copyfileobj(stream, out)
        verified_archive(archive, target)
        unpacked = Path(tmp)/'runtime'
        unpacked.mkdir()
        if sys.platform == 'win32':
            with zipfile.ZipFile(archive) as z:
                z.extractall(unpacked)
        else:
            command = ['ditto', '-x', '-k', archive, unpacked] if sys.platform == 'darwin' else ['unzip', '-q', archive, '-d', unpacked]
            subprocess.run(command, check=True)
        try:
            unpacked.rename(dest)
        except OSError:
            if not (dest/BIN[sys.platform]).is_file():
                raise
    return dest


def command():
    override = os.getenv('WORM_STUDIO_ELECTRON')
    if override:
        return [override, str(ROOT/'tools/electron')]
    if sys.platform == 'darwin':
        contents = Path.home()/'Applications/WormStudio.app/Contents'
        config = contents/'Resources/app/local.json'
        if config.is_file() and json.loads(config.read_text()).get('workspace') == str(ROOT):
            return [str(contents/'MacOS/Electron')]
    return [str(runtime_dir()/BIN[sys.platform]), str(ROOT/'tools/electron')]


def environment():
    return dict(os.environ, WORM_STUDIO_WORKSPACE=str(ROOT), WORM_STUDIO_PYTHON=sys.executable)


def launch():
    return subprocess.call(command(), env=environment(), cwd=ROOT)


def open_view(url, on_close):
    # CLI review/annotation servers own their lifetime. Closing either side
    # releases the other without touching a separately running desktop session.
    args = command()
    profile = tempfile.TemporaryDirectory(prefix='wormstudio-view-')
    process = subprocess.Popen([*args, '--user-data-dir='+profile.name], stdin=subprocess.PIPE,
                               env=dict(environment(), WORM_STUDIO_VIEW_URL=url))
    def stop():
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait()

    def follow():
        process.wait()
        on_close()
        atexit.unregister(stop)
        profile.cleanup()

    atexit.register(stop)
    threading.Thread(target=follow, daemon=True).start()
    return stop

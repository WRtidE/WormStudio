"""Finder entry: offline environment, persistent workspace and Electron window."""
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
import tempfile


def dialog(message, *, question=False):
    script = ('on run argv\n'
              'display dialog (item 1 of argv) with title "WormStudio" '
              + ('buttons {"取消", "准备并打开"} default button 2 cancel button 1'
                 if question else 'buttons {"好"} default button 1')
              + '\nend run')
    return subprocess.run(['/usr/bin/osascript', '-e', script, message]).returncode == 0


def install(res, base):
    # Install from pinned, hash-checked local wheels; interrupted installs can be retried.
    import hashlib
    lock = res/'requirements.lock'
    name = 'cp311-' + hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
    base.mkdir(exist_ok=True, parents=True)
    target = base/name
    if target.is_dir():
        return target
    if not dialog('首次打开需要在本机展开分析组件，可能需要几分钟。无需联网。准备完成后，会自动进入线虫工作台。', question=True):
        return None
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        pending = Path(tmp)/'packages'
        subprocess.run([sys.executable, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check',
                        'install', '--no-index', '--no-cache-dir', '--no-compile', '--only-binary=:all:',
                        '--require-hashes', '--ignore-installed', '--find-links', str(res/'wheels'),
                        '--target', str(pending), '-r', str(lock)], check=True)
        pending.rename(target)
    return target


def populate(res, workspace):
    # Copy only shipped code and defaults; mutable catalogues, sessions, videos and
    # results live in this stable path and never in the read-only app bundle.
    shutil.copytree(res/'app', workspace, dirs_exist_ok=True)
    sample = workspace/'examples/practice.avi'
    sys.path.insert(0, str(workspace/'src'))
    from workspace_library import register
    register(workspace, sample, auto=True)


def run(res, base, logfile):
    import fcntl
    with (base/'session.lock').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            dialog('WormStudio 已经打开，请切换到已有窗口。')
            return
        packages = install(res, base/'environments')
        if packages is None:
            return
        site.addsitedir(str(packages))
        workspace = base/'workspace'
        populate(res, workspace)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, [packages, res/'sam2'])),
                   WORM_STUDIO_PYTHON=sys.executable,
                   WORM_STUDIO_RESOURCES=str(res), WORMSTUDIO_SINGLE_ROOT=str(workspace/'packages/single-worm-methods'),
                   WORMSTUDIO_TAPNEXT_CHECKPOINT=str(res/'models/tapnextpp_ckpt.pt'),
                   WORMSTUDIO_SAM2_CHECKPOINT=str(res/'models/sam2_hiera_large.pt'))
        runtime = res/'electron/Electron.app/Contents/MacOS/Electron'
        env.update(WORM_STUDIO_ELECTRON=str(runtime), WORM_STUDIO_WORKSPACE=str(workspace), PYTHONUTF8='1', MPLBACKEND='Agg')
        subprocess.run([str(runtime), str(workspace/'tools/electron')], cwd=workspace, env=env, check=True)


if __name__ == '__main__':
    run(*map(Path, sys.argv[1:]))

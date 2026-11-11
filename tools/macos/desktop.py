"""Finder entry: offline environment, persistent workspace and native WebKit window."""
import html
import os
from pathlib import Path
import re
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import threading


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


def message_page(title, text):
    return ('<!doctype html><meta charset=utf-8><style>body{font:18px -apple-system;'
            'color:#14453e;background:#f4f9f7;margin:12vh 10vw}small{color:#487b70}'
            'p{line-height:1.7;overflow-wrap:anywhere}</style><small>WORMSTUDIO</small><h1>'
            + html.escape(title) + '</h1><p>' + html.escape(text) + '</p>')


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
                   WORM_STUDIO_PYTHON=sys.executable, WORMSTUDIO_DEVICE='cpu',
                   WORM_STUDIO_RESOURCES=str(res), WORMSTUDIO_SINGLE_ROOT=str(workspace/'packages/single-worm-methods'),
                   WORMSTUDIO_TAPNEXT_CHECKPOINT=str(res/'models/tapnextpp_ckpt.pt'),
                   WORMSTUDIO_SAM2_CHECKPOINT=str(res/'models/sam2_hiera_large.pt'))
        open_window(workspace, env, logfile)


def open_window(workspace, env, logfile):
    import webview
    from webview.menu import Menu, MenuAction

    results = workspace/'outputs'
    results.mkdir(exist_ok=True)
    menu = [Menu('工作区', [MenuAction('在 Finder 中打开结果目录', lambda: subprocess.run(['/usr/bin/open', str(results)])),
                          MenuAction('在 Finder 中打开工作区', lambda: subprocess.run(['/usr/bin/open', str(workspace)]))])]
    webview.settings['ALLOW_DOWNLOADS'] = True
    window = webview.create_window('WormStudio · 线虫分析', width=1200, height=800, min_size=(800, 560),
                                   html=message_page('正在打开工作台', '正在载入本地视频分析组件…'),
                                   confirm_close=True)
    # The child process group includes pipeline stages and video encoders.
    # Closing the GUI must not leave them running or lock the workspace.
    proc = subprocess.Popen([sys.executable, '-u', '-c', 'import studio; studio.main(open_browser=False)'],
                            cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, text=True, encoding='utf-8', errors='replace')
    closing = threading.Event()

    def follow():
        ready = False
        for line in proc.stdout:
            print(line, end='', flush=True)
            match = re.fullmatch(r'Worm Studio: (http://127\.0\.0\.1:\d+/studio)', line.strip())
            if match and not closing.is_set() and not ready:
                ready = True
                window.load_url(match[1])
        status = proc.wait()
        if not closing.is_set():
            if ready and not status:
                window.destroy()
            else:
                window.load_html(message_page('工作台未能继续运行', f'请查看本次启动日志：{logfile}'))

    try:
        webview.start(follow, gui='cocoa', menu=menu, localization={
            'global.quitConfirmation': '退出将停止当前分析。确定退出 WormStudio 吗？',
            'global.ok': '确定', 'global.cancel': '取消'})
    finally:
        closing.set()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except ProcessLookupError:
            pass

if __name__ == '__main__':
    run(*map(Path, sys.argv[1:]))

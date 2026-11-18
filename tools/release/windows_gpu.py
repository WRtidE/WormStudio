"""Install the optional offline accelerator into a WormStudio Windows desktop installation."""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from zipfile import BadZipFile, ZipFile


# Official Windows CPython 3.11 wheels from the PyTorch cu128 indexes.
WHEELS = {
    'torch-2.7.0+cu128-cp311-cp311-win_amd64.whl':
        'bf88f647d76d79da9556ca55df49e45aff1d66c12797886364343179dd09a36c',
    'torchvision-0.22.0+cu128-cp311-cp311-win_amd64.whl':
        'f5dae1307c34813425c0b753530c035e1cc72af0bded395d1ba64dcb2872889f',
}
OVERLAY = 'gpu-cp311-torch270-cu128'
BASE_PACKAGES = ('torch-2.7.0', 'torchvision-0.22.0')
CHUNK = 4 * 1024 * 1024
GPU_PARTS = ('WormStudio-GPU-cu128-part1.zip', 'WormStudio-GPU-cu128-part2.zip')
PART_BYTES = 1600 * 1024**2


@contextmanager
def wheel_directory(source, scratch, update):
    """Accept the two downloaded ZIPs in-place, or a legacy directory of wheels."""
    parts = [source / name for name in GPU_PARTS]
    if not any(p.is_file() for p in parts):
        if not all((source / name).is_file() for name in WHEELS):
            raise ValueError('请选择同时包含两个 GPU 分包的文件夹，或旧版 wheels 文件夹。')
        yield source
        return
    for part in parts:
        if not part.is_file():
            raise ValueError(f'缺少 {part.name}，请将两个 GPU 分包下载到同一个文件夹。')
    torch, vision = WHEELS
    with tempfile.TemporaryDirectory(dir=scratch) as temp, ExitStack() as stack:
        folder = Path(temp)
        try:
            archives = [stack.enter_context(ZipFile(part)) for part in parts]
            members = [(archives[0], torch + '.001', torch),
                       (archives[1], torch + '.002', torch), (archives[1], vision, vision)]
            total = sum(archive.getinfo(member).file_size for archive, member, _ in members)
        except (BadZipFile, KeyError) as exc:
            raise ValueError('GPU 分包损坏或版本不匹配，请重新下载同一版本的两个分包。') from exc
        if total > 2 * 2**31 or shutil.disk_usage(scratch).free < total + 64 * 1024**2:
            raise OSError('分包异常或临时空间不足，请检查下载文件和软件所在磁盘空间。')
        done = 0
        for archive, member, name in members:
            with archive.open(member) as src, (folder / name).open('ab') as dest:
                while block := src.read(CHUNK):
                    dest.write(block)
                    done += len(block)
                    update('正在读取离线分包', done, total)
        yield folder


def probe_runtime(overlay=None):
    """Run only in a disposable process using the desktop's analysis Python."""
    if overlay:
        sys.path.insert(0, str(overlay))
    try:
        import torch
        if torch.version.cuda is None:
            return 'cpu', ''
        import torchvision
        if overlay:
            if not all(Path(m.__file__).resolve().is_relative_to(Path(overlay).resolve())
                       for m in (torch, torchvision)):
                return 'failed', ''
            if torch.__version__ != '2.7.0+cu128' or torchvision.__version__ != '0.22.0+cu128':
                return 'failed', ''
        boxes = torch.tensor([[0., 0., 1., 1.]], device='cuda')
        if torchvision.ops.nms(boxes, boxes[:, 2], 0.5).tolist() == [0]:
            return 'ready', torch.cuda.get_device_name()
    except (ImportError, OSError, RuntimeError, AttributeError, AssertionError):
        pass
    return 'failed', ''


def runtime_status(python, overlay=None):
    try:
        result = subprocess.run([python, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                                 '--probe', str(overlay or '')],
                                capture_output=True, encoding='utf-8', errors='replace', timeout=90,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 'failed', ''


def gpu_name(python, overlay=None):
    state, name = runtime_status(python, overlay)
    return name if state == 'ready' else ''


def base_metadata():
    return [(name + '.dist-info', name + '+cpu.dist-info') for name in BASE_PACKAGES]


def detect(python, probe=None):
    """Read-only preflight. Only the Python used by this desktop installation matters."""
    try:
        check_driver()
    except RuntimeError:
        return 'cpu', '当前没有可用的 NVIDIA 显卡或驱动。可直接用 CPU 分析，无需下载加速包。'
    state, name = probe() if probe is not None else runtime_status(python)
    if state == 'ready':
        if os.environ.get('WORMSTUDIO_DEVICE', '').lower() == 'cpu':
            return 'ready', f'{name} 已通过 GPU 运算检测。本次启动已指定使用 CPU。'
        return 'ready', f'WormStudio 已能使用 {name} 计算。无需安装加速包。'
    if state != 'cpu':
        return 'retry', '当前运行环境未通过 GPU 运算检查。请检查 NVIDIA 驱动和工作台运行环境后重试。'
    site = python.parent / 'Lib/site-packages'
    if not all(any((site / entry).is_dir() for entry in names) for names in base_metadata()):
        return 'incompatible', '当前基础版使用 CPU。此离线加速包与该版本不匹配，可继续用 CPU 分析。'
    if (python.parent.parent / OVERLAY).is_dir():
        return 'prepared', '本机已准备加速组件，可直接验证并启用，无需再次下载。'
    return 'available', '已检测到 NVIDIA 显卡，当前工作台使用 CPU。需要 GPU 加速时再领取离线包（3.12 GiB）。'


def ensure_studio_closed():
    tasklist = Path(os.environ['SystemRoot']) / 'System32/tasklist.exe'
    result = subprocess.run([tasklist, '/FI', 'IMAGENAME eq WormStudio.exe', '/FO', 'CSV', '/NH'],
                            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('无法确认工作台是否已退出，请稍后重试。')
    if b'"wormstudio.exe"' in result.stdout.lower():
        raise RuntimeError('请在任务结束后退出 WormStudio，再点击启用。已准备好的组件可以继续使用。')


def check_driver():
    import ctypes
    message = '未检测到可用的 NVIDIA 显卡和驱动。你可以继续用 CPU 分析。'
    try:
        driver = ctypes.WinDLL(str(Path(os.environ['SystemRoot']) / 'System32/nvcuda.dll'))
    except OSError as exc:
        raise RuntimeError(message) from exc
    count = ctypes.c_int()
    if driver.cuInit(0) or driver.cuDeviceGetCount(ctypes.byref(count)) or not count.value:
        raise RuntimeError(message)


@contextmanager
def setup_lock(payload):
    import msvcrt
    with (payload / '.gpu-setup.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('另一份加速包正在安装，请等待该窗口完成。') from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def prepare(wheels, destination, update):
    """The two pinned wheels have importable packages at their root and no .data relocation."""
    with ExitStack() as stack:
        archives = []
        sources = [wheels / name for name in WHEELS]
        if any(not path.is_file() for path in sources):
            raise ValueError('请选择离线包中包含两份 .whl 文件的 wheels 文件夹。')
        total = sum(path.stat().st_size for path in sources)
        done = 0
        for path in sources:
            stream = stack.enter_context(path.open('rb'))
            digest = hashlib.sha256()
            while block := stream.read(CHUNK):
                digest.update(block)
                done += len(block)
                update('正在校验离线组件', done, total)
            if digest.hexdigest() != WHEELS[path.name]:
                raise ValueError(f'{path.name} 校验失败，请从原始离线包重新复制此文件。')
            archives.append(stack.enter_context(ZipFile(stream)))
        total = sum(item.file_size for archive in archives for item in archive.infolist())
        if shutil.disk_usage(destination.parent).free < total + 64 * 1024**2:
            raise OSError(f'软件所在磁盘至少需要 {total / 1024**3 + 0.07:.1f} GiB 可用空间。')
        done = 0
        for archive in archives:
            for item in archive.infolist():
                target = destination / item.filename
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open('xb') as output:
                    while block := source.read(CHUNK):
                        output.write(block)
                        done += len(block)
                        update('正在解压加速组件', done, total)


def install(python, wheels, update):
    runtime = python.parent
    payload = runtime.parent
    if (sys.platform != 'win32' or sys.version_info[:2] != (3, 11)
            or not (payload.parent.parent / 'WormStudio.exe').is_file()
            or not (payload / 'code/docs/models.json').is_file()
            or not (runtime / 'Lib/site-packages').is_dir()):
        raise RuntimeError('请选择使用 Python 3.11 的 Windows x64 版 WormStudio。')
    # Some torch headers exceed MAX_PATH inside a staging folder on Windows.
    prefix = str(payload.resolve())
    if not prefix.startswith('\\\\?\\'):
        prefix = ('\\\\?\\UNC\\' + prefix[2:]) if prefix.startswith('\\\\') else '\\\\?\\' + prefix
    payload = Path(prefix)
    with setup_lock(payload):
        update('正在检查现有 GPU 加速', 0, 0)
        name = gpu_name(python)
        if name:
            return f'GPU 加速已可用：{name}'
        site = runtime / 'Lib/site-packages'
        checks = []
        for names in base_metadata():
            if not any((site / entry).is_dir() for entry in names):
                raise RuntimeError('此加速包与当前基础版的 PyTorch 版本不匹配。')
            checks.append('(' + ' or '.join(
                f"os.path.isdir(os.path.join(sys.prefix, 'Lib/site-packages/{entry}'))" for entry in names) + ')')
        check_driver()
        overlay = payload / OVERLAY
        if not overlay.is_dir():
            with tempfile.TemporaryDirectory(dir=payload, prefix='.gpu-setup-') as tmp:
                stage = Path(tmp) / OVERLAY
                stage.mkdir()
                with wheel_directory(wheels, Path(tmp), update) as source:
                    prepare(source, stage, update)
                update('正在用显卡验证加速组件', 0, 0)
                name = gpu_name(python, stage)
                if not name:
                    raise RuntimeError('显卡验证未通过。请检查 NVIDIA 驱动，或继续使用 CPU 版。')
                stage.rename(overlay)
        else:
            name = gpu_name(python, overlay)
            if not name:
                raise RuntimeError('已有加速组件未通过显卡验证，请检查 NVIDIA 驱动。')
        ensure_studio_closed()
        update('正在启用已验证的组件', 0, 0)
        # Future base installations can change the Python ABI or PyTorch version.
        condition = ' and '.join(['sys.version_info[:2] == (3, 11)', *checks])
        line = (f"import os, sys; {condition} and "
                f"sys.path.insert(0, os.path.join(os.path.dirname(sys.prefix), '{OVERLAY}'))\n")
        hook = site / 'zz-wormstudio-gpu.pth'
        content = line.encode('ascii')
        if hook.exists() and hook.read_bytes() != content:
            raise RuntimeError('现有加速配置与此版本不同，请使用与桌面版匹配的加速包。')
        with tempfile.TemporaryDirectory(dir=site) as tmp:
            staged_hook = Path(tmp) / hook.name
            staged_hook.write_bytes(content)
            staged_hook.replace(hook)
        try:
            if not gpu_name(python):
                raise RuntimeError('启用后复查未通过，已切回原有运行环境。')
        except BaseException:
            hook.unlink(missing_ok=True)
            raise
        return f'GPU 加速已启用：{name}。现在可以打开 WormStudio。'


def main():
    import tkinter as tk
    from tkinter import filedialog, ttk
    root = tk.Tk()
    root.title('WormStudio · GPU 加速检测')
    root.resizable(False, False)
    pane = ttk.Frame(root, padding=22)
    pane.pack()
    label = ttk.Label(pane, text='正在检测这台电脑能否直接使用 GPU…', wraplength=430)
    label.pack(fill='x')
    bar = ttk.Progressbar(pane, length=430)
    bar.pack(pady=16)
    cancel = threading.Event()
    events = queue.Queue()
    running = False
    closing = False

    python = Path(sys.executable).resolve().with_name('python.exe')
    wheels = Path(__file__).resolve().with_name('wheels')

    def start(check_only=False):
        nonlocal running
        source = wheels
        if not check_only and not source.is_dir() and not (python.parent.parent / OVERLAY).is_dir():
            selected = filedialog.askdirectory(title='选择同时包含两个 GPU 分包的文件夹（也支持旧版 wheels 文件夹）')
            if not selected:
                return
            source = Path(selected)
        running = True
        button.state(['disabled'])
        def worker():
            last = 0
            previous = None
            def update(message, done, total):
                nonlocal last, previous
                if cancel.is_set():
                    raise InterruptedError('安装已取消。')
                now = time.monotonic()
                if previous != message or now - last > 0.12 or done == total:
                    events.put(('progress', (message, done, total)))
                    last, previous = now, message
            try:
                if check_only:
                    update('正在检测这台电脑能否直接使用 GPU…', 0, 0)
                    events.put(('detected', detect(python)))
                else:
                    events.put(('success', install(python, source, update)))
            except Exception as exc:
                events.put(('check_error' if check_only else 'error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll():
        nonlocal running
        while not events.empty():
            kind, value = events.get_nowait()
            if kind == 'progress':
                message, done, total = value
                bar.stop()
                if total:
                    bar.configure(mode='determinate', maximum=100, value=100 * done / total)
                    label.configure(text=f'{message}　{done / 1024**3:.2f} / {total / 1024**3:.2f} GiB')
                else:
                    label.configure(text=message)
                    bar.configure(mode='indeterminate')
                    bar.start()
            else:
                running = False
                bar.stop()
                if closing:
                    root.destroy()
                    return
                if kind == 'detected':
                    state, message = value
                    label.configure(text=message)
                    if state in ('available', 'prepared'):
                        label.configure(text=message + ' 请先退出工作台，再启用加速。')
                        title = '启用 GPU 加速' if wheels.is_dir() or state == 'prepared' else '选择已有离线包…'
                        button.configure(text=title, command=start)
                    elif state == 'retry':
                        button.configure(text='重新检测', command=lambda: start(True))
                    else:
                        button.configure(text='完成', command=root.destroy)
                else:
                    label.configure(text=value)
                    if kind == 'check_error':
                        button.configure(text='重新检测', command=lambda: start(True))
                bar.configure(mode='determinate', maximum=100, value=0 if kind in ('error', 'check_error') else 100)
                button.state(['!disabled'])
                if kind == 'success':
                    button.configure(text='完成', command=root.destroy)
        root.after(100, poll)

    def close():
        nonlocal closing
        if running:
            closing = True
            cancel.set()
            label.configure(text='正在结束安装…')
        else:
            root.destroy()
    button = ttk.Button(pane, text='正在检测…', state='disabled')
    button.pack(anchor='e')
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(0, lambda: start(True))
    root.after(100, poll)
    root.mainloop()


if __name__ == '__main__':
    if sys.argv[1:2] == ['--status-json']:
        print(json.dumps(detect(Path(sys.executable), probe_runtime)))
    elif sys.argv[1:2] == ['--probe']:
        print(json.dumps(probe_runtime(sys.argv[2])))
    else:
        try:
            main()
        except Exception as exc:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'WormStudio GPU', 0x10)

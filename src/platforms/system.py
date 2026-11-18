"""Host-OS services: memory pressure, process shutdown and atomic publication."""
import os
import subprocess
import sys


def is_darwin():
    return sys.platform == 'darwin'


def is_windows():
    return os.name == 'nt'


def memory_pressure_level():
    """Return macOS dispatch pressure (1 normal, 2 warning, 4 critical); 0 elsewhere."""
    if not is_darwin():
        return 0
    import ctypes
    level = ctypes.c_int()
    size = ctypes.c_size_t(ctypes.sizeof(level))
    status = ctypes.CDLL(None).sysctlbyname(b'kern.memorystatus_vm_pressure_level',
        ctypes.byref(level), ctypes.byref(size), None, 0)
    return level.value if status == 0 else 0


def check_memory_pressure():
    """Return True at warning level; stop the stage at critical pressure."""
    level = memory_pressure_level()
    if level == 4:
        raise RuntimeError('Mac 系统内存压力已达到严重级别，已停止分析。请保存并关闭占用内存的应用，'
                           '检查启动磁盘可用空间，然后在工作台选择“继续此任务”。')
    return level == 2


def stage_wait_slice(interactive):
    """A short poll keeps cancellation responsive; macOS also polls memory pressure."""
    return .6 if interactive or is_darwin() else 30


def terminate_children(process):
    """Kill the whole tree on Windows; the direct child is terminated by the caller."""
    if is_windows() and process.poll() is None:
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def publish_new_file(temporary, path):
    """Publish a complete file atomically, never overwriting an existing one."""
    if is_windows():
        os.rename(temporary, path)  # Windows rename refuses an existing destination.
    else:
        os.link(temporary, path)    # Atomic exclusive publication on POSIX.

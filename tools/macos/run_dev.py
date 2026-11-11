"""Open the Mac desktop UI directly from a clone; edits require no repackaging."""
import os
from pathlib import Path
import platform
import sys
from desktop import open_window

ROOT = Path(__file__).resolve().parents[2]

if __name__ == '__main__':
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise SystemExit('桌面开发入口需要原生 Apple 芯片 Mac。其他平台请运行 python studio.py。')
    env = dict(os.environ, WORM_STUDIO_PYTHON=sys.executable,
               WORMSTUDIO_DEVICE=os.environ.get('WORMSTUDIO_DEVICE', 'cpu'),
               PYTHONUTF8='1', MPLBACKEND='Agg')
    open_window(ROOT, env, '当前开发终端')

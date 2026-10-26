"""Shared runtime/model locations; data stays separate from the source package."""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def resource_root(project=PROJECT_ROOT):
    """Prefer an explicit/local resource directory; retain legacy workspace support."""
    configured = os.environ.get('WORM_STUDIO_RESOURCES')
    if configured:
        return Path(configured).expanduser().resolve()
    local = project / 'resources'
    if local.is_dir() or not (project.parent / 'TWARDISv0.1').is_dir():
        return local.resolve()
    return project.parent.resolve()


RESOURCE_ROOT = resource_root()

def runtime_python(project=PROJECT_ROOT):
    """A checkout runs in the active environment unless locally overridden."""
    setting = project / 'config/python_path.txt'
    configured = os.environ.get('WORM_STUDIO_PYTHON')
    if not configured and setting.is_file():
        configured = setting.read_text(encoding='utf-8-sig').strip()
    return Path(configured or sys.executable).expanduser().resolve()


RUNTIME_PYTHON = runtime_python()


def motion_constraints_path(project=PROJECT_ROOT):
    """Keep experiment-specific manual constraints out of shared defaults."""
    local = project / 'config/motion_constraints.local.json'
    return local if local.is_file() else project / 'config/motion_constraints.json'


def sam2_checkpoint(filename):
    """Find weights without requiring the development machine's folder layout."""
    explicit = os.environ.get('WORMSTUDIO_SAM2_CHECKPOINT')
    if explicit:
        return Path(explicit).expanduser().resolve()
    paths = [PROJECT_ROOT / 'models' / filename,
             RESOURCE_ROOT / 'sam2' / 'checkpoints' / filename,
             RESOURCE_ROOT / 'TWARDISv0.1/segment-anything-2/checkpoints' / filename]
    return next((p for p in paths if p.is_file()), paths[0])


def ensure_runtime():
    """Relaunch an entry point in the configured environment before heavy imports."""
    if not RUNTIME_PYTHON.is_file():
        raise RuntimeError(f'找不到 Python：{RUNTIME_PYTHON}。请检查 WORM_STUDIO_PYTHON 或 config/python_path.txt')
    if Path(sys.executable).resolve() != RUNTIME_PYTHON:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        print(f'切换至 Conda WormStudio：{RUNTIME_PYTHON}', flush=True)
        os.execv(str(RUNTIME_PYTHON), [str(RUNTIME_PYTHON), *sys.argv])

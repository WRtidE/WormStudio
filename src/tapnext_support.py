"""Locate the workspace's existing single-worm method without loading GPU models."""
import hashlib
import importlib.util
import importlib
import json
from functools import lru_cache
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'studio-tapnext-adapter-v1'


def validate_single_worm(count, model_size='large'):
    if count != 1:
        raise ValueError('SAM2 + TAPNext++ 仅支持单虫，请保留一条线虫或选择其他方法')
    if model_size != 'large':
        raise ValueError('SAM2 + TAPNext++ 使用 SAM2 Large，请以 Large 模型重新打开视频')


def resources():
    bundled = ROOT / 'packages' / 'single-worm-methods'
    default = bundled if bundled.is_dir() else ROOT.parent / 'github-ready' / 'single-worm-methods'
    package = Path(os.environ.get('WORMSTUDIO_SINGLE_ROOT', default)).expanduser().resolve()
    if not (package / 'wormstudio_single' / 'fusion_v4.py').is_file():
        raise ValueError('未找到本地 single-worm-methods 算法包，可用 WORMSTUDIO_SINGLE_ROOT 指定目录')
    explicit = os.environ.get('WORMSTUDIO_TAPNEXT_CHECKPOINT')
    candidates = [Path(explicit)] if explicit else [
        package / 'models' / 'tapnextpp_ckpt.pt',
        ROOT / 'models' / 'tapnextpp_ckpt.pt',
    ]
    checkpoint = next((p.resolve() for p in candidates if p.is_file()), None)
    if checkpoint is None:
        raise ValueError('未找到 TAPNext++ 权重，可用 WORMSTUDIO_TAPNEXT_CHECKPOINT 指定本地文件')
    return package, checkpoint


def availability():
    try:
        package, checkpoint = resources()
        prepare_runtime()
        verify_checkpoint(package, checkpoint)
        return dict(available=True, detail='依赖可导入，权重 SHA-256 通过 · v4 实验版融合')
    except Exception as exc:
        # Optional capability probes are an isolation boundary, including OSError,
        # ImportError, broken package metadata and corrupt model manifests.
        detail = str(exc) or type(exc).__name__
        if isinstance(exc, PermissionError): detail = '无权读取 TAPNext++ 依赖或权重：' + detail
        return dict(available=False, detail=detail)


@lru_cache(maxsize=8)
def _checkpoint_hash(path, size, mtime_ns, ctime_ns):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(4*1024*1024), b''): digest.update(chunk)
    return digest.hexdigest()


def verify_checkpoint(package, checkpoint):
    manifest = json.loads((package/'docs'/'models.json').read_text(encoding='utf-8'))
    expected = manifest['tapnext']['sha256']
    stat = checkpoint.stat()
    actual = _checkpoint_hash(str(checkpoint), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if actual != expected:
        raise ValueError('TAPNext++ 权重 SHA-256 与模型清单不符，文件损坏或版本错误')
    return actual


def dependency_path():
    if importlib.util.find_spec('einops') is not None:
        return None
    raise ValueError('缺少 TAPNext++ 依赖 einops；请在 WormStudio 环境安装 einops==0.8.1')


def prepare_runtime():
    """Use the formal runtime environment; never import from validation folders."""
    dependency_path()
    einops = importlib.import_module('einops')
    return dict(einops_version=einops.__version__, einops_path=einops.__file__)


def signature():
    """Invalidate fits when local algorithm code or the selected weights change."""
    package, checkpoint = resources()
    digest = hashlib.sha256(VERSION.encode())
    for path in (Path(__file__), ROOT / 'src' / 'extract_tapnext.py'):
        digest.update(path.read_bytes())
    for path in sorted((package / 'wormstudio_single').rglob('*.py')):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(path.read_bytes())
    stat = checkpoint.stat()
    digest.update(f'{checkpoint}:{stat.st_size}:{stat.st_mtime_ns}'.encode())
    return digest.hexdigest()

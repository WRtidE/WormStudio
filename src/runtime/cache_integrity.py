"""Content snapshots for stage outputs, independent of the run directory name."""
import hashlib
from pathlib import Path


def snapshot(stage, base):
    base=Path(base).resolve()
    result={}
    for name in stage['outputs']:
        path=Path(name).resolve()
        relative=path.relative_to(base).as_posix()
        digest=hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
        result[relative]=digest.hexdigest()
    return result


def matches(stage, base, record):
    """Legacy runs have no content history; structural checks remain mandatory."""
    expected=record.get('output_sha256')
    if expected is None:return True
    try:return expected==snapshot(stage,base)
    except (OSError,ValueError):return False

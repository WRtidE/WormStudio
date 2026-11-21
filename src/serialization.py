"""Serialize numerical records without changing measurement inputs."""
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np


def json_safe(value):
    if isinstance(value,dict):return {k:json_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [json_safe(v) for v in value]
    if isinstance(value,(float,np.floating)):return float(value) if np.isfinite(value) else None
    if isinstance(value,np.integer):return int(value)
    return value


def write_json_records(path, records):
    """Stream an array one record at a time, publishing only a complete file."""
    path = Path(path)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                prefix='.'+path.name+'.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write('[')
            separator = '\n'
            for record in records:
                handle.write(separator)
                json.dump(json_safe(record), handle, ensure_ascii=False, indent=2, allow_nan=False)
                separator = ',\n'
            handle.write('\n]\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

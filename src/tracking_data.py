"""Read saved tracking masks and validate their quality records."""
import csv
import json
from pathlib import Path

import numpy as np
from image_io import read_image
from multiworm_quality import VERSION as GUARD_VERSION
from quality_schema import VERSION
from resource_paths import PROJECT_ROOT as ROOT

RECOVERY_FOLDER = 'crop-recovery'


def load_sample(sample,mask_source=None,apply_guards=True,*,root=None):
    base=(root or ROOT)/'outputs'/sample
    snapshot = base/'pipeline-config.json'
    configured = snapshot.is_file()
    config = json.loads(snapshot.read_text(encoding='utf-8')) if configured and (mask_source is None or apply_guards) else {}
    default_source='multiworm-tracking-manual' if not configured and sample=='2-1-1314' else 'sam2-tracking'
    data=Path(mask_source) if mask_source is not None else base/default_source
    if mask_source is None and configured:
        data=tracking_directory(base,config)
    summary=json.loads((data/'summary.json').read_text(encoding='utf-8'))
    summary['mask_source']=str(data)
    n=summary['frames'];bad={}
    if (data/'masks').is_dir():
        with (data/'tracking-quality.csv').open() as handle:
            for row in csv.DictReader(handle):
                bad[(int(row['frame']),int(row['worm_id']))]=row['low_confidence'].lower()=='true'
        def get_mask(worm,frame):return read_image(data/'masks'/f'worm-{worm}'/f'{frame:06d}.png',True)>0
        ids=summary.get('worm_ids') or sorted(int(p.name.split('-')[-1]) for p in (data/'masks').glob('worm-*'))
    else:
        with np.load(data/'masks-packed.npz') as packed:shape=tuple(packed['shape']);packed_masks=packed['masks']
        def get_mask(worm,frame):return np.unpackbits(packed_masks[frame],count=int(np.prod(shape))).reshape(shape).astype(bool)
        ids=[1]
    if apply_guards:
        guards=load_guards(data,ids,n,required=bool(config.get('quality_schema_version')))
        if guards is not None:
            for worm,records in guards.items():
                for record in records:
                    key=(record['frame'],worm)
                    bad[key]=bad.get(key,False) or not record['mask_valid']
    return data,summary,ids,get_mask,bad


def load_guards(data, ids, frames, required=False):
    path = Path(data)/'quality-guards.json'
    if not path.is_file():
        if required: raise ValueError('缺少统一质量检查，请先运行 quality 阶段')
        return None
    report = json.loads(path.read_text(encoding='utf-8'))
    if (report.get('quality_schema_version') != VERSION or report.get('guard_version') not in (GUARD_VERSION,'quality-guards-v6-single-body')
            or report.get('frames') != frames or sorted(report.get('worm_ids', [])) != sorted(ids)):
        raise ValueError('质量检查版本或输入帧数不匹配，请重新运行 quality 阶段')
    result = {w: report['worms'][str(w)] for w in ids}
    if any(len(rows) != frames or any(r['frame'] != f or r['worm_id'] != w for f,r in enumerate(rows))
           for w,rows in result.items()):
        raise ValueError('质量记录缺帧或虫编号不匹配')
    return result


def tracking_directory(base, config):
    raw = base / 'sam2-tracking'
    recovered = raw / RECOVERY_FOLDER
    if config.get('centerline_method') == 'self_contact' and (recovered / 'summary.json').is_file():
        return recovered
    return raw

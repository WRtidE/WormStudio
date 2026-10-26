"""Preserve completed runs when reviewed inputs change."""
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
from datetime import datetime
import secrets


def fresh_sample(name, outputs):
    stem = re.sub(r'[^\w.-]', '-', name).strip('.-') or 'video'
    stem = stem[:50]
    while True:
        sample = f'{stem}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}'
        if not (outputs / sample).exists():
            return sample


def input_identity(config, phenotype, motion_path):
    stat = Path(config['video']).stat()
    identity = dict(config=config, video_size=stat.st_size, video_mtime_ns=stat.st_mtime_ns,
                    phenotype=phenotype.read_text(encoding='utf-8'),
                    motion_constraints=json.loads(motion_path.read_text(encoding='utf-8')))
    masks = {w['mask_path']: hashlib.sha256(Path(w['mask_path']).read_bytes()).hexdigest()
             for g in config['groups'] for a in [dict(worms=g['worms'])]+g.get('additional_anchors', [])
             for w in a['worms'] if 'mask_path' in w}
    if masks: identity['manual_masks'] = masks
    return identity


def preserve_run(config, outputs):
    """Copy current seeds to a fresh run; never move or overwrite old results."""
    config = copy.deepcopy(config)
    old_base = outputs / config['sample']
    config['sample'] = fresh_sample(config['sample'], outputs)
    base = outputs / config['sample']
    target = base / 'manual-initialization'
    target.mkdir(parents=True)
    for g in config['groups']:
        for anchor in [dict(frame=g['anchor_frame'], worms=g['worms'])]+g.get('additional_anchors', []):
            for worm in anchor['worms']:
                if 'mask_path' in worm:
                    path = target / f'{g["name"]}-frame-{anchor["frame"]}-worm-{worm["id"]}.png'
                    shutil.copy2(worm['mask_path'], path)
                    worm['mask_path'] = str(path.resolve())
    original = old_base / 'manual-initialization/annotation.json'
    if original.is_file():
        record = json.loads(original.read_text(encoding='utf-8'))
        record['config'] = config
        (target/'annotation.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return config

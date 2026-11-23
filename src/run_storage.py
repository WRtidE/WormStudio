"""Preserve completed runs when reviewed inputs change."""
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
from datetime import datetime
import secrets
import os
from tempfile import NamedTemporaryFile


def replace_bytes(path, content):
    """Replace existing files without changing any linked historical snapshot."""
    path = Path(path)
    # New tracking frames cannot be shared yet. Keep their original single-write
    # path; replacement is needed only when an existing file could be linked.
    try:
        with path.open('xb') as handle:
            handle.write(content)
        return
    except FileExistsError:
        pass
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.'+path.name+'.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def copy_tracking_snapshot(source, destination):
    """Share mask bytes; keep metadata and independently rewritten files separate.

    Tracking/cleanup replace PNG files atomically via write_png, so edits in
    either run detach that file without changing any other snapshot. Deleting
    the original run also leaves the derived run intact. Volumes without hard
    links fall back to ordinary copies.
    """
    source = Path(source)

    def copy_file(original, target):
        original = Path(original)
        relative = original.relative_to(source)
        if relative.parts[0] == 'masks' and original.suffix.lower() == '.png' and not original.is_symlink():
            try:
                os.link(original, target)
                return str(target)
            except OSError:
                pass
        return shutil.copy2(original, target)

    return shutil.copytree(source, destination, copy_function=copy_file,
                           ignore=shutil.ignore_patterns('frames-*', '.crop-replay-*'))


def fresh_sample(name, outputs):
    stem = re.sub(r'[^\w.-]', '-', name).strip('.-') or 'video'
    if (outputs / stem).is_dir():
        # Older revisions could truncate a preceding hex suffix before appending another.
        stem = re.sub(r'(?:-\d{8}-\d{6}-[0-9a-f]{1,4})+$', '', stem) or 'video'
    stem = stem[:50]
    while True:
        sample = f'{stem}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}'
        if not (outputs / sample).exists():
            return sample


def video_path(folder, preferred, compact):
    """Keep normal download names and find compact exports after a workspace move."""
    normal = folder / preferred
    short = folder / compact
    if short.is_file():
        return short
    if len(str(normal.absolute()).encode('utf-16-le')) // 2 < 260:
        return normal
    try:
        if normal.is_file():  # Existing export on a long-path-capable filesystem.
            return normal
    except OSError:
        pass
    return short


def annotated_video_path(folder, sample, *, clean=False):
    suffix = '_clean' if clean else ''
    return video_path(folder, f'{sample}_annotated_full-frame{suffix}.mp4', f'annotated{suffix}.mp4')


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


def result_video_files(base, config):
    from centerline_methods import method_source
    folder = base / ('phenotypes-' + method_source(config['centerline_method']))
    original = annotated_video_path(folder, config['sample'])
    clean = annotated_video_path(folder, config['sample'], clean=True)
    video = original
    if clean.is_file() and (not original.is_file() or clean.stat().st_mtime_ns >= original.stat().st_mtime_ns):
        video = clean
    return [('video', '标注视频', video, 'export'),
            ('source-video', '原始视频（只读）', Path(config['video']), 'metrics')]


def saved_videos(base, config):
    stages = json.loads((base / 'pipeline-state.json').read_text(encoding='utf-8')).get('stages', {})
    return {key: (label, path) for key, label, path, stage in result_video_files(base, config)
            if stages.get(stage, {}).get('status') in ('complete', 'reused') and path.is_file()}

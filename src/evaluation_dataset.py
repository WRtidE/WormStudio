"""Read-only source inventory, conservative group splits and reproducible tasks.

Dataset snapshots are append-only directories. Existing experiment data is never
an output of this module. Folder names are hints, not verified experimental IDs.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
import zipfile

VERSION = 'wormstudio-dataset-v1'
VIDEO_SUFFIXES = {'.mp4', '.avi', '.mov', '.mkv', '.m4v', '.wmv', '.webm'}
SKIP = {'node_modules', '.git', '__pycache__', 'vendor', 'deps', 'site-packages',
        'masks', 'frames', 'jpeg-frames', 'before', 'evaluation_data', 'models', 'checkpoints',
        '.venv', '.venv-twardis', '.uv-python', '.uv-cache', '.hf-cache', '.micro-sam-cache'}
REFERENCE_NAMES = {'source.json', 'metadata.json', 'manifest.json', 'inventory.json', 'annotation.json',
                   'batch-configs.json', 'seed_configs.json'}
REQUIRED_METADATA = ('acquisition_id', 'batch_id', 'pool_ids', 'individual_ids', 'device_id')


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_new(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists(): raise FileExistsError(str(path))
    # Publish only a complete file; readers never see a partially saved revision.
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,suffix='.tmp',delete=False) as handle:
            temporary=Path(handle.name);handle.write(content);handle.flush();os.fsync(handle.fileno())
        if os.name == 'nt': os.rename(temporary,path)  # Windows rename refuses an existing destination.
        else: os.link(temporary,path)  # Atomic exclusive publication on POSIX.
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def walk_files(root):
    root = Path(root)
    if root.is_file():
        yield root
        return
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.'))
        for name in sorted(names):
            yield Path(folder) / name


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def reference_index(roots):
    by_path, by_hash = defaultdict(list), defaultdict(list)
    missing, inspected, errors = [], [], []
    seen = set()
    for root in roots:
        for path in walk_files(root):
            if path.suffix.lower() != '.json' or not (path.name in REFERENCE_NAMES or 'config' in path.name):
                continue
            path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            if path.stat().st_size > 8 * 1024 * 1024:
                errors.append(dict(path=str(path), reason='metadata_larger_than_8_MiB_not_parsed'))
                continue
            try:
                data = read_json(path)
            except (OSError, ValueError) as exc:
                errors.append(dict(path=str(path), reason=str(exc)))
                continue
            inspected.append(dict(path=str(path), sha256=sha256(path)))
            for record in objects(data):
                refs = []
                for key in ('video', 'source_video', 'video_path', 'path', 'original_video'):
                    value = record.get(key)
                    if not isinstance(value, str) or Path(value).suffix.lower() not in VIDEO_SUFFIXES:
                        continue
                    p = Path(value)
                    if not p.is_absolute():
                        p = path.parent / p
                    p = p.resolve()
                    if p.is_file():
                        refs.append((key, p))
                    else:
                        missing.append(dict(record=str(path), field=key, reference=value))
                source_hash = record.get('source_video_sha256')
                # An inventory entry alone proves observation, not algorithm use.
                used = path.name not in {'inventory.json', 'manifest.json'} or any(
                    k in record for k in ('tracks_dir', 'baseline', 'anchor_frames', 'tracking_roi'))
                if record.get('decision') == 'include' or '/examples/v4/' in path.as_posix():
                    used = True
                evidence = dict(record=str(path), sample=record.get('sample'),
                    usage='development_run' if used else 'inventory_observation',
                    crop_xyxy=record.get('crop_xyxy'), scale_um_per_px=record.get('scale_um_per_px'),
                    scale_source=record.get('scale_source'), source_kind=record.get('source_kind'),
                    group=record.get('group'), note=record.get('note'),
                    target_worm_count=sum(len(g.get('worms', [])) for g in record.get('groups', []) if isinstance(g, dict)))
                for _, p in refs:
                    by_path[str(p)].append(evidence)
                if isinstance(source_hash, str) and re.fullmatch('[0-9a-f]{64}', source_hash):
                    by_hash[source_hash].append(evidence)
    return by_path, by_hash, dict(inspected=inspected, unresolved_references=missing, parse_issues=errors)


def video_metadata(path):
    import cv2
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return dict(readable=False, error='video_open_failed')
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        ok, _ = cap.read()
        fps = fps if math.isfinite(fps) and fps > 0 else None
        return dict(readable=bool(ok), width=width, height=height, frame_count=n,
                    fps_reported=fps, duration_seconds_nominal=n / fps if fps else None,
                    timebase_status='reported_fps_only_pts_not_verified',
                    frame_count_status='container_reported_not_full_decode')
    finally:
        cap.release()


def inventory(video_roots, record_roots, output):
    output = Path(output)
    if output.exists():
        raise ValueError('盘点输出目录已存在；请使用新目录，保留已冻结记录')
    refs, hashes, audit = reference_index(record_roots)
    paths = {p.resolve() for root in video_roots for p in walk_files(root) if p.suffix.lower() in VIDEO_SUFFIXES}
    # Only referenced source files outside generated results are added implicitly.
    for name in refs:
        p = Path(name)
        if not {'validation', 'outputs', 'tests'} & set(p.parts) and p.is_file():
            paths.add(p)
    grouped = {}
    failures = []
    for index, path in enumerate(sorted(paths, key=str)):
        try:
            stat = path.stat(); fingerprint = sha256(path)
            after = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('source_changed_during_hash')
            row = grouped.setdefault(fingerprint, dict(video_id='vid-' + fingerprint[:16], sha256=fingerprint,
                aliases=[], evidence=[], folder_hints=[], metadata=video_metadata(path), experimental_metadata={},
                lineage=dict(parent_video_ids=[], status='needs_human_confirmation')))
            row['aliases'].append(dict(path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))
            row['evidence'].extend(refs.get(str(path), []))
            parts = path.parts
            row['folder_hints'].append(dict(path=str(path),
                worm_count=next((int(p[:-1]) for p in parts if re.fullmatch(r'\d+条', p)), None),
                developmental_stage=next((p for p in parts if p in ('成虫前', '成虫后')), None),
                status='directory_label_only_not_independent_annotation'))
            if index % 10 == 0:
                print(f'INVENTORY {index + 1}/{len(paths)} {path.name}', flush=True)
        except (OSError, ValueError) as exc:
            failures.append(dict(path=str(path), error=str(exc)))
    videos = []
    for fingerprint, row in sorted(grouped.items()):
        row['evidence'].extend(hashes.get(fingerprint, []))
        row['evidence'] = list({digest(e): e for e in row['evidence']}.values())
        row['development_exposure'] = 'observed' if any(e['usage'] == 'development_run' for e in row['evidence']) else 'unknown'
        row['missing_fields'] = list(REQUIRED_METADATA) + ['lineage_confirmation', 'development_exposure_confirmation', 'physical_calibration_confirmation']
        row['primary_path'] = row['aliases'][0]['path']
        videos.append(row)
    manifest = dict(schema_version=VERSION, created_at=now(), video_roots=[str(Path(p).resolve()) for p in video_roots],
        record_roots=[str(Path(p).resolve()) for p in record_roots], videos=videos,
        summary=dict(files=len(paths), unique_videos=len(videos), exact_duplicate_copies=sum(len(r['aliases'])-1 for r in videos),
            development_observed=sum(r['development_exposure'] == 'observed' for r in videos), unreadable_or_missing=failures),
        scope='Source bytes and existing metadata only; no inference, thresholds, or labels changed.')
    output.mkdir(parents=True)
    write_new(output / 'manifest.json', manifest)
    write_new(output / 'inventory-audit.json', audit)
    write_new(output / 'metadata-overrides.template.json', dict(schema_version=VERSION, videos={r['video_id']: dict(
        acquisition_id=None, batch_id=None, pool_ids=[], individual_ids=[], device_id=None,
        developmental_stage=None, worm_count=None, physical_calibration=None, parent_video_ids=[],
        lineage_confirmed=False, development_exposure='unknown', reviewer=None, evidence=None, reviewed_at=None) for r in videos}))
    export_manifest_csv(manifest, output / 'manifest.csv')
    return manifest


def export_manifest_csv(manifest, path):
    fields = ['video_id', 'sha256', 'primary_path', 'copies', 'development_exposure', 'width', 'height', 'frame_count',
              'fps_reported', 'timebase_status', 'worm_count_hints', 'stage_hints', 'missing_fields']
    with Path(path).open('x', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for row in manifest['videos']:
            writer.writerow({**{k: row[k] for k in ('video_id', 'sha256', 'primary_path', 'development_exposure')},
                **{k: row['metadata'].get(k) for k in fields if k in row['metadata']}, 'copies':len(row['aliases']),
                'worm_count_hints':';'.join(map(str, sorted({h['worm_count'] for h in row['folder_hints'] if h['worm_count'] is not None}))),
                'stage_hints':';'.join(sorted({h['developmental_stage'] for h in row['folder_hints'] if h['developmental_stage']})),
                'missing_fields':';'.join(row['missing_fields'])})


def freeze_split(manifest, overrides=None, seed='wormstudio-stage2-20261019'):
    """Unknown grouping/exposure never qualifies a video as an independent test."""
    videos = {r['video_id']:r for r in manifest['videos']}
    overrides = (overrides or {}).get('videos', {})
    if set(overrides) - set(videos):
        raise ValueError('元数据包含不在 manifest 中的视频')
    parent = {key:key for key in videos}
    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]; key = parent[key]
        return key
    def union(a, b):
        if b not in parent:
            raise ValueError('同源关系指向未知视频')
        parent[find(b)] = find(a)
    shared = {}
    for key, row in videos.items():
        meta = overrides.get(key, {})
        for field in ('pool_ids', 'individual_ids', 'parent_video_ids'):
            if field in meta and (not isinstance(meta[field], list) or any(not isinstance(x, str) or not x.strip() for x in meta[field])):
                raise ValueError(f'{field} 必须为非空字符串列表')
        for field in ('acquisition_id', 'batch_id', 'device_id'):
            if meta.get(field) is not None and not isinstance(meta[field], str):
                raise ValueError(f'{field} 必须为字符串或 null')
        # Batch is the most conservative grouping unit; device alone is not a batch.
        for field in ('acquisition_id', 'batch_id', 'pool_ids', 'individual_ids'):
            values = meta.get(field) or []
            values = values if isinstance(values, list) else [values]
            for value in values:
                token = (field, value)
                if token in shared:
                    union(key, shared[token])
                shared[token] = key
        for ancestor in meta.get('parent_video_ids', []) + row['lineage']['parent_video_ids']:
            union(key, ancestor)
        if meta.get('development_exposure', 'unknown') not in ('unknown', 'used', 'never_used'):
            raise ValueError('development_exposure 必须为 unknown/used/never_used')
    groups = defaultdict(list)
    for key in videos:
        groups[find(key)].append(key)
    assignments = []
    for members in sorted(groups.values(), key=lambda items:sorted(items)):
        members.sort(); reasons = []
        exposed = any(videos[k]['development_exposure'] == 'observed' or overrides.get(k, {}).get('development_exposure') == 'used' for k in members)
        for key in members:
            meta = overrides.get(key, {})
            missing = [f for f in REQUIRED_METADATA if not meta.get(f)]
            if missing: reasons.append(key + ': missing ' + ','.join(missing))
            if meta.get('lineage_confirmed') is not True: reasons.append(key + ': lineage_unconfirmed')
            if meta.get('development_exposure') != 'never_used': reasons.append(key + ': no_never_used_attestation')
            if not all(isinstance(meta.get(f), str) and meta[f].strip() for f in ('reviewer', 'evidence', 'reviewed_at')):
                reasons.append(key + ': no_metadata_review_evidence')
            else:
                try:
                    if datetime.fromisoformat(meta['reviewed_at']).tzinfo is None: raise ValueError()
                except ValueError:
                    raise ValueError('元数据复核时间必须包含时区')
            if not videos[key]['metadata'].get('readable'): reasons.append(key + ': unreadable_video')
        group_id = 'grp-' + digest(members)[:16]
        if exposed:
            split, reason = 'development', 'historical_use_or_related_to_development'
        elif reasons:
            split, reason = 'pending', 'independence_not_confirmed'
        else:
            bucket = int(hashlib.sha256((seed + group_id).encode()).hexdigest()[:8], 16) / 2**32
            split = 'development' if bucket < .6 else 'validation' if bucket < .8 else 'test'
            reason = 'deterministic_group_hash_60_20_20'
        assignments.extend(dict(video_id=k, group_id=group_id, split=split, reason=reason, pending_checks=reasons) for k in members)
    return dict(schema_version=VERSION, created_at=now(), manifest_sha256=digest(manifest), overrides_sha256=digest(overrides),
        seed=seed, policy='Related acquisitions, batches, pools and individuals stay together; observed development use dominates.',
        assignments=assignments, counts=dict(Counter(r['split'] for r in assignments)),
        readiness='ready' if all(r['split'] != 'pending' for r in assignments) and any(r['split']=='test' for r in assignments) else 'independent_evaluation_pending_metadata_or_data')


def build_tasks(manifest, splits, plan):
    if splits['manifest_sha256'] != digest(manifest):
        raise ValueError('划分与 manifest 不匹配')
    videos = {r['video_id']:r for r in manifest['videos']}
    assignments = {r['video_id']:r for r in splits['assignments']}
    if set(assignments) != set(videos): raise ValueError('划分记录不完整')
    ids = plan['video_ids']
    if not ids or len(ids) != len(set(ids)): raise ValueError('试点视频列表为空或重复')
    count = plan.get('random_frames_per_video', 24)
    if type(count) is not int or not 1 <= count <= 1000: raise ValueError('随机帧数量无效')
    tasks = []
    def task(video_id, stratum, frames, event=None):
        n = videos[video_id]['metadata']['frame_count']
        if not frames or any(type(f) is not int or not 0 <= f < n for f in frames): raise ValueError('任务帧号越界')
        payload = dict(video_id=video_id, stratum=stratum, frames=frames, event=event)
        tasks.append(dict(task_id='task-' + digest(payload)[:16], **payload,
            roles=['annotator_a', 'annotator_b', 'adjudicator'], source_sha256=videos[video_id]['sha256'],
            coordinate_system='full_source_image_xy_pixels_zero_based', truth_status='unannotated'))
    for video_id in ids:
        if video_id not in videos or assignments[video_id]['split'] not in ('development', 'validation'):
            raise ValueError('试点只接受已划分的开发／验证视频，不自动打开待划分或最终留出视频')
        row = videos[video_id]
        if not row['metadata'].get('readable') or row['metadata'].get('frame_count',0) <= 0: raise ValueError('视频不可解码')
        rng = random.Random(str(plan.get('seed', 20261019)) + video_id)
        for frame in sorted(rng.sample(range(row['metadata']['frame_count']), min(count, row['metadata']['frame_count']))):
            task(video_id, 'random', [frame])
    for event in plan.get('events', []):
        video_id = event['video_id']
        if video_id not in ids: raise ValueError('事件必须属于试点视频')
        start, end = event['start_frame'], event['end_frame']
        before, after = event['context_before'], event['context_after']
        if any(type(n) is not int for n in (start,end,before,after)) or not 0 <= before <= start <= end <= after < videos[video_id]['metadata']['frame_count']:
            raise ValueError('事件及前后上下文范围无效')
        if not event.get('evidence') or event.get('status') not in ('proposal_needs_confirmation', 'human_confirmed'):
            raise ValueError('事件需记录来源与确认状态')
        task(video_id, 'event', list(range(before, after+1)), event)
    return dict(schema_version=VERSION, created_at=now(), manifest_sha256=digest(manifest), split_sha256=digest(splits),
        plan_sha256=digest(plan), tasks=tasks, summary=dict(tasks=len(tasks), random_tasks=sum(t['stratum']=='random' for t in tasks),
            event_tasks=sum(t['stratum']=='event' for t in tasks), frame_presentations=sum(len(t['frames']) for t in tasks),
            independent_annotation_passes=2), protocol='Random and event strata reported separately; predictions never used as truth.')


def snapshot_baseline(studio, output):
    studio = Path(studio)
    paths = [studio/name for name in ('worm_pipeline.py','studio.py','evaluation.py','requirements.txt')]
    paths += [p for folder in ('src', 'web', 'config') for p in (studio/folder).glob('*') if p.is_file() and p.suffix in ('.py','.js','.css','.html','.json','.txt')]
    archive=Path(output).with_name('baseline-source.zip')
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(set(paths)): handle.write(path,path.relative_to(studio))
    record = dict(created_at=now(), thresholds_changed=False, files=[dict(path=str(p.relative_to(studio)), sha256=sha256(p)) for p in sorted(set(paths))],
        source_archive=dict(path=archive.name,sha256=sha256(archive)),
        models='See docs/MODELS.md and model manifests; weights not copied or rehashed by this snapshot. No inference or weights changed in this stage.')
    write_new(output, record)
    return record

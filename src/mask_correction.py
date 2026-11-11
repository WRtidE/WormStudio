"""Turn a displayed source frame into a new, non-destructive correction run."""
import base64
import copy
import json
from pathlib import Path

import cv2
import numpy as np


def request_edit(progress, root, run, sequence):
    from manual_worm_annotation import Annotation, png
    from live_preview import latest_sequence
    from run_storage import fresh_sample
    from head_tail import normalize_anchors

    with progress.lock:
        current = getattr(progress.server, 'annotation', None)
        # A retry after a lost response must reopen the same editor, not stop a new run.
        if (current and not current.done.is_set() and
                getattr(current, 'correction_request', None) == (run, sequence) and
                progress.data['status'] == 'preparing'):
            return dict(annotation_url='/', frame=current.frame)
        if progress.data.get('read_only') or not progress.config:
            raise ValueError('当前会话不能修改掩膜')
        if not progress.preview_dir or run != progress.run_id:
            raise ValueError('预览任务已切换，请重新暂停后选择画面')
        if type(sequence) is not int or not 1 <= sequence <= latest_sequence(progress.preview_dir):
            raise ValueError('请先暂停在已显示的处理帧')
        item = json.loads((progress.preview_dir / f'{sequence:09d}.json').read_text(encoding='utf-8'))
        frame = item['frame']
        if type(frame) is not int or not 0 <= frame < progress.data['frames']:
            raise ValueError('预览源帧无效')
        if progress.next_ready.is_set() or progress.data['status'] not in ('running', 'complete', 'failed', 'cancelled'):
            raise ValueError('当前任务正在切换，请稍后重试')
        source = copy.deepcopy(progress.config)
        paths = {worm['id']: root / 'outputs' / source['sample'] / 'sam2-tracking' / 'masks' /
                 f"worm-{worm['id']}" / f'{frame:06d}.png'
                 for group in source['groups'] for worm in group['worms']}
        paths = {worm: path for worm, path in paths.items() if path.is_file()}
        if not paths:
            raise ValueError('这一帧还没有已保存的掩膜，请选择已完成跟踪的帧')
        if progress.data['status'] == 'running':
            # The runner marks a terminal status only after the child process exits.
            # Never read masks while the writer is still running or load a second GPU model.
            progress.cancel_requested.set()
            return dict(waiting=True, frame=frame)

        settings = dict(source, sample=fresh_sample(source['sample'], root / 'outputs'),
                        previous_sample=source['sample'])
        settings.pop('identity_confirmations', None)
        candidate = Annotation(settings, frame=frame)
        h, w = candidate.image.shape[:2]
        worms = []
        for worm, path in paths.items():
            mask = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
            if mask is None or mask.shape != (h, w):
                raise ValueError(f'线虫 {worm} 的掩膜文件不完整，请选择其他帧')
            rgba = np.full((h, w, 4), 255, np.uint8)
            rgba[:, :, 3] = (mask > 127).astype(np.uint8) * 255
            heads = [a for a in normalize_anchors(source.get('head_annotations', {}).get(str(worm))) if a['frame'] == frame]
            worms.append(dict(id=worm, mask='data:image/png;base64,' + base64.b64encode(png(rgba)).decode('ascii'),
                              area_px=int((mask > 127).sum()), confirmed=False,
                              head_point=heads[0]['point'] if len(heads) == 1 else None))
        candidate.correction_request = (run, sequence)
        candidate.identity['mask_correction'] = dict(source_sample=source['sample'], run=run, sequence=sequence)
        candidate.correction_source = source
        candidate.correction_previous = dict(worms=worms, roi=[0, 0, w, h],
            measurement_mode=source.get('measurement_mode', 'physical'), scale_um_per_px=source['scale_um_per_px'],
            calibration=None, centerline_method=source['centerline_method'], head_detection=source.get('head_detection', 'twardis'))
        progress.cancel_requested.clear()
        progress.queue_video(candidate)
        return dict(annotation_url='/', frame=frame)


def validate_edit(annotation, absolute, masks):
    source = annotation.correction_source
    if absolute != source['crop_xyxy']:
        raise ValueError('修正掩膜时请保持原分析区域')
    expected = {w['id'] for w in annotation.correction_previous['worms']}
    if set(masks) != expected:
        raise ValueError('修正时请保留已有线虫编号；新增或删除线虫请重新标注视频')
    cx, cy, _, _ = absolute
    for group in source['groups']:
        x, y, x1, y1 = group['crop_xyxy']
        for worm in group['worms']:
            mask = masks.get(worm['id'])
            if mask is not None and mask[y-cy:y1-cy, x-cx:x1-cx].sum() != mask.sum():
                raise ValueError(f"线虫 {worm['id']} 的修正超出了原跟踪区域")


def merge_edit(annotation, config, masks):
    """Keep other groups and anchors; add/replace only this frame's reviewed masks."""
    from manual_worm_annotation import png
    from head_tail import normalize_anchors
    source = annotation.correction_source
    groups = copy.deepcopy(source['groups'])
    cx, cy, _, _ = source['crop_xyxy']
    for group in groups:
        x, y, x1, y1 = group['crop_xyxy']
        for worm in group['worms']:
            worm_id = worm['id']
            if worm_id not in masks:
                continue
            path = annotation.output / f'correction-worm-{worm_id}.png'
            path.write_bytes(png(masks[worm_id][y-cy:y1-cy, x-cx:x1-cx].astype(np.uint8) * 255))
            replacement = dict(id=worm_id, mask_path=str(path.resolve()))
            if group['anchor_frame'] == annotation.frame:
                worm.clear(); worm.update(replacement)
            else:
                anchors = group.setdefault('additional_anchors', [])
                target = next((a for a in anchors if a['frame'] == annotation.frame), None)
                if target is None:
                    target = dict(frame=annotation.frame, worms=[]); anchors.append(target)
                target['worms'] = [w for w in target['worms'] if w['id'] != worm_id] + [replacement]
    heads = copy.deepcopy(source.get('head_annotations', {}))
    for worm_id in masks:
        key = str(worm_id)
        values = [a for a in normalize_anchors(heads.get(key)) if a['frame'] != annotation.frame]
        values.extend(normalize_anchors(config['head_annotations'].get(key)))
        if values:
            heads[key] = values
        else:
            heads.pop(key, None)
    config.update(groups=groups, head_annotations=heads,
        mask_correction=dict(source_sample=source['sample'], frame=annotation.frame, worm_ids=sorted(masks)))
    config.pop('identity_confirmations', None)

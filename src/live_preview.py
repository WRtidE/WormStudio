"""Opt-in, ordered preview frames. Preview failures never interrupt inference."""
import json
import os
from pathlib import Path


def enabled():
    return bool(os.environ.get('WORM_PREVIEW_DIR'))


def latest_sequence(folder):
    try:
        return int(json.loads((Path(folder) / 'latest.json').read_text(encoding='utf-8'))['sequence'])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def report_preview(image, frame, total, detail=''):
    """Commit a bounded JPEG then metadata, then publish its sequence number.

    Pipeline stages run serially. No frame throttle: a slow viewer reads the
    ordered disk queue without blocking inference or retaining images in RAM.
    """
    target = os.environ.get('WORM_PREVIEW_DIR')
    if not target:
        return False
    try:
        import cv2
        folder = Path(target)
        folder.mkdir(parents=True, exist_ok=True)
        height, width = image.shape[:2]
        scale = min(1., 960 / max(height, width))
        if scale < 1:
            image = cv2.resize(image, (max(1, round(width*scale)), max(1, round(height*scale))))
        ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return False
        sequence = latest_sequence(folder) + 1
        name = f'{sequence:09d}'
        temporary = folder / (name + '.tmp')
        temporary.write_bytes(encoded.tobytes())
        temporary.replace(folder / (name + '.jpg'))
        item = dict(sequence=sequence, frame=int(frame), total=int(total),
                    stage=os.environ.get('WORM_PREVIEW_STAGE', ''), detail=str(detail))
        temporary.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
        temporary.replace(folder / (name + '.json'))
        temporary = folder / 'latest.tmp'
        temporary.write_text(json.dumps(dict(sequence=sequence)), encoding='utf-8')
        temporary.replace(folder / 'latest.json')
        return True
    except Exception:
        return False


def report_geometry(mask, line, frame, total, detail='', image=None, accepted=False):
    """Render crop coordinates; candidates remain visibly different from accepted lines."""
    if not enabled():
        return False
    try:
        import cv2
        import numpy as np
        canvas = np.full((*mask.shape, 3), 38, np.uint8) if image is None else image.copy()
        foreground = np.asarray(mask, bool)
        canvas[foreground] = (.55*canvas[foreground] + .45*np.array([112, 186, 109])).astype(np.uint8)
        contours, _ = cv2.findContours(foreground.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(canvas, contours, -1, (112, 210, 109), 1)
        if line is not None:
            points = np.asarray(line)
            color = (245, 245, 245) if accepted else (60, 190, 255)
            for a, b in zip(points[:-1], points[1:]):
                if np.isfinite([a, b]).all():
                    cv2.line(canvas, tuple(np.rint(a).astype(int)), tuple(np.rint(b).astype(int)), color, 2, cv2.LINE_AA)
        return report_preview(canvas, frame, total, detail)
    except Exception:
        return False


def preview_batch(folder, after, limit=12):
    if after < 0:
        raise ValueError('预览序号无效')
    count = latest_sequence(folder) if folder else 0
    items = []
    for sequence in range(after+1, min(count, after+limit)+1):
        item = json.loads((Path(folder) / f'{sequence:09d}.json').read_text(encoding='utf-8'))
        items.append(item)
    return dict(latest=count, frames=items)


def report_tracking(path, masks, frame, total, detail):
    if not enabled():
        return False
    try:
        import cv2
        import numpy as np
        from worm_colors import worm_color
        image = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        for worm, mask in masks.items():
            color = worm_color(worm)
            image[mask] = (.6*image[mask] + .4*np.asarray(color)).astype(np.uint8)
            contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(image, contours, -1, color, 2)
            yy, xx = np.nonzero(mask)
            if len(xx):
                cv2.putText(image, str(worm), (int(xx.mean()), int(yy.mean())), 0, .7, color, 2)
        return report_preview(image, frame, total, detail)
    except Exception:
        return False

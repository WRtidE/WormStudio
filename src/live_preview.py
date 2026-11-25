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


class PreviewCrop:
    """Read the user's analysis crop when a stage works in a smaller ROI."""
    def __init__(self, source):
        self.video = source['video']
        self.crop = source['crop_xyxy']
        self.capture = None
        self.next_frame = None

    def close(self):
        if self.capture is not None:
            self.capture.release()
            self.capture = None
            self.next_frame = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self, frame):
        import cv2
        if self.capture is None:
            self.capture = cv2.VideoCapture(str(self.video))
        if frame != self.next_frame:
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, image = self.capture.read()
        self.next_frame = frame + 1 if ok else None
        if not ok:
            raise ValueError('无法读取预览源帧')
        x, y, right, bottom = self.crop
        return image[y:bottom, x:right]


def discard_preview(folder):
    """Remove generated frames owned by a finished workspace session."""
    import re
    if folder is None or not folder.is_dir() or folder.is_symlink():
        return
    try:
        for item in folder.iterdir():
            if re.fullmatch(r'(?:\d{9,}(?:\.focus)?\.(?:jpg|json|tmp)|latest\.(?:json|tmp))', item.name):
                item.unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        pass  # Cleanup of optional previews must not prevent the next analysis.


def focus_patch(raw, masks):
    """Use a stable visible ID and keep a margin around its complete mask."""
    import cv2
    import numpy as np
    from worm_colors import worm_color
    height, width = raw.shape[:2]
    for worm, mask in masks:
        mask = np.asarray(mask, np.uint8)
        x, y, w, h = cv2.boundingRect(mask)
        if not w or not h:
            continue
        side = max(32, round(max(w, h) * 1.45))
        fw, fh = min(width, side), min(height, side)
        left = min(max(0, x + (w-fw)//2), width-fw)
        top = min(max(0, y + (h-fh)//2), height-fh)
        original = raw[top:top+fh, left:left+fw]
        outline = original.copy()
        contours, _ = cv2.findContours(mask[top:top+fh, left:left+fw], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(outline, contours, -1, worm_color(worm), 1, cv2.LINE_AA)
        if max(fw, fh) > 512:
            scale = 512 / max(fw, fh)
            size = max(1, round(fw*scale)), max(1, round(fh*scale))
            original = cv2.resize(original, size, interpolation=cv2.INTER_AREA)
            outline = cv2.resize(outline, size, interpolation=cv2.INTER_AREA)
        tiles = np.hstack((original, outline))
        return dict(worm=int(worm), rect=[left/width, top/height, fw/width, fh/height]), tiles
    return None


def report_preview(image, frame, total, detail='', *, raw=None, masks=()):
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
        if raw is not None:
            try:
                patch = focus_patch(raw, masks)
                if patch:
                    focus, tiles = patch
                    ok, encoded = cv2.imencode('.jpg', tiles, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    if ok:
                        temporary.write_bytes(encoded.tobytes())
                        temporary.replace(folder / (name + '.focus.jpg'))
                        item['focus'] = focus
            except Exception:
                pass  # The overview remains playable if the optional detail is unavailable.
        temporary.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
        temporary.replace(folder / (name + '.json'))
        temporary = folder / 'latest.tmp'
        temporary.write_text(json.dumps(dict(sequence=sequence)), encoding='utf-8')
        temporary.replace(folder / 'latest.json')
        return True
    except Exception:
        return False


def report_geometry(mask, line, frame, total, detail='', image=None, accepted=False, *, reader=None, offset=(0, 0), worm=1):
    """Render crop coordinates; candidates remain visibly different from accepted lines."""
    if not enabled():
        return False
    try:
        import cv2
        import numpy as np
        if reader is not None:
            image = reader.read(frame)
        canvas = np.full((*mask.shape, 3), 38, np.uint8) if image is None else image.copy()
        foreground = np.asarray(mask, bool)
        x, y = offset
        h, w = mask.shape
        region = canvas[y:y+h, x:x+w]
        region[foreground] = (.55*region[foreground] + .45*np.array([112, 186, 109])).astype(np.uint8)
        contours, _ = cv2.findContours(foreground.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(region, contours, -1, (112, 210, 109), 1)
        if line is not None:
            points = np.asarray(line)
            color = (245, 245, 245) if accepted else (60, 190, 255)
            for a, b in zip(points[:-1], points[1:]):
                if np.isfinite([a, b]).all():
                    cv2.line(region, tuple(np.rint(a).astype(int)), tuple(np.rint(b).astype(int)), color, 2, cv2.LINE_AA)
        if foreground.shape != canvas.shape[:2]:
            full_mask = np.zeros(canvas.shape[:2], bool)
            full_mask[y:y+h, x:x+w] = foreground
            foreground = full_mask
        return report_preview(canvas, frame, total, detail, raw=image, masks=[(worm, foreground)])
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


def report_tracking(path, masks, frame, total, detail, *, reader=None, offset=(0, 0), image=None):
    if not enabled():
        return False
    try:
        import cv2
        import numpy as np
        from worm_colors import worm_color
        if reader is None:
            raw = image if image is not None else cv2.imdecode(np.frombuffer(Path(path).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        else:
            raw = reader.read(frame)
            x, y = offset
            full_masks = {}
            for worm, mask in masks.items():
                h, w = mask.shape
                full = np.zeros(raw.shape[:2], bool)
                full[y:y+h, x:x+w] = mask
                full_masks[worm] = full
            masks = full_masks
        image = raw.copy()
        for worm, mask in masks.items():
            color = worm_color(worm)
            image[mask] = (.6*image[mask] + .4*np.asarray(color)).astype(np.uint8)
            contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(image, contours, -1, color, 2)
            yy, xx = np.nonzero(mask)
            if len(xx):
                cv2.putText(image, str(worm), (int(xx.mean()), int(yy.mean())), 0, .7, color, 2)
        return report_preview(image, frame, total, detail, raw=raw, masks=sorted(masks.items()))
    except Exception:
        return False

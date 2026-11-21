"""Render the selected chambers on one pass through their shared source video."""
import csv
import hashlib
import json
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from workspace_sessions import load_config
from run_storage import video_path
from video_timing import verify_encoded


def members_for_video(items, sample, *, partial=False, single=False):
    chosen = next((i for i in items if i['sample'] == sample and i['status'] == 'complete'), None)
    if not chosen or chosen.get('chamber_id') is None:
        raise ValueError('请选择已完成的培养槽')
    members = [chosen] if single else [
        i for i in items if i.get('chamber_id') is not None and Path(i['video_path']) == Path(chosen['video_path'])]
    missing = [i['chamber_id'] for i in members if i['status'] != 'complete']
    if missing and not partial:
        raise ValueError('以下培养槽尚未完成：' + '、'.join(map(str, missing)))
    ready = sorted((i for i in members if i['status'] == 'complete'), key=lambda i: i['chamber_id'])
    if len({i['chamber_id'] for i in ready}) != len(ready):
        raise ValueError('所选批次包含同一培养槽的多个结果，请选择对应的分析批次')
    return ready, missing


def chamber_layer(root, item, stack, video, frames, fps):
    import numpy as np
    from tracking_data import load_sample
    from centerline_methods import method_source
    from result_overlay import draw_worm
    from quality_schema import permissions

    sample = item['sample']
    base, config = load_config(root, sample)
    chamber = item['chamber_id']
    if Path(config['video']).resolve() != video or config.get('chamber_id', chamber) != chamber:
        raise ValueError(f'槽 {chamber} 的记录与所选原片或槽号不符')
    state = json.loads((base/'pipeline-state.json').read_text(encoding='utf-8'))
    identity = state.get('identity', {})
    stat = video.stat()
    if identity.get('video_size', stat.st_size) != stat.st_size or identity.get('video_mtime_ns', stat.st_mtime_ns) != stat.st_mtime_ns:
        raise ValueError('原片与分析时的文件不符，请恢复当时的原片')
    method = method_source(config['centerline_method'])
    folder = base / ('phenotypes-' + method)
    report = json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    mask_source = report.get('mask_source')
    if mask_source and not Path(mask_source).is_dir():
        # Workspaces can be restored under a different parent directory.
        mask_source = None
    _, tracking, ids, get_mask, _ = load_sample(sample, mask_source, apply_guards=False, root=root)
    box = tracking['crop_xyxy']
    if (Path(tracking['video']).resolve() != video or tracking['frames'] != frames or
            abs(tracking['fps'] - fps) > .001 or box != report.get('crop_xyxy', box) or len(ids) != 1):
        raise ValueError(f'槽 {chamber} 的帧、裁剪区域或虫体记录不匹配')
    rows = csv.DictReader(stack.enter_context((folder/'per_frame_metrics.csv').open(encoding='utf-8-sig', newline='')))
    lines = stack.enter_context(np.load(folder/'centerlines.npz', allow_pickle=False))
    x, y, right, bottom = box

    def draw(image, frame):
        row = next(rows, None)
        if not row or int(row['frame']) != frame or int(row['worm_id']) != ids[0]:
            raise ValueError(f'槽 {chamber} 第 {frame} 帧缺少对应指标')
        roi = image[y:bottom, x:right]
        mask = get_mask(ids[0], frame)
        if x < 0 or y < 0 or mask.shape != roi.shape[:2] or mask.shape != (bottom-y, right-x):
            raise ValueError(f'槽 {chamber} 的掩膜超出原片范围')
        # Draw into a view of the shared frame. Overlapping ROIs never paste background.
        points = lines[f'w{ids[0]}_f{frame}'] if permissions(row)['posture'] else None
        draw_worm(roi, mask, points, row, label=f'CH {chamber}')
    return draw


def render_video(root, members, target):
    import cv2
    import imageio_ffmpeg

    video = Path(members[0]['video_path']).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as scratch, ExitStack() as stack:
        cap = cv2.VideoCapture(str(video))
        stack.callback(cap.release)
        if not cap.isOpened():
            raise ValueError('无法打开分析所用的原片')
        w, h = int(cap.get(3)), int(cap.get(4))
        fps, frames = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        layers = [chamber_layer(root, item, stack, video, frames, fps) for item in members]
        output = Path(scratch)/'video.mp4'
        command = [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                   '-f', 'rawvideo', '-pixel_format', 'bgr24', '-video_size', f'{w}x{h}',
                   '-framerate', str(fps), '-i', 'pipe:0', '-i', str(video), '-map', '0:v', '-map', '1:a?',
                   '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-c:v', 'libx264', '-threads', '4',
                   '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                   '-movflags', '+faststart', str(output)]
        with (Path(scratch)/'encoder.log').open('w+b') as log:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log) as encoder:
                try:
                    for frame in range(frames):
                        ok, pixels = cap.read()
                        if not ok:
                            raise ValueError(f'原片第 {frame} 帧无法解码')
                        for draw in layers:
                            draw(pixels, frame)
                        encoder.stdin.write(memoryview(pixels))
                    if cap.read()[0]:
                        raise ValueError('原片帧数与分析记录不符')
                    encoder.stdin.close()
                    if encoder.wait():
                        raise BrokenPipeError()
                except BrokenPipeError as exc:
                    encoder.wait()
                    log.seek(0)
                    raise RuntimeError('视频编码失败 ' + log.read().decode(errors='replace')[-500:]) from exc
                except BaseException:
                    encoder.kill()
                    raise
        verify_encoded(output, dict(frames=frames, fps=fps), (w, h))
        output.replace(target)


class ChamberVideos:
    """Queue encodes away from the HTTP server; share a job across repeat clicks."""
    def __init__(self, root):
        self.root = root
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='chamber-video')
        self.lock = threading.Lock()
        self.jobs = {}
        self.full_jobs = {}

    def start(self, group, members, missing=(), *, full=False):
        video = Path(members[0]['video_path']).resolve()
        revision = [(i['chamber_id'], i['sample'], (self.root/'outputs'/i['sample']/'pipeline-state.json').stat().st_mtime_ns)
                    for i in members]
        key = hashlib.sha256(json.dumps([1, group, str(video), revision, sorted(missing)]).encode()).hexdigest()
        suffix = '部分槽标注' if missing else '全槽标注' if len(members)>1 else f'槽{members[0]["chamber_id"]}标注'
        path = video_path(self.root/'outputs'/'chamber-videos'/key, video.stem+'-'+suffix+'.mp4', suffix+'.mp4')
        with self.lock:
            existing = self.jobs.get(key)
            if path.is_file():
                self.jobs[key] = path, None
            elif not existing or existing[1] is None or existing[1].done():
                self.jobs[key] = path, self.pool.submit(render_video, self.root, members, path)
            if full and not missing:
                self.full_jobs[group, members[0]['video_path']] = key
        return key

    def summaries(self):
        with self.lock:
            jobs = self.full_jobs.copy()
        return [dict(group=group, video_path=video, **self.status(key))
                for (group, video), key in jobs.items()]

    def status(self, key):
        with self.lock:
            if key not in self.jobs:
                raise ValueError('未找到这次视频导出')
            path, future = self.jobs[key]
        if future and not future.done():
            return dict(status='running')
        if future and future.exception():
            return dict(status='failed', error=str(future.exception()))
        if not path.is_file():
            return dict(status='failed', error='视频已移动，请重新导出')
        return dict(status='complete', filename=path.name)

    def artifact(self, key):
        if self.status(key)['status'] == 'complete':
            return self.jobs[key][0]
        return None

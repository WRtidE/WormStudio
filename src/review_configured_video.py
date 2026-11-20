"""Export full-frame H.264 video and verify any configured worm sample."""
import argparse
from pipeline_progress import report_progress
from live_preview import report_preview
import csv
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np
from measurements.analyze_phenotypes import ROOT, load_sample, write_png
from worm_colors import worm_color
from quality.quality_schema import permissions, describe


def selected_frames(count, samples=9):
    if count < 1:
        raise ValueError('Video has no frames')
    return sorted(set(np.linspace(0, count - 1, min(count, samples)).astype(int)))


def preview(image, width=960):
    height = max(1, round(image.shape[0] * width / image.shape[1]))
    return cv2.resize(image, (width, height))


def verify(folder, target, source, ids, size, *, records=None):
    records = records or folder
    cap = cv2.VideoCapture(str(target))
    if not cap.isOpened():
        raise RuntimeError('Cannot open exported video')
    encoded_size = tuple(value + value % 2 for value in size)
    if (int(cap.get(3)), int(cap.get(4))) != encoded_size:
        raise RuntimeError('Exported resolution mismatch')
    if abs(cap.get(cv2.CAP_PROP_FPS) - source['fps']) > .001:
        raise RuntimeError('Exported frame rate mismatch')
    count = 0
    while cap.read()[0]:
        count += 1
        report_progress(count,source['frames'],'校验导出视频的帧数与可读性',fraction=.85+.14*count/source['frames'])
    cap.release()
    if count != source['frames']:
        raise RuntimeError('Exported frame count mismatch')
    expected = {(w, f) for w in ids for f in range(count)}
    with (records / 'per_frame_metrics.csv').open(encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))
    valid_keys={(int(r['worm_id']),int(r['frame'])) for r in rows if permissions(r)['posture']}
    from measurements.measurement_units import metric_key
    available_keys={(int(r['worm_id']),int(r['frame'])) for r in rows if r.get(metric_key('length_um',r)) and np.isfinite(float(r[metric_key('length_um',r)]))}
    with np.load(records / 'centerlines.npz', allow_pickle=False) as points:
        keys = {tuple(int(v[1:]) for v in key.split('_')) for key in points.files}
        if not valid_keys <= keys <= expected or keys != available_keys or any(points[k].shape != (101, 2) or not np.isfinite(points[k]).all() for k in points.files):
            raise RuntimeError('Missing or invalid centerline records')
    if len(rows) != len(expected) or {(int(r['worm_id']), int(r['frame'])) for r in rows} != expected:
        raise RuntimeError('Missing or duplicate metric records')
    checks = {}
    for w in ids:
        sub = [r for r in rows if int(r['worm_id']) == w]
        checks[w] = dict(mask_valid=sum(r['mask_valid'] == 'True' for r in sub),
                         posture_valid=sum(permissions(r)['posture'] for r in sub),
                         identity_valid=sum(r.get('identity_valid') == 'True' for r in sub))
    review_checks = []
    for interval in source.get('review_intervals', []):
        lo, hi = interval['frames']
        sub = [r for r in rows if int(r['worm_id']) in interval['worm_ids'] and lo <= int(r['frame']) <= hi]
        if len(sub) != (hi - lo + 1) * len(interval['worm_ids']) or not all(
                r['mask_valid'] == r['centerline_valid'] == r['valid_for_summary'] == 'False' for r in sub):
            raise RuntimeError('Review interval was not excluded from summaries')
        review_checks.append(dict(**interval, excluded_records=len(sub)))
    report = dict(quality_schema_version=describe(rows[0])['version'],frames=count, fps=source['fps'], source_resolution=size, full_resolution=encoded_size,
                  codec='H.264 / yuv420p', object_ids=ids, centerline_records=len(keys), metric_records=len(rows),
                  available_centerlines=len(keys), missing_centerlines=len(expected-keys), quality_counts=checks, review_intervals=review_checks, full_video=str(target))
    (folder / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', required=True)
    parser.add_argument('--tracking-only', action='store_true')
    parser.add_argument('--refresh-saved-video', action='store_true', help='Render a separate video from saved masks and measurements')
    parser.add_argument('--centerline-source',default='centerlines-body-sequence',
        choices=['wormtracer-temporal','centerlines-niu','centerlines-hybrid','centerlines-topology','centerlines-tapnext','centerlines-self-contact','centerlines-body-sequence'])
    args = parser.parse_args()
    base = ROOT / 'outputs' / args.sample
    _, source, ids, get_mask, _ = load_sample(args.sample)
    folder = base / ('phenotypes-'+args.centerline_source)
    filename = f'{args.sample}_annotated_full-frame.mp4'
    if args.refresh_saved_video:
        from measurements.analyze_phenotypes import render_saved
        from workspace_sessions import clean_video_path
        config = json.loads((base/'pipeline-config.json').read_text(encoding='utf-8'))
        identity = json.loads((base/'pipeline-state.json').read_text(encoding='utf-8')).get('identity') or {}
        if 'video_size' in identity and Path(source['video']).stat().st_size != identity['video_size']:
            raise ValueError('原视频大小与保存的记录不符。请恢复原视频或新建分析任务。')
        with tempfile.TemporaryDirectory(dir=folder, prefix='video-') as scratch:
            work = Path(scratch)
            render_saved(args.sample,config['centerline_method'],args.centerline_source,work)
            export_video(work, filename, source, ids, get_mask, records=folder)
            (work/filename).replace(clean_video_path(folder, args.sample))
        return
    tracking_review(base, source, ids, get_mask)
    if not args.tracking_only:
        export_video(folder, filename, source, ids, get_mask)


def tracking_review(base, source, ids, get_mask):
    raw = cv2.VideoCapture(source['video'])
    if not raw.isOpened():
        raise RuntimeError('Cannot open source video')
    x0, y0, x1, y1 = source['crop_xyxy']
    panels = []
    for f in selected_frames(source['frames']):
        raw.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, im = raw.read()
        if not ok:
            raise RuntimeError(f'Missing source frame {f}')
        im = im[y0:y1, x0:x1].copy()
        for w in ids:
            mask = get_mask(w, f)
            im[mask] = (.6 * im[mask] + .4 * np.array(worm_color(w))).astype(np.uint8)
            yy, xx = np.nonzero(mask)
            if len(xx):
                cv2.putText(im, str(w), (int(xx.mean()), int(yy.mean())), 0, 1.1, worm_color(w), 3)
        panel = preview(im)
        cv2.putText(panel, f'Frame {f} | IDs: {", ".join(map(str, ids))}', (10, 24), 0, .6, (0, 0, 255), 2)
        panels.append(panel)
    write_png(base / 'tracking-review.png', np.vstack(panels))
    raw.release()


def export_video(folder, filename, source, ids, get_mask, *, records=None):
    target = folder / filename
    x0, y0, x1, y1 = source['crop_xyxy']
    cropped = cv2.VideoCapture(str(folder / 'phenotypes-overlay.mp4'))
    raw = cv2.VideoCapture(source['video'])
    if not cropped.isOpened() or not raw.isOpened():
        raise RuntimeError('Source or phenotype video is missing')
    if int(raw.get(cv2.CAP_PROP_FRAME_COUNT)) != source['frames'] or abs(raw.get(cv2.CAP_PROP_FPS)-source['fps']) > .001:
        cropped.release()
        raw.release()
        raise ValueError('原视频的帧数或帧率与保存的标注不符')
    size = (int(raw.get(3)), int(raw.get(4)))
    encoded_size = tuple(value + value % 2 for value in size)
    previews = []
    samples = {0, source['frames'] // 2, source['frames'] - 1}
    with tempfile.TemporaryDirectory(prefix='worm-export-') as temporary:
        intermediate = Path(temporary) / 'full.mp4'
        encoded = Path(temporary) / 'encoded.mp4'
        writer = cv2.VideoWriter(str(intermediate), cv2.VideoWriter_fourcc(*'mp4v'), source['fps'], encoded_size)
        if not writer.isOpened():
            raise RuntimeError('Cannot encode full video')
        try:
            for f in range(source['frames']):
                report_progress(f,source['frames'],'合成完整画面视频',fraction=.55*f/source['frames'])
                ok, im = raw.read()
                ok2, overlay = cropped.read()
                crop_h,crop_w=y1-y0,x1-x0
                if not ok or not ok2 or overlay.shape[:2] != (crop_h+crop_h%2,crop_w+crop_w%2):
                    raise RuntimeError(f'Missing frame or crop mismatch at frame {f}')
                crop = overlay[:crop_h,:crop_w]
                report_preview(crop, f, source['frames'], '正在合成完整画面', raw=im[y0:y1, x0:x1],
                               masks=((worm, get_mask(worm, f)) for worm in ids))
                im[y0:y1, x0:x1] = crop
                if f in samples:
                    write_png(folder / f'full-preview-{f:04d}.png', im)
                    previews.append(preview(im))
                writer.write(cv2.copyMakeBorder(im, 0, size[1] % 2, 0, size[0] % 2, cv2.BORDER_CONSTANT))
        finally:
            writer.release()
            raw.release()
            cropped.release()
        report_progress(detail='正在编码 H.264 视频',fraction=.55,force=True)
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-y',
                        '-i', str(intermediate), '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                        '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(encoded)], check=True)
        shutil.copy2(encoded, target)
    write_png(folder / 'full-comparison.png', np.vstack(previews))
    print(json.dumps(verify(folder, target, source, ids, size, records=records), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

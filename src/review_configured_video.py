"""Export full-frame H.264 video and verify any configured worm sample."""
import argparse
from pipeline_progress import report_progress
import csv
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np
from analyze_phenotypes import ROOT, load_sample, write_png
from worm_colors import worm_color
from quality_schema import permissions, describe


def selected_frames(count, samples=9):
    if count < 1:
        raise ValueError('Video has no frames')
    return sorted(set(np.linspace(0, count - 1, min(count, samples)).astype(int)))


def preview(image, width=960):
    height = max(1, round(image.shape[0] * width / image.shape[1]))
    return cv2.resize(image, (width, height))


def verify(folder, target, source, ids, size):
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
    with (folder / 'per_frame_metrics.csv').open(encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))
    valid_keys={(int(r['worm_id']),int(r['frame'])) for r in rows if permissions(r)['posture']}
    from measurement_units import metric_key
    available_keys={(int(r['worm_id']),int(r['frame'])) for r in rows if r.get(metric_key('length_um',r)) and np.isfinite(float(r[metric_key('length_um',r)]))}
    with np.load(folder / 'centerlines.npz', allow_pickle=False) as points:
        keys = {tuple(int(v[1:]) for v in key.split('_')) for key in points.files}
        if not valid_keys <= keys <= expected or keys != available_keys or any(points[k].shape != (101, 2) or not np.isfinite(points[k]).all() for k in points.files):
            raise RuntimeError('Missing or invalid centerline records')
    with (folder / 'per_frame_metrics.csv').open(encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))
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
    parser.add_argument('--centerline-source',default='wormtracer-temporal',
        choices=['wormtracer-temporal','centerlines-niu','centerlines-hybrid','centerlines-topology','centerlines-tapnext'])
    args = parser.parse_args()
    base = ROOT / 'outputs' / args.sample
    _, source, ids, get_mask, _ = load_sample(args.sample)
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
    if args.tracking_only:
        return
    folder = base / ('phenotypes-'+args.centerline_source)
    target = folder / f'{args.sample}_annotated_full-frame.mp4'
    cropped = cv2.VideoCapture(str(folder / 'phenotypes-overlay.mp4'))
    raw = cv2.VideoCapture(source['video'])
    if not cropped.isOpened() or not raw.isOpened():
        raise RuntimeError('Source or phenotype video is missing')
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
                im[y0:y1, x0:x1] = overlay[:crop_h,:crop_w]
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
    print(json.dumps(verify(folder, target, source, ids, size), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

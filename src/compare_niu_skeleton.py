"""在同一批已有掩膜上，对比 NIU OSW25 骨架和当前 WormTracer 时序中心线。"""
from pathlib import Path

from resource_paths import PROJECT_ROOT as ROOT
# ==================== 常用设置：在这里修改 ====================
SAMPLES = ['5-1-910', '2-1-1314', '2-4-1314']
# None 自动选择该样本最新的接触修正版或时序版；也可直接填文件夹名。
CURRENT_SOURCE = None
OUTPUT_ROOT = ROOT / 'outputs' / 'niu-skeleton-comparison'
RANDOM_SEED = 42                # 原博客随机选 5 个骨架点；不将它们误连为身体路径
RANDOM_NODE_COUNT = 5
ORDERED_POINT_COUNT = 101       # 为姿态比较新增的有序等弧长采样，不是博客原步骤
SMOOTH_WINDOW = 7              # 仅平滑有序路径；原始骨架不变；0 表示不平滑
MAKE_VIDEOS = True
PANEL_WIDTH = 960              # 并排视频中每种方法的宽度，原始坐标不缩放保存
CENTERLINE_WIDTH_PX = 2         # 橘黄色中心线宽度（原来为 3 像素）
# 以下是比较用的统一几何筛查阈值，不是经人工真值标定的准确率阈值。
MIN_INSIDE = 0.97
MIN_SKELETON_COVERAGE = 0.90
MAX_TURN_DEGREES = 150
LENGTH_RANGE = (0.80, 1.20)
# ==================== 以下通常不需要修改 ====================

import argparse
import csv
import hashlib
import json
import math
import subprocess
import tempfile
import time

import cv2
import imageio_ffmpeg
import numpy as np
import scipy
import skimage
from scipy import ndimage as ndi
from scipy.signal import savgol_filter
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize

from analyze_phenotypes import load_sample, fitted_mask_source, resample, write_png, json_safe
from centerline_topology import geometry_quality
from worm_colors import worm_color

BLOG = 'https://neuroinformatics.dev/blog/worms_osw25.html'
REPOSITORY = 'https://github.com/jyoti-bhogal/neuroinformatics_osw/tree/main/python_code_skeletonisation_and_node_selection'
METHODS = ('niu_ordered', 'current')


def save_json(path, value):
    path.write_text(json.dumps(json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def write_csv(path, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows([{k: ('' if isinstance(v, (float, np.floating)) and not np.isfinite(v) else v)
                           for k, v in r.items()} for r in rows])


def skeleton_graph(sk):
    """8-neighbour graph; omit diagonal triangles with an orthogonal connection.

    This affects path ordering only, never the original skeleton pixels. Keeping
    such triangles would falsely label every staircase corner as a branch/cycle.
    """
    coords = np.argwhere(sk)
    lookup = np.full(sk.shape, -1, np.int32)
    lookup[sk] = np.arange(len(coords))
    rows, cols, weights = [], [], []
    for dy, dx in ((0, 1), (1, -1), (1, 0), (1, 1)):
        q = coords + [dy, dx]
        valid = (q[:, 0] >= 0) & (q[:, 0] < sk.shape[0]) & (q[:, 1] >= 0) & (q[:, 1] < sk.shape[1])
        a = np.flatnonzero(valid)
        b = lookup[q[valid, 0], q[valid, 1]]
        keep = b >= 0
        a, b = a[keep], b[keep]
        if dy and dx:
            y, x = coords[a].T
            keep = ~(sk[y + dy, x] | sk[y, x + dx])
            a, b = a[keep], b[keep]
        rows.extend(a); cols.extend(b); weights.extend([math.hypot(dx, dy)] * len(a))
        rows.extend(b); cols.extend(a); weights.extend([math.hypot(dx, dy)] * len(a))
    graph = coo_matrix((weights, (rows, cols)), shape=(len(coords), len(coords))).tocsr()
    return coords, graph


def extract_niu(mask):
    """Exact skeletonize + seeded random nodes, with a separately named adapter.

    The adapter selects the longest finite endpoint geodesic in the largest
    connected skeleton component. It cannot infer an occluded tip or decide
    traversal order through a self-contact loop. Such cases remain flagged.
    """
    ys, xs = np.nonzero(mask)
    empty = dict(raw_xy=np.empty((0, 2), np.int32), nodes_xy=np.empty((0, 2), np.int32),
                 ordered=None, topology='empty', endpoints=0, branch_pixels=0,
                 components=0, cycles=0, path_fraction=0., radius=1., centroid=[np.nan, np.nan])
    if not len(xs):
        return empty
    x0, y0 = max(0, int(xs.min()) - 3), max(0, int(ys.min()) - 3)
    local = np.ascontiguousarray(mask[y0:int(ys.max()) + 4, x0:int(xs.max()) + 4], dtype=bool)
    # No filling, erosion, dilation, component removal, or pruning before NIU.
    sk = skeletonize(local)
    coords, graph = skeleton_graph(sk)
    if not len(coords):
        return empty
    xy = coords[:, ::-1] + [x0, y0]
    rng = np.random.default_rng(RANDOM_SEED)
    nodes = xy[rng.choice(len(xy), size=min(RANDOM_NODE_COUNT, len(xy)), replace=False)]
    degree = np.diff(graph.indptr)
    count, labels = connected_components(graph, directed=False)
    ends = np.flatnonzero(degree == 1)
    cycles = graph.nnz // 2 - len(coords) + count
    simple = count == 1 and len(ends) == 2 and not np.any(degree > 2) and cycles == 0
    result = dict(raw_xy=xy, nodes_xy=nodes, ordered=None,
                  topology='simple' if simple else 'complex', endpoints=len(ends),
                  branch_pixels=int(np.sum(degree > 2)), components=count, cycles=cycles,
                  path_fraction=0., radius=float(np.median(ndi.distance_transform_edt(local)[sk])),
                  centroid=[float(xs.mean()), float(ys.mean())])
    largest = int(np.bincount(labels).argmax())
    ends = ends[labels[ends] == largest]
    if len(ends) < 2:
        return result
    # One source at a time keeps the memory bounded even for many noisy endpoints.
    best = (-1., None, None)
    for start in ends:
        distances = dijkstra(graph, indices=int(start))
        scores = distances[ends].copy()
        scores[~np.isfinite(scores)] = -1
        idx = int(scores.argmax())
        if scores[idx] > best[0]:
            best = (float(scores[idx]), int(start), int(ends[idx]))
    if best[0] <= 1:
        return result
    _, pred = dijkstra(graph, indices=best[1], return_predecessors=True)
    node, route = best[2], [best[2]]
    while node != best[1]:
        node = int(pred[node])
        if node < 0:
            raise RuntimeError('Unexpected disconnected skeleton route')
        route.append(node)
    p = resample(xy[route], ORDERED_POINT_COUNT)
    if SMOOTH_WINDOW >= 3:
        window = min(SMOOTH_WINDOW, len(p) if len(p) % 2 else len(p) - 1)
        if window % 2 == 0:
            window -= 1
        smooth = savgol_filter(p, window, min(2, window - 1), axis=0)
        smooth[[0, -1]] = p[[0, -1]]
        p = resample(smooth, ORDERED_POINT_COUNT)
    result.update(ordered=p, path_fraction=len(route) / len(coords))
    return result


def valid_line(p):
    return p is not None and np.asarray(p).shape == (ORDERED_POINT_COUNT, 2) and np.isfinite(p).all()


def line_metrics(p, mask, raw_xy, radius):
    if not valid_line(p):
        return dict(available=False, length_px=np.nan, inside_fraction=np.nan,
                    skeleton_coverage=np.nan, max_turn_deg=np.nan, geometric_pass=False)
    length = float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    # Dense interpolation makes coverage independent of 101-point sampling density.
    dense = resample(p, max(101, int(math.ceil(length)) + 1))
    inside = ndi.map_coordinates(mask.astype(np.uint8), [dense[:, 1], dense[:, 0]], order=0, mode='constant')
    coverage = float(np.mean(cKDTree(dense).query(raw_xy)[0] <= max(3., radius * .75))) if len(raw_xy) else 0.
    turn = geometry_quality(p)['max_turn_5pct_deg']
    return dict(available=True, length_px=length, inside_fraction=float(inside.mean()),
                skeleton_coverage=coverage, max_turn_deg=turn,
                geometric_pass=bool(inside.mean() >= MIN_INSIDE and coverage >= MIN_SKELETON_COVERAGE
                                    and turn <= MAX_TURN_DEGREES))


def align(p, previous):
    if valid_line(p) and valid_line(previous):
        if np.mean((p[::-1] - previous) ** 2) < np.mean((p - previous) ** 2):
            return p[::-1].copy()
    return p


def sequence_metrics(p, previous, center, previous_center, reference):
    if not valid_line(p) or not valid_line(previous):
        return dict(length_change_fraction=np.nan, endpoint_change_fraction=np.nan, shape_change_fraction=np.nan)
    current_length = np.linalg.norm(np.diff(p, axis=0), axis=1).sum()
    old_length = np.linalg.norm(np.diff(previous, axis=0), axis=1).sum()
    norm = reference if reference is not None else (old_length + current_length) / 2
    q = align(p, previous)
    delta = (q - center) - (previous - previous_center)
    return dict(length_change_fraction=abs(current_length - old_length) / max(norm, 1.),
                endpoint_change_fraction=float(np.linalg.norm(delta[[0, -1]], axis=1).max() / max(norm, 1.)),
                shape_change_fraction=float(np.mean(np.linalg.norm(delta, axis=1)) / max(norm, 1.)))


def source_for(sample, requested=None):
    base = ROOT / 'outputs' / sample
    current = requested or ('wormtracer-contact-corrected' if (base / 'wormtracer-contact-corrected' / 'mask-source.json').exists()
                            else 'wormtracer-temporal')
    manifest = fitted_mask_source(sample, current)
    # run.json also records the actual input masks when no shared manifest exists.
    if manifest is None:
        runs = sorted((base / current).glob('worm-*/run.json'))
        sources = {json.loads(p.read_text(encoding='utf-8')).get('mask_source') for p in runs}
        sources.discard(None)
        if len(sources) > 1:
            raise ValueError('Current worm fits do not share one mask source')
        manifest = next(iter(sources), None)
    data, source, ids, get_mask, bad = load_sample(sample, manifest)
    return current, data, source, ids, get_mask, bad


def source_digest(data):
    paths = sorted(p for p in data.rglob('*') if p.is_file() and
                   (p.suffix.lower() == '.png' and 'masks' in p.parts or p.name in ('masks-packed.npz', 'summary.json', 'tracking-quality.csv')))
    digest = hashlib.sha256()
    for p in paths:
        digest.update(p.relative_to(data).as_posix().encode('utf-8'))
        digest.update(p.read_bytes())
    return digest.hexdigest(), len(paths)


def fit_digest(base, current, ids):
    digest = hashlib.sha256()
    for w in ids:
        for name in ('centerlines.npy', 'quality.json', 'run.json'):
            path = base / current / f'worm-{w}' / name
            if path.exists():
                digest.update(path.read_bytes())
    return digest.hexdigest()


def stat(values, operation='median'):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return None
    if operation == 'p95':
        return float(np.percentile(values, 95))
    if operation == 'cv':
        return float(np.std(values) / np.mean(values)) if np.mean(values) else None
    return float(np.median(values))


def summarise(rows):
    out = []
    ids = sorted({r['worm_id'] for r in rows})
    for worm in ids + ['all']:
        for stratum in ('all', 'simple', 'complex'):
            selected = [r for r in rows if (worm == 'all' or r['worm_id'] == worm)
                        and (stratum == 'all' or r['topology'] == stratum)]
            if not selected:
                continue
            eligible = [r for r in selected if r['mask_eligible']]
            # A common frame set avoids making a method appear stable by dropping its failures.
            paired = [r for r in eligible if all(r[m + '_available'] for m in METHODS)]
            for method in METHODS:
                get = lambda subset, key: [r[method + '_' + key] for r in subset]
                out.append(dict(sample=selected[0]['sample'], worm_id=worm, stratum=stratum, method=method,
                                total_frames=len(selected), eligible_mask_frames=len(eligible),
                                excluded_mask_frames=len(selected) - len(eligible), paired_available_frames=len(paired),
                                available_frames=sum(get(eligible, 'available')),
                                common_pass_frames=sum(get(eligible, 'common_pass')),
                                median_inside=stat(get(paired, 'inside_fraction')),
                                median_skeleton_coverage=stat(get(paired, 'skeleton_coverage')),
                                paired_length_cv=stat(get(paired, 'length_px'), 'cv'),
                                paired_length_change_p95=stat(get(paired, 'length_change_fraction'), 'p95'),
                                paired_endpoint_change_p95=stat(get(paired, 'endpoint_change_fraction'), 'p95'),
                                paired_shape_change_p95=stat(get(paired, 'shape_change_fraction'), 'p95'),
                                length_jump_frames=sum(v > .20 for v in get(eligible, 'length_change_fraction') if np.isfinite(v))))
    return out


def label(canvas, text, xy, scale=.6, color=(255, 255, 255)):
    cv2.putText(canvas, text, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(canvas, text, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def draw_path(image, p, offset=(0, 0), width=2):
    if not valid_line(p):
        return
    points = np.rint(p - offset).astype(np.int32)
    cv2.polylines(image, [points], False, (0, 165, 255), width, cv2.LINE_AA)


def crop_panel(image, width=PANEL_WIDTH, header=66):
    h = int(round(image.shape[0] * width / image.shape[1]))
    h += h % 2
    result = np.zeros((h + header, width, 3), np.uint8)
    result[header:] = cv2.resize(image, (width, h), interpolation=cv2.INTER_AREA)
    return result


def render(sample, output, source, ids, get_mask, all_data, rows, make_videos=True):
    """One row per method, shared coloured masks; uncertainty remains visible."""
    row_lookup = {(r['worm_id'], r['frame']): r for r in rows}
    cap = cv2.VideoCapture(source['video'])
    if not cap.isOpened():
        raise RuntimeError('Cannot open source video')
    x0, y0, x1, y1 = source['crop_xyxy']
    # Review choices are deterministic: representative simple, biggest disagreement,
    # lowest NIU coverage, and WT-only coverage improvement in complex frames.
    review = []
    for worm in ids:
        sub = [r for r in rows if r['worm_id'] == worm and r['mask_eligible']]
        simple = [r for r in sub if r['topology'] == 'simple']
        complex_rows = [r for r in sub if r['topology'] == 'complex']
        if simple:
            review.append((worm, simple[len(simple) // 2]['frame'], 'simple'))
        if complex_rows:
            chosen = min(complex_rows, key=lambda r: r['niu_ordered_skeleton_coverage'] if np.isfinite(r['niu_ordered_skeleton_coverage']) else -1)
            review.append((worm, chosen['frame'], 'lowest NIU coverage'))
        comparable = [r for r in sub if np.isfinite(r['method_difference_px'])]
        if comparable:
            review.append((worm, max(comparable, key=lambda r: r['method_difference_px'])['frame'], 'largest disagreement'))
    review = list(dict.fromkeys(review))
    save_json(output / 'review-selection.json', [dict(worm_id=w, frame=f, reason=r) for w, f, r in review])
    selected_frames = {0, source['frames'] // 2, source['frames'] - 1} | {f for _, f, _ in review}
    writer = None
    target = output / f'{sample}_NIU_vs_current.mp4'
    gallery = []
    try:
        for f in range(source['frames']):
            ok, full = cap.read()
            if not ok:
                raise RuntimeError(f'Missing video frame {f}')
            if not make_videos and f not in selected_frames:
                continue
            image = full[y0:y1, x0:x1].copy()
            masks = {w: get_mask(w, f) for w in ids}
            for w, mask in masks.items():
                image[mask] = (.68 * image[mask] + .32 * np.array(worm_color(w))).astype(np.uint8)
            left, right = image.copy(), image.copy()
            for w in ids:
                data, row = all_data[w][f], row_lookup[w, f]
                raw = data['raw_xy'].astype(int)
                # Raw topology is visible in pale cyan; orange is the ordered adapter.
                left[raw[:, 1], raw[:, 0]] = (255, 255, 0)
                draw_path(left, data['ordered'], width=CENTERLINE_WIDTH_PX)
                draw_path(right, data['current'], width=CENTERLINE_WIDTH_PX)
                for q in data['nodes_xy']:
                    cv2.circle(left, tuple(q), 4, (255, 60, 30), -1, cv2.LINE_AA)
                for canvas, p in ((left, data['ordered']), (right, data['current'])):
                    if valid_line(p):
                        for t, name in ((0, 'A'), (-1, 'B')):
                            cv2.circle(canvas, tuple(np.rint(p[t]).astype(int)), 4, worm_color(w), -1)
                            label(canvas, f'{name}{w}', tuple(np.rint(p[t] + [3, -5]).astype(int)), .6, worm_color(w))
            a, b = crop_panel(left), crop_panel(right)
            label(a, f'NIU skeleton + ordered adapter | {sample} | frame {f}', (10, 23))
            label(a, 'Cyan: raw graph; blue: 5 random nodes; orange: ordered path', (10, 49), .50)
            label(b, f'Current WormTracer + temporal | frame {f} | {f / source["fps"]:.2f}s', (10, 23))
            label(b, 'Same masks. Solid lines; review flags in CSV. A/B are unconfirmed.', (10, 49), .49)
            pair = np.hstack([a, b])
            if make_videos:
                if writer is None:
                    writer = imageio_ffmpeg.write_frames(str(target), (pair.shape[1], pair.shape[0]),
                              fps=source['fps'], codec='libx264', pix_fmt_in='rgb24', pix_fmt_out='yuv420p',
                              output_params=['-crf', '18', '-preset', 'fast', '-movflags', '+faststart'], macro_block_size=1)
                    writer.send(None)
                writer.send(np.ascontiguousarray(pair[:, :, ::-1]))
            if f in {0, source['frames'] // 2, source['frames'] - 1}:
                write_png(output / f'preview-{f:04d}.png', pair)
            for w, frame, reason in review:
                if f != frame:
                    continue
                yy, xx = np.nonzero(masks[w])
                if not len(xx):
                    continue
                bx, by = max(0, int(xx.min()) - 30), max(0, int(yy.min()) - 30)
                ex, ey = min(image.shape[1], int(xx.max()) + 31), min(image.shape[0], int(yy.max()) + 31)
                cells = []
                for title, im in [('NIU + adapter', left), ('Current temporal', right)]:
                    panel = crop_panel(im[by:ey, bx:ex], 640)
                    label(panel, f'{title} | W{w} F{f}', (10, 23))
                    label(panel, reason, (10, 49), .5)
                    cells.append(panel)
                closeup = np.hstack(cells)
                filename = f'review-w{w}-f{f:04d}-{reason.replace(" ", "-")}.png'
                write_png(output / filename, closeup)
                gallery.append(dict(worm=w, frame=f, reason=reason, image=filename))
            if f % 40 == 0:
                print(f'{sample}: video {f + 1}/{source["frames"]}', flush=True)
    finally:
        cap.release()
        if writer is not None:
            writer.close()
    save_json(output / 'gallery.json', gallery)
    if make_videos:
        check = cv2.VideoCapture(str(target))
        fps, size, frames = check.get(cv2.CAP_PROP_FPS), [int(check.get(3)), int(check.get(4))], 0
        while check.read()[0]:
            frames += 1
        check.release()
        if frames != source['frames'] or abs(fps - source['fps']) > .01:
            raise RuntimeError('Encoded comparison video failed frame/fps verification')
        return dict(frames=frames, fps=fps, resolution=size, codec='H.264', path=str(target))
    return None


def run_sample(sample, output_root, requested=None, make_videos=True):
    if not sample or Path(sample).name != sample or sample in ('.', '..'):
        raise ValueError('sample must be an existing output directory name')
    current, data, source, ids, get_mask, bad = source_for(sample, requested)
    base = ROOT / 'outputs' / sample
    output = output_root / sample
    output.mkdir(parents=True, exist_ok=True)
    print(f'{sample}: shared masks = {data}; current = {current}', flush=True)
    before, file_count = source_digest(data)
    fits_before = fit_digest(base, current, ids)
    started = time.perf_counter()
    all_data, rows, niu_npz, ordered_npz, current_npz, timings = {}, [], {}, {}, {}, []
    for w in ids:
        fit = base / current / f'worm-{w}'
        fitted = np.load(fit / 'centerlines.npy', allow_pickle=False)
        if fitted.shape != (source['frames'], ORDERED_POINT_COUNT, 2) or not np.isfinite(fitted).all():
            raise ValueError(f'Invalid cached current centerlines: {fit}')
        quality = json.loads((fit / 'quality.json').read_text(encoding='utf-8')) if (fit / 'quality.json').exists() else None
        if quality is not None and (len(quality) != source['frames'] or any(q['frame'] != i for i, q in enumerate(quality))):
            raise ValueError('Current quality frame mismatch')
        seq, reference_lengths, extraction_seconds = [], [], 0.
        previous = None
        for f in range(source['frames']):
            mask = get_mask(w, f)
            t = time.perf_counter()
            item = extract_niu(mask)
            extraction_seconds += time.perf_counter() - t
            item['ordered'] = align(item['ordered'], previous)
            previous = item['ordered']
            item['current'] = fitted[f].copy()
            item['mask_eligible'] = bool(mask.any() and not bad.get((f, w), False))
            item['legacy_valid'] = bool(quality[f]['centerline_valid']) if quality else True
            if item['mask_eligible'] and item['topology'] == 'simple' and valid_line(item['ordered']):
                reference_lengths.append(np.linalg.norm(np.diff(item['ordered'], axis=0), axis=1).sum())
            seq.append(item)
            niu_npz[f'w{w}_f{f}_skeleton_xy'] = item['raw_xy']
            niu_npz[f'w{w}_f{f}_random_nodes_xy'] = item['nodes_xy']
            if item['ordered'] is not None:
                ordered_npz[f'w{w}_f{f}'] = item['ordered']
            current_npz[f'w{w}_f{f}'] = fitted[f]
            if f % 50 == 0:
                print(f'{sample} W{w}: skeleton {f + 1}/{source["frames"]}', flush=True)
        reference = float(np.median(reference_lengths)) if reference_lengths else None
        for f, item in enumerate(seq):
            mask = get_mask(w, f)
            row = dict(sample=sample, worm_id=w, frame=f, time_s=f / source['fps'],
                       mask_eligible=item['mask_eligible'], topology=item['topology'],
                       skeleton_endpoints=item['endpoints'], skeleton_branch_pixels=item['branch_pixels'],
                       skeleton_components=item['components'], skeleton_cycles=item['cycles'],
                       skeleton_pixels=len(item['raw_xy']), ordered_path_fraction=item['path_fraction'],
                       length_reference_px=reference, current_legacy_valid=item['legacy_valid'])
            for method, key in (('niu_ordered', 'ordered'), ('current', 'current')):
                metrics = line_metrics(item[key], mask, item['raw_xy'], item['radius'])
                length_ok = (reference is None or (metrics['available'] and
                             LENGTH_RANGE[0] * reference <= metrics['length_px'] <= LENGTH_RANGE[1] * reference))
                metrics['common_pass'] = bool(item['mask_eligible'] and metrics['geometric_pass'] and length_ok)
                metrics['length_in_range'] = bool(length_ok)
                # Never form a temporal pair across a mask-excluded frame.
                previous = seq[f - 1] if (f and item['mask_eligible'] and seq[f - 1]['mask_eligible']
                           and all(valid_line(item[k]) and valid_line(seq[f - 1][k]) for k in ('ordered', 'current'))) else None
                metrics.update(sequence_metrics(item[key], previous[key] if previous else None,
                               np.array(item['centroid']), np.array(previous['centroid']) if previous else None, reference))
                row.update({method + '_' + k: v for k, v in metrics.items()})
            row['method_difference_px'] = float(np.mean(np.linalg.norm(align(item['ordered'], item['current']) - item['current'], axis=1))) if valid_line(item['ordered']) else np.nan
            rows.append(row)
        all_data[w] = seq
        timings.append(dict(worm_id=w, niu_skeleton_and_adapter_seconds=extraction_seconds,
                            niu_seconds_per_frame=extraction_seconds / source['frames'],
                            reference_from_simple_masks_frames=len(reference_lengths), reference_length_px=reference,
                            current_result='reused; no comparable fresh end-to-end timing'))
    np.savez_compressed(output / 'niu-raw-skeletons.npz', **niu_npz)
    np.savez_compressed(output / 'niu-ordered-centerlines.npz', **ordered_npz)
    np.savez_compressed(output / 'current-centerlines.npz', **current_npz)
    summaries = summarise(rows)
    write_csv(output / 'per-frame-comparison.csv', rows)
    write_csv(output / 'summary.csv', summaries)
    save_json(output / 'summary.json', summaries)
    verification = render(sample, output, source, ids, get_mask, all_data, rows, make_videos)
    after, _ = source_digest(data)
    fits_after = fit_digest(base, current, ids)
    if before != after or fits_before != fits_after:
        raise RuntimeError('Inputs changed during comparison; results are not a controlled comparison')
    metadata = dict(sample=sample, current_source=current, shared_mask_source=str(data), video=source['video'],
                    crop_xyxy=source['crop_xyxy'], frames=source['frames'], ids=ids,
                    coordinate_system='analysis crop XY pixels; add crop_xyxy[:2] for original video',
                    masks_sha256_before=before, masks_sha256_after=after, hashed_input_files=file_count,
                    current_fits_sha256_before=fits_before, current_fits_sha256_after=fits_after,
                    input_unchanged=True, blog=BLOG, repository=REPOSITORY,
                    exact_blog_steps='skeletonize(binary mask) + random 5 pixels, seed 42 per frame',
                    adapter='largest skeleton component; longest endpoint geodesic; 101 arc-length points; spatial smoothing; endpoint orientation alignment. No hidden-tip inference, pruning, or temporal fitting.',
                    code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    settings=dict(random_seed=RANDOM_SEED, random_nodes=RANDOM_NODE_COUNT,
                                  ordered_points=ORDERED_POINT_COUNT, smooth_window=SMOOTH_WINDOW,
                                  min_inside=MIN_INSIDE, min_skeleton_coverage=MIN_SKELETON_COVERAGE,
                                  max_turn_degrees=MAX_TURN_DEGREES, length_range=LENGTH_RANGE),
                    versions=dict(numpy=np.__version__, scipy=scipy.__version__, skimage=skimage.__version__, cv2=cv2.__version__),
                    timing=timings, total_seconds=time.perf_counter() - started, video_verification=verification,
                    caveats=['No manually traced centerline ground truth: geometric pass is not accuracy.',
                             'Current pipeline includes prior image/temporal/manual corrections; NIU uses individual masks only.',
                             'A simple graph can still be the wrong worm or incomplete; mask QA is not perfect.',
                             'Raw skeleton coverage favours medial methods; length and temporal regularity favour constrained fits. Inspect both.',
                             'Random nodes have no anatomical identity across frames. Never use node index as a tracked body point.',
                             'Old masks/fits were not recomputed or modified; no fresh current-method speed comparison.'])
    save_json(output / 'run.json', metadata)
    print(f'{sample}: complete; {len(rows)} worm-frames; inputs unchanged', flush=True)
    return summaries, metadata


def report(output_root, summaries, metadata):
    lines = ['# NIU OSW25 骨架与当前中心线方法对比', '',
             f'方法来源：[NIU 博客]({BLOG})；[作者代码]({REPOSITORY})。', '',
             '原文方法输出单像素骨架，并随机选 5 个节点。随机节点没有头尾顺序或跨帧身份。本次保留这两项原始输出；为比较连续中心线，另加最长端点路径、101 点等弧长采样、空间平滑与端点方向对齐。没有为新方法加入时序拟合、隐蔽端点推断或交叉解环。', '',
             '两种方法逐帧使用完全相同的已保存掩膜。当前中心线直接复用原有结果，包含原流程中的时序及人工修正。输入掩膜和当前中心线文件在运行前后做 SHA-256 检查。', '',
             '## 统一几何筛查', '',
             f'进入统计的帧先通过共享的掩膜质量标记，再用相同的掩膜内比例 ≥ {MIN_INSIDE:.0%}、骨架覆盖率 ≥ {MIN_SKELETON_COVERAGE:.0%}、局部转角 ≤ {MAX_TURN_DEGREES}° 和长度范围 {LENGTH_RANGE[0]:.0%}–{LENGTH_RANGE[1]:.0%} 进行筛查。长度参考取同一条虫的简单掩膜 NIU 路径长度中位数；无简单帧时不使用长度筛查。', '',
             '骨架覆盖率是原始骨架像素到密集路径的距离在 max(3 px, 0.75 × 骨架处中位半径) 内的比例。它用于发现漏段，不能决定交叉处遍历方向。简单/复杂分层来自同一原始骨架图的端点、分叉、连通分量和环。', '',
             '**这些是几何一致性检查，不是中心线准确率。没有逐帧人工中心线真值，不能据此声称某方法达到某准确率。**', '',
             '|视频|形态分层|共享可用掩膜帧|NIU 可提取路径|当前可提取路径|NIU 几何通过|当前几何通过|',
             '|---|---|---:|---:|---:|---:|---:|']
    for meta in metadata:
        for kind in ('all', 'simple', 'complex'):
            pair = [r for r in summaries if r['sample'] == meta['sample'] and r['worm_id'] == 'all' and r['stratum'] == kind]
            if len(pair) != 2:
                continue
            a, b = pair
            lines.append(f'|{meta["sample"]}|{kind}|{a["eligible_mask_frames"]}|{a["available_frames"]}|{b["available_frames"]}|{a["common_pass_frames"]}|{b["common_pass_frames"]}|')
    lines += ['', '## 时间连续性（同一组可提取帧）', '',
              '|视频 / 虫|NIU 体长 CV|当前体长 CV|NIU 体长变化 P95|当前体长变化 P95|NIU 端点变化 P95|当前端点变化 P95|',
              '|---|---:|---:|---:|---:|---:|---:|']
    def fmt(value):
        return '—' if value is None else f'{value:.2%}'
    for meta in metadata:
        for w in meta['ids']:
            a, b = [r for r in summaries if r['sample'] == meta['sample'] and r['worm_id'] == w and r['stratum'] == 'all']
            lines.append(f'|{meta["sample"]} / {w}|{fmt(a["paired_length_cv"])}|{fmt(b["paired_length_cv"])}|{fmt(a["paired_length_change_p95"])}|{fmt(b["paired_length_change_p95"])}|{fmt(a["paired_endpoint_change_p95"])}|{fmt(b["paired_endpoint_change_p95"])}|')
    lines += ['', '端点变化已去除掩膜质心平移并允许 A/B 反向对齐，按参考体长归一化；仍包含真实身体运动。两种方法各自的连续性数值只在共同可提取帧上汇总；缺帧和掩膜排除帧之间不跨帧计算。当前拟合本来就使用时序和长度约束，因此低波动不等于高准确率。', '',
              '## 查看结果', '',
              '每个样本目录包括：并排 H.264 视频、逐帧表、按虫与形态分层的汇总、原始骨架与随机节点、NIU 有序中心线、当前中心线副本、放大审阅图和运行记录。缺失的 NIU 路径在 NPZ 中不生成条目，不借用另一种方法填补。', '',
              '视频左侧：NIU 骨架及新增有序路径；右侧：当前中心线。两侧彩色掩膜相同，橘黄色中心线全程画实线。青色为原始骨架，蓝点为原文随机节点。质量和待复核信息保留在逐帧表中；实线不表示结果已经确认。A/B 不代表已确认的生物学头尾。', '',
              '放大审阅图按确定规则选择：简单帧中点、复杂帧最低 NIU 覆盖率、两种路径最大差异。选择规则和帧号写入 review-selection.json。请同时查看完整视频，避免只依据差异最大的帧下结论。', '',
              '### 运算时间', '', 'NIU 骨架与有序路径耗时包含骨架化、图搜索和采样，不含读取掩膜、指标比较及视频编码。当前结果为缓存复用，不能把它的读取时间作为拟合速度。']
    for meta in metadata:
        seconds = sum(t['niu_skeleton_and_adapter_seconds'] for t in meta['timing'])
        lines.append(f'- {meta["sample"]}：NIU 新计算 {seconds:.2f} s / {meta["frames"] * len(meta["ids"])} 个虫帧；当前版本 `{meta["current_source"]}`。输入校验：未改变。')
    lines += ['', '方法选择与具体帧的人工审阅结论见同目录 `结论与使用建议.md`（首次运行脚本时不自动生成主观准确性结论）。']
    (output_root / '对比报告.md').write_text('\n'.join(lines), encoding='utf-8')
    write_csv(output_root / 'all-summaries.csv', summaries)
    save_json(output_root / 'all-summaries.json', summaries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', nargs='+', default=SAMPLES)
    parser.add_argument('--current-source', default=CURRENT_SOURCE)
    parser.add_argument('--output', type=Path, default=OUTPUT_ROOT)
    parser.add_argument('--no-video', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    summaries, metadata = [], []
    for sample in args.sample:
        sub, meta = run_sample(sample, args.output, args.current_source, MAKE_VIDEOS and not args.no_video)
        summaries.extend(sub); metadata.append(meta)
        report(args.output, summaries, metadata)
    print(f'Completed: {args.output / "对比报告.md"}', flush=True)


if __name__ == '__main__':
    main()

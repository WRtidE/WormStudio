"""Adapt local SAM2 + TAPNext++ results to the Studio measurement contract."""
import argparse
import json
import sys
import time
import uuid

import numpy as np

from analyze_phenotypes import ROOT, load_sample
from pipeline_progress import report_progress
from tapnext_support import VERSION, resources, signature, validate_single_worm, prepare_runtime


def measurement_output(curves, baseline_quality, metrics, meta):
    """Keep rejected hypotheses separate; output coordinates are analysis-crop XY."""
    candidates = curves + np.asarray(meta['tracking_roi'][:2])
    accepted = candidates.copy()
    quality = []
    for i, (base, row) in enumerate(zip(baseline_quality, metrics)):
        valid = bool(row['pose_metrics_valid'] and np.isfinite(candidates[i]).all())
        reasons = list(dict.fromkeys(filter(None, row['review_reason'].split(';'))))
        if not valid:
            accepted[i] = np.nan
            if not reasons:
                reasons.append('tapnext_pose_not_supported')
        item = dict(base, frame=i, mask_valid=bool(row['mask_metrics_valid']),
            centerline_valid=valid, quality_reasons=reasons,
            selected_candidate=row['candidate'], branch_score_margin=None,
            endpoint_a_inferred=not row['endpoint_a_observed'],
            endpoint_b_inferred=not row['endpoint_b_observed'],
            uncertain_point_ranges=[] if valid else [[0, 100]],
            temporal_anchors=meta['anchors'], temporal_reconstructed=row['temporal_reconstructed'],
            tapnext_status=row['status'])
        quality.append(item)
    return accepted, quality, candidates


def run(sample, worm_filter=None):
    package, checkpoint = resources()
    dependencies = prepare_runtime()
    sys.path.insert(0, str(package))
    from wormstudio_single.pipeline import fresh_inputs, solve_fresh, sha256
    from wormstudio_single.metrics import calculate_metrics, write_csv
    from wormstudio_single.quality import write_json
    from wormstudio_single.fusion_v4 import VERSION as SOLVER_VERSION

    started = time.monotonic()
    data, source, ids, get_mask, bad = load_sample(sample)
    validate_single_worm(len(ids), source.get('sam2_model', 'large'))
    worm = ids[0]
    if worm_filter is not None and worm_filter != worm:
        raise ValueError('所选线虫编号与分割结果不一致')
    report_progress(detail='校验本地 TAPNext++ 模型权重', force=True)
    expected = json.loads((package / 'docs' / 'models.json').read_text(encoding='utf-8'))['tapnext']['sha256']
    checksum = sha256(checkpoint)
    if checksum != expected:
        raise ValueError('TAPNext++ 权重 SHA-256 与本地算法清单不符，请检查是否下载完整')
    folder = ROOT / 'outputs' / sample / 'centerlines-tapnext'
    target = folder / f'worm-{worm}'
    target.mkdir(parents=True, exist_ok=True)
    # Keep each run's evidence together, so old tracking windows cannot mix with a retry.
    evidence = target / ('evidence-' + uuid.uuid4().hex[:10])
    evidence.mkdir()
    config = dict(video=source['video'], crop_xyxy=source['crop_xyxy'],
        scale_um_per_px=source['scale_um_per_px'] if source.get('measurement_mode')!='pixels' else 1., anchor_interval_seconds=2.,
        excluded_frames=[i for i in range(source['frames']) if bad.get((i, worm), False)])
    masks = []
    for i in range(source['frames']):
        report_progress(i, source['frames'], '读取 SAM2 轮廓，准备单虫时序跟踪')
        masks.append(get_mask(worm, i))
    report_progress(detail='选择可靠姿态与双向跟踪锚点', force=True)
    masks, baseline, base_quality, meta = fresh_inputs(config, np.asarray(masks, bool), source['fps'])
    report_progress(detail='TAPNext++ 双向点跟踪与时序融合；详细窗口进度见运行日志', force=True)
    curves, checks, timings = solve_fresh(config, masks, baseline, base_quality, meta,
        evidence, checkpoint, 'cuda', solver='auto')
    rows = calculate_metrics(curves, masks, base_quality, checks, baseline, meta)
    if source.get('measurement_mode') == 'pixels':
        from measurement_units import pixel_fields
        for row in rows: pixel_fields(row)
        meta['measurement_mode'] = 'pixels'
        meta['scale_um_per_px'] = None
    accepted, quality, candidates = measurement_output(curves, base_quality, rows, meta)
    np.save(target / 'centerlines.npy', accepted)
    np.save(target / 'diagnostic-candidates.npy', candidates)
    write_json(target / 'quality.json', quality)
    write_json(target / 'frame-metrics.json', rows)
    write_csv(target / 'frame-metrics.csv', rows)
    write_json(evidence / 'metadata.json', meta)
    write_json(evidence / 'baseline-quality.json', base_quality)
    write_json(evidence / 'solver-quality.json', checks)
    np.savez_compressed(evidence / 'inputs.npz', masks=masks, baseline=baseline)
    write_json(folder / 'mask-source.json', dict(mask_source=str(data.resolve())))
    write_json(target / 'run.json', dict(version=VERSION, algorithm_signature=signature(),
        solver=SOLVER_VERSION, solver_selection='auto', method='tapnext', frames=len(curves), worm_id=worm,
        valid_frames=sum(q['centerline_valid'] for q in quality),
        mask_source=str(data.resolve()), package=str(package), checkpoint=str(checkpoint),
        checkpoint_sha256=checksum, evidence_directory=evidence.name, timings=timings, dependencies=dependencies,
        seconds=time.monotonic()-started, coordinates='Analysis crop pixels; A/B is not anatomical head/tail',
        missing_policy='Rejected hypotheses are NaN in measurements; diagnostic-candidates.npy is for review only'))
    report_progress(len(curves), len(curves), '单虫跟踪完成；待复核姿态已与正式测量分开保存', force=True)
    print(f'TAPNext++: {sum(q["centerline_valid"] for q in quality)}/{len(curves)} supported frames', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', required=True)
    parser.add_argument('--worm', type=int)
    args = parser.parse_args()
    run(args.sample, args.worm)

"""Export body16 suggestions only where the v4 baseline has no centerline.

This module never merges suggestions into primary coordinates or metrics.
"""
from pathlib import Path
import hashlib
import json

import numpy as np

from .metrics import write_csv
from .quality import write_json


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_missing_references(primary, auxiliary, primary_mask_valid, auxiliary_mask_valid, auxiliary_geometry):
    """Select references without changing any primary frame, including REVIEW."""
    primary=np.asarray(primary);auxiliary=np.asarray(auxiliary)
    if primary.ndim!=3 or primary.shape[1:]!=(101,2) or auxiliary.shape!=primary.shape:
        raise ValueError('Both result arrays must have the same (frames,101,2) shape.')
    n=len(primary)
    if any(np.asarray(x).shape!=(n,) for x in (primary_mask_valid,auxiliary_mask_valid,auxiliary_geometry)):
        raise ValueError('Quality arrays must cover every frame.')
    # A partially populated frame is corruption, not an unavailable centerline.
    for array in (primary,auxiliary):
        if not (np.isfinite(array).all((1,2)) | np.isnan(array).all((1,2))).all():
            raise ValueError('Each centerline must be entirely finite or entirely NaN.')
    available=np.isfinite(primary).all((1,2));candidate=np.isfinite(auxiliary).all((1,2))
    selected=(~available & candidate & np.asarray(primary_mask_valid,bool)
              & np.asarray(auxiliary_mask_valid,bool) & np.asarray(auxiliary_geometry,bool))
    references=np.full(primary.shape,np.nan,dtype=float)
    references[selected]=auxiliary[selected]
    return references,selected


def export_references(baseline_dir,body16_dir,output):
    baseline=Path(baseline_dir).resolve();body16=Path(body16_dir).resolve();out=Path(output).resolve()
    if any(out==source or out.is_relative_to(source) for source in (baseline,body16)):
        raise ValueError('Reference output must be separate from both input result directories.')
    if out.exists() and any(out.iterdir()):raise ValueError('Output directory is not empty.')
    primary_meta=_read(baseline/'metadata.json');aux_meta=_read(body16/'metadata.json')
    for key in ('source_video_sha256','frames','fps','crop_xyxy','tracking_roi','anchors',
                'scale_um_per_px','tapnext_checkpoint_sha256','version'):
        if key not in primary_meta or key not in aux_meta or primary_meta[key]!=aux_meta[key]:
            raise ValueError(f'Baseline/body16 metadata mismatch: {key}.')
    if not str(primary_meta['version']).startswith('tapnext-evidence-v4'):
        raise ValueError('This reference policy requires v4 results.')
    main_settings=primary_meta.get('tapnext_support',{});aux_settings=aux_meta.get('tapnext_support',{})
    if main_settings.get('per_query')!=0:
        raise ValueError('Primary results must use zero support queries (baseline).')
    if (aux_settings.get('per_query'),aux_settings.get('mode'),aux_settings.get('radius_model_px'))!=(16,'body',32.):
        raise ValueError('Auxiliary results must use body16 with the validated 32 px radius.')
    for key in ('input_resolution','primary_queries'):
        if main_settings.get(key)!=aux_settings.get(key):raise ValueError(f'Tracker setting mismatch: {key}.')
    with np.load(baseline/'inputs.npz',allow_pickle=False) as main,np.load(body16/'inputs.npz',allow_pickle=False) as aux:
        for key in ('masks','baseline'):
            if not np.array_equal(main[key],aux[key],equal_nan=True):
                raise ValueError(f'Baseline/body16 inputs differ: {key}.')
    a=np.load(baseline/'centerlines-candidate.npy',allow_pickle=False)
    b=np.load(body16/'centerlines-candidate.npy',allow_pickle=False)
    primary_rows=_read(baseline/'frame-metrics.json');aux_rows=_read(body16/'frame-metrics.json')
    primary_quality=_read(baseline/'baseline-quality.json');aux_quality=_read(body16/'baseline-quality.json')
    checks=_read(body16/'quality.json');n=primary_meta['frames'];fps=primary_meta['fps']
    if len(a)!=n or any(len(rows)!=n for rows in (primary_rows,aux_rows,primary_quality,aux_quality,checks)):
        raise ValueError('Results do not cover the declared frame count.')
    for rows in (primary_rows,aux_rows,checks):
        if [row.get('frame') for row in rows]!=list(range(n)):
            raise ValueError('Frame records must be ordered and complete.')
    references,selected=select_missing_references(a,b,
        [q['mask_valid'] for q in primary_quality],[q['mask_valid'] for q in aux_quality],
        [q.get('geometric_pass',False) for q in checks])
    rows=[]
    for i in range(n):
        present=np.isfinite(a[i]).all()
        rows.append(dict(frame=i,time_s=i/fps,baseline_status=primary_rows[i]['status'],
            reference_status='review_only' if selected[i] else ('baseline_available' if present else 'no_reference'),
            reference_source='body16' if selected[i] else None,pose_metrics_valid=False,
            length_px=None,mean_width_px=None,mean_abs_curvature_rad_px=None,length_um_estimate=None,
            reference_candidate_length_px=float(np.linalg.norm(np.diff(references[i],axis=0),axis=1).sum()) if selected[i] else None,
            head_identity='unconfirmed_A_B',review_reason='auxiliary_only;inferred_pose;manual_review_required' if selected[i] else None))
    selected_ids=np.flatnonzero(selected).tolist()
    summary=dict(policy='v4-baseline-primary-body16-missing-reference',frames=n,
        baseline_candidate_frames=int(np.isfinite(a).all((1,2)).sum()),
        baseline_auto_supported_frames=sum(bool(r['pose_metrics_valid']) for r in primary_rows),
        body16_reference_frames=len(selected_ids),reference_frame_indices=selected_ids,
        primary_results_modified=False,references_promoted_to_measurements=False,
        source_video_sha256=primary_meta['source_video_sha256'],coordinate_space='original_video_xy_pixels',
        frame_index_base=0,baseline_coordinate_sha256=_digest(baseline/'centerlines-candidate.npy'),
        body16_coordinate_sha256=_digest(body16/'centerlines-candidate.npy'),
        accuracy_claim='Auxiliary suggestions only; no independent human ground-truth accuracy established.')
    out.mkdir(parents=True,exist_ok=True)
    np.save(out/'centerlines-body16-reference.npy',references)
    write_json(out/'reference-metrics.json',rows);write_csv(out/'reference-metrics.csv',rows)
    write_json(out/'summary.json',summary)
    table=''.join(f'<tr><td>{i+1}</td><td>{i/fps:.2f}</td><td>待复核；正式姿态指标为空</td></tr>' for i in selected_ids)
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>body16 缺失帧辅助参考</title><style>body{max-width:1000px;margin:32px auto;padding:20px;font:17px/1.7 system-ui;color:#243044}table{border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #ddd}</style>
<h1>baseline 主结果 · body16 缺失帧辅助参考</h1><p>baseline 的中线、状态和正式指标完全保留。此处只列出 baseline 无中线、body16 提供几何可行候选的帧。参考结果不会自动变为主结果或有效测量。</p>'''
    page+=f'<p>主结果：{summary["baseline_candidate_frames"]}/{n} 帧有候选，{summary["baseline_auto_supported_frames"]} 帧自动质检通过。另有 {len(selected_ids)} 帧辅助参考；这些计数不是准确率。</p>'
    page+='<table><tr><th>画面帧号（从 1 开始）</th><th>时间/秒</th><th>状态</th></tr>'+table+'</table>'
    page+='<p>CSV/JSON/NPY 使用从 0 开始的帧号，坐标为原视频像素。</p><p><a href="centerlines-body16-reference.npy">独立参考坐标</a> · <a href="reference-metrics.csv">参考 CSV</a> · <a href="reference-metrics.json">参考 JSON</a> · <a href="summary.json">来源与统计</a></p></html>'
    (out/'index.html').write_text(page,encoding='utf-8')
    return summary

"""Portable CLI, preserved v3 replay and experimental v4 evidence fusion."""
from pathlib import Path
import hashlib
import json
import time

import cv2
import numpy as np

from .baseline import resolve_sequence
from .fusion import VERSION, fit, feasible_quality
from .mask_quality import mark_quality
from .metrics import choose_anchors, make_windows, crop_union, calculate_metrics, write_csv
from .quality import INDICES, align, write_json, _SKELETON_CACHE


def run_fusion(data, tracks, solver='auto'):
    from .evidence import has_evidence
    if solver not in ('auto','v3','v4'):raise ValueError('Unknown solver.')
    if solver=='v4' or (solver=='auto' and has_evidence(tracks)):
        from .fusion_v4 import fit as fit_v4
        return fit_v4(data,tracks)
    return fit(data,tracks)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(2**20), b''):
            digest.update(block)
    return digest.hexdigest()


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding='utf-8'))
    for key in ('video', 'seed_mask', 'cache'):
        if config.get(key):
            config[key] = (path.parent / config[key]).resolve()
    if not config.get('video') or not config['video'].is_file():
        raise ValueError('Config video must reference an existing local video.')
    crop = config.get('crop_xyxy')
    if not (isinstance(crop, list) and len(crop) == 4 and all(isinstance(v, int) for v in crop)
            and 0 <= crop[0] < crop[2] and 0 <= crop[1] < crop[3]):
        raise ValueError('crop_xyxy must contain ordered nonnegative integer bounds.')
    scale = config.get('scale_um_per_px')
    if scale is not None and (not np.isfinite(scale) or scale <= 0):
        raise ValueError('scale_um_per_px must be positive, or null when unknown.')
    interval = config.get('anchor_interval_seconds', 2.)
    if not np.isfinite(interval) or interval <= 0:
        raise ValueError('anchor_interval_seconds must be positive.')
    config['config_path'] = path
    return config


def video_info(config):
    cap = cv2.VideoCapture(str(config['video']))
    try:
        if not cap.isOpened():
            raise ValueError('Cannot open input video.')
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    if n < 1 or not np.isfinite(fps) or fps <= 0:
        raise ValueError('Input needs a positive frame count and constant frame rate.')
    if config['crop_xyxy'][2] > width or config['crop_xyxy'][3] > height:
        raise ValueError('crop_xyxy extends outside the source video.')
    if not 0 <= config.get('anchor_frame', 0) < n:
        raise ValueError('anchor_frame is outside the video.')
    if any(not isinstance(i, int) or not 0 <= i < n for i in config.get('excluded_frames', [])):
        raise ValueError('excluded_frames must contain zero-based frame indices.')
    return n, fps


def read_frames(config, roi):
    cx, cy, _, _ = config['crop_xyxy']
    x0, y0, x1, y1 = roi
    cap = cv2.VideoCapture(str(config['video']))
    frames = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(frame[cy+y0:cy+y1, cx+x0:cx+x1].copy())
    finally:
        cap.release()
    return np.asarray(frames)


def replay_inputs(config):
    cache = config.get('cache')
    if cache is None:
        raise ValueError('Replay requires a cache directory in the config.')
    meta = json.loads((cache/'metadata.json').read_text(encoding='utf-8'))
    if sha256(config['video']) != meta['source_video_sha256']:
        raise ValueError('Cached tracks belong to a different source video (SHA-256 mismatch).')
    n, fps = video_info(config)
    if n != meta['frames'] or abs(fps-meta['fps']) > 1e-7 or config['crop_xyxy'] != meta['crop_xyxy']:
        raise ValueError('Cache/video frame count, frame rate or crop mismatch.')
    with np.load(cache/'inputs.npz', allow_pickle=False) as data:
        masks, baseline = data['masks'].copy(), data['baseline'].copy()
    quality = json.loads((cache/'baseline-quality.json').read_text(encoding='utf-8'))
    if masks.shape[0] != n or baseline.shape != (n, 101, 2) or len(quality) != n:
        raise ValueError('Incomplete cache.')
    meta.update(source_video=str(config['video']), scale_um_per_px=config.get('scale_um_per_px'))
    return masks, baseline, quality, meta


def fresh_inputs(config, masks, fps):
    areas = masks.sum((1, 2))
    rows = [dict(frame=i, worm_id=1, area_px=int(a), ambiguous_overlap_px=0) for i,a in enumerate(areas)]
    mark_quality(rows, [1])
    valid = np.array([not r['low_confidence'] for r in rows])
    roi = crop_union(masks)
    x0, y0, x1, y1 = roi
    local_masks = masks[:, y0:y1, x0:x1].copy()
    baseline, quality, _ = resolve_sequence(local_masks, mask_valid=valid, fps=fps)
    previous = None
    for i,p in enumerate(baseline):
        if np.isfinite(p).all():
            if previous is not None:
                baseline[i] = align(p, previous)
            previous = baseline[i]
    # Apply visual exclusions at the same stage as the validated historical batch.
    for i in config.get('excluded_frames', []):
        quality[i]['mask_valid'] = False
        quality[i]['centerline_valid'] = False
        quality[i]['quality_reasons'].append('user_excluded_mask')
    valid = np.array([q['mask_valid'] and q['centerline_valid'] and q['topology']=='simple'
                      and np.isfinite(p).all() for p,q in zip(baseline,quality)])
    anchors = choose_anchors(valid, max_gap=max(1, round(config.get('anchor_interval_seconds',2.)*fps)))
    refs = [q['reference_length_px'] for q in quality if q.get('reference_length_px')]
    meta = dict(source_video=str(config['video']), source_video_sha256=sha256(config['video']),
                frames=len(masks), fps=fps, crop_xyxy=config['crop_xyxy'], tracking_roi=roi,
                scale_um_per_px=config.get('scale_um_per_px'), anchors=anchors,
                reference_length_px=float(np.median(refs)) if refs else None,
                head_identity='unconfirmed_A_B')
    return local_masks, baseline, quality, meta


def solve_cached(masks, baseline, quality, meta, cache, solver='auto'):
    curves = np.full((len(masks), 101, 2), np.nan)
    qa = [None] * len(masks)
    covered = np.zeros(len(masks), bool)
    for path in sorted(Path(cache).glob('tracks-*.npz')):
        with np.load(path, allow_pickle=False) as raw:
            z = {k:raw[k] for k in raw.files if k.startswith(('forward','backward'))}
            ids, roi = raw['frame_ids'], raw['roi']
        if ids.ndim != 1 or not len(ids) or np.any(ids < 0) or np.any(ids >= len(masks)):
            raise ValueError(f'Invalid frame indices in {path.name}')
        lx,ly,rx,by = roi
        offset = np.array([lx,ly])
        has_end = np.isfinite(z['backward']).all()
        data = dict(first=baseline[ids[0]]-offset,
                    last=baseline[ids[-1]]-offset if has_end else np.full((101,2),np.nan),
                    masks=masks[ids,ly:by,lx:rx], frame_ids=ids,
                    mask_valid=np.array([quality[i]['mask_valid'] for i in ids]),
                    reference_length=meta['reference_length_px'])
        points, checks = run_fusion(data,z,solver)
        for i,p,q in zip(ids,points,checks):
            curves[i],qa[i],covered[i] = p+offset,q,True
        _SKELETON_CACHE.clear()
    if not covered.all() or any(q is None for q in qa):
        raise ValueError('Saved point-track windows do not cover the entire video.')
    return curves,qa


def solve_fresh(config, masks, baseline, quality, meta, out, checkpoint, device, solver='auto',
                support_count=0,support_mode='local',support_radius=32.):
    from .inference import load_tracker, track
    from .evidence import SCHEMA
    from .support import support_points
    n = len(masks)
    curves = np.full((n,101,2),np.nan)
    qa = [dict(frame=i,candidate='none',geometric_pass=False) for i in range(n)]
    if not meta['anchors']:
        return curves,qa,dict(model_load_seconds=0.,tracking_seconds=0.,fusion_seconds=0.)
    start = time.perf_counter()
    model = load_tracker(checkpoint, device)
    if device=='cuda':
        import torch
        torch.cuda.reset_peak_memory_stats()
    meta['tapnext_support']=dict(per_query=support_count,mode=support_mode,radius_model_px=support_radius,
                                 input_resolution=model.input_resolution,primary_queries=len(INDICES),windows=[])
    timings = dict(model_load_seconds=time.perf_counter()-start,tracking_seconds=0.,fusion_seconds=0.)
    frames = read_frames(config, meta['tracking_roi'])
    if len(frames) != n:
        raise ValueError('Decoded frame count differs from mask count.')
    for k,(ids,has_end) in enumerate(make_windows(n,meta['anchors'])):
        print(f'Window {k+1}: frames {ids[0]}..{ids[-1]}',flush=True)
        lx,ly,rx,by = crop_union(masks[ids]); offset=np.array([lx,ly])
        data=dict(first=baseline[ids[0]]-offset,last=baseline[ids[-1]]-offset if has_end else np.full((101,2),np.nan),
                  masks=masks[ids,ly:by,lx:rx],frame_ids=ids,
                  mask_valid=np.array([quality[i]['mask_valid'] for i in ids]),reference_length=meta['reference_length_px'])
        local=frames[ids,ly:by,lx:rx]
        stamp=time.perf_counter()
        fs,fp=support_points(data['first'][INDICES],data['masks'][0],support_count,support_mode,support_radius,model.input_resolution)
        fd={};bd={}
        f,fv,fe=track(model,local,data['first'][INDICES],with_evidence=True,support=fs,diagnostics=fd)
        if has_end:
            bs,bp=support_points(data['last'][INDICES],data['masks'][-1],support_count,support_mode,support_radius,model.input_resolution)
            b,bv,be=track(model,local[::-1],data['last'][INDICES],with_evidence=True,support=bs,diagnostics=bd)
            b,bv=b[::-1].copy(),bv[::-1].copy();be={key:value[::-1].copy() for key,value in be.items()}
            for key in ('support_tracks','support_visible'):bd[key]=bd[key][::-1].copy()
        else:
            b=np.full_like(f,np.nan);bv=np.zeros_like(fv)
            be={key:np.zeros_like(value) if key=='coordinate_probabilities' else np.full_like(value,np.nan) for key,value in fe.items()}
            bs=np.empty((0,2),np.float32);bp=np.empty(0,np.int32)
        timings['tracking_seconds']+=time.perf_counter()-stamp
        z=dict(forward=f,backward=b,forward_visible=fv,backward_visible=bv)
        z.update({f'forward_{key}':value for key,value in fe.items()})
        z.update({f'backward_{key}':value for key,value in be.items()})
        aux={f'forward_{key}':value for key,value in fd.items()}
        aux.update({f'backward_{key}':value for key,value in bd.items()})
        aux.update(forward_support_parent=fp,backward_support_parent=bp)
        np.savez_compressed(out/f'tracks-{k:03d}.npz',**z,**aux,frame_ids=ids,roi=[lx,ly,rx,by],evidence_schema=SCHEMA)
        meta['tapnext_support']['windows'].append(dict(first=int(ids[0]),last=int(ids[-1]),forward_count=len(fs),backward_count=len(bs)))
        stamp=time.perf_counter();p,checks=run_fusion(data,z,solver);timings['fusion_seconds']+=time.perf_counter()-stamp
        for i,points,q in zip(ids,p,checks):curves[i]=points+offset;qa[i]=q
        _SKELETON_CACHE.clear()
    if device=='cuda':meta['tapnext_support']['peak_torch_allocated_mib']=torch.cuda.max_memory_allocated()/2**20
    del model
    return curves,qa,timings


def execute(config_path, output, mode='replay', sam2_checkpoint=None, tapnext_checkpoint=None, device='cuda', render=True,
            solver='auto', cache_override=None,support_count=0,support_mode='local',support_radius=32.):
    from .support import support_points
    support_points(np.zeros((1,2)),np.ones((1,1),bool),support_count,support_mode,support_radius)
    if mode=='replay' and support_count:raise ValueError('Adding support queries requires fresh tracking (retrack/run).')
    config=load_config(config_path)
    if cache_override is not None:config['cache']=Path(cache_override).resolve()
    output=Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory is not empty; choose a fresh output directory.')
    output.mkdir(parents=True,exist_ok=True)
    start=time.perf_counter();timings={}
    n,fps=video_info(config)
    if mode=='replay':
        masks,baseline,bq,meta=replay_inputs(config)
        timings['input_seconds']=time.perf_counter()-start
        stamp=time.perf_counter();curves,qa=solve_cached(masks,baseline,bq,meta,config['cache'],solver)
        timings.update(fusion_seconds=time.perf_counter()-stamp,segmentation_seconds=0.,tracking_seconds=0.)
        scope='cached SAM2 masks and TAPNext++ tracks; input, fusion, metrics and optional rendering'
    elif mode=='retrack':
        masks,baseline,bq,meta=replay_inputs(config)
        timings.update(input_seconds=time.perf_counter()-start,segmentation_seconds=0.)
        curves,qa,stage=solve_fresh(config,masks,baseline,bq,meta,output,tapnext_checkpoint,device,solver,support_count,support_mode,support_radius)
        timings.update(stage)
        scope='cached SAM2 masks/baseline; fresh TAPNext++ with evidence, fusion, metrics and optional rendering; excludes segmentation'
    elif mode=='run':
        from .inference import segment
        stamp=time.perf_counter();full_masks=segment(config,sam2_checkpoint,device)
        timings['segmentation_seconds']=time.perf_counter()-stamp
        if len(full_masks)!=n:raise ValueError('Incomplete SAM2 propagation.')
        stamp=time.perf_counter();masks,baseline,bq,meta=fresh_inputs(config,full_masks,fps);del full_masks
        timings['baseline_seconds']=time.perf_counter()-stamp
        curves,qa,stage=solve_fresh(config,masks,baseline,bq,meta,output,tapnext_checkpoint,device,solver,support_count,support_mode,support_radius)
        timings.update(stage)
        scope='fresh segmentation, baseline, TAPNext++ with evidence, fusion, metrics and optional rendering; excludes model downloads'
    else:raise ValueError('Unknown execution mode.')
    stamp=time.perf_counter()
    if any(np.isfinite(p).all() and not q.get('geometric_pass',False) for p,q in zip(curves,qa)):
        raise RuntimeError('Infeasible curve reached the exporter.')
    from .fusion_v4 import VERSION as V4_VERSION
    version=V4_VERSION if any(q.get('solver')==V4_VERSION for q in qa) else VERSION
    meta.update(version=version,display_method='Contour + temporal contact recovery',requested_solver=solver)
    if mode!='replay':meta.update(tapnext_checkpoint_sha256=sha256(tapnext_checkpoint),tapnext_evidence_schema=1)
    rows=calculate_metrics(curves,masks,bq,qa,baseline,meta)
    offset=np.array(meta['crop_xyxy'][:2])+meta['tracking_roi'][:2]
    global_curves=curves+offset
    accepted=global_curves.copy();accepted[[not r['pose_metrics_valid'] for r in rows]]=np.nan
    np.save(output/'centerlines-candidate.npy',global_curves)
    np.save(output/'centerlines-auto-supported.npy',accepted)
    np.savez_compressed(output/'inputs.npz',masks=masks,baseline=baseline)
    write_json(output/'baseline-quality.json',bq);write_json(output/'metadata.json',meta)
    write_json(output/'frame-metrics.json',rows);write_csv(output/'frame-metrics.csv',rows)
    write_json(output/'quality.json',qa)
    timings['metrics_seconds']=time.perf_counter()-stamp
    if render:
        from .render import export_video
        stamp=time.perf_counter();export_video(config,masks,curves,rows,meta,output)
        timings['render_seconds']=time.perf_counter()-stamp
    summary=dict(video=config['video'].name,version=version,mode=mode,frames=n,fps=fps,
        candidate_frames=int(np.isfinite(curves).all((1,2)).sum()),
        auto_supported_frames=sum(r['pose_metrics_valid'] for r in rows),
        temporal_recovered_frames=sum(r['temporal_reconstructed'] for r in rows),
        wall_seconds=time.perf_counter()-start,timing_scope=scope,stages=timings,
        source_video_sha256=meta['source_video_sha256'],accuracy_claim='No human ground-truth accuracy established.')
    write_json(output/'summary.json',summary)
    from .render import write_index
    write_index(output,rows,summary,render)
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return summary

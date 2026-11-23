"""Anonymous, offline tracklet measurements; never establish biological identity.

Every rejected mask, source gap or identity segment boundary breaks continuity.
Three consecutive samples spanning >= 0.25 s are required (offline look-ahead).
This proves only rule continuity, including for a mistakenly tracked static object.
"""
import csv
import json
import math
import numpy as np
from quality_schema import flag

VERSION = 'anonymous-tracklets-v1'
MIN_SPAN_S = .25


def measure(rows, fps, scale_um_per_px=None, timestamps=None):
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError('Tracklets require a positive frame rate')
    minimum = max(3, math.ceil(MIN_SPAN_S * fps) + 1)
    summaries = []
    for worm in sorted({int(r['worm_id']) for r in rows}):
        group = sorted([r for r in rows if int(r['worm_id']) == worm], key=lambda r: int(r['frame']))
        chunks = []; chunk = []
        for r in group:
            r.update(tracklet_id=None, tracklet_status='rejected', tracklet_pose_valid=False,
                     tracklet_motion_valid=False, tracklet_identity_confirmed=False,
                     tracklet_length_px=None, tracklet_mean_width_px=None,
                     tracklet_centroid_x_px=None, tracklet_centroid_y_px=None,
                     tracklet_speed_px_s=None)
            # Source coordinates are always pixels, independent of physical calibration.
            center = [r.get('centroid_x_source_px'), r.get('centroid_y_source_px')]
            good = all(flag(r, k) for k in ('source_valid', 'mask_valid', 'time_valid'))
            good = good and all(isinstance(v, (int, float)) and math.isfinite(v) for v in center)
            boundary = chunk and (int(r['frame']) != int(chunk[-1]['frame']) + 1 or
                r.get('identity_segment') != chunk[-1].get('identity_segment') or
                flag(r, 'identity_valid') != flag(chunk[-1], 'identity_valid'))
            if chunk and (not good or boundary):
                chunks.append(chunk); chunk = []
            if good: chunk.append(r)
        if chunk: chunks.append(chunk)
        for j, chunk in enumerate(chunks):
            tid = f'w{worm}-t{j+1}'
            stable = len(chunk) >= minimum
            times = (timestamps[[int(r['frame']) for r in chunk]] if timestamps is not None
                     else np.arange(len(chunk))/fps)
            stable = stable and bool(times[-1]-times[0] >= MIN_SPAN_S-1e-9)
            centers = np.array([[r['centroid_x_source_px'], r['centroid_y_source_px']] for r in chunk])
            # Backward differences: first sample is null, never bridge missing frames/jumps.
            speeds = [None] + (np.linalg.norm(np.diff(centers, axis=0), axis=1) / np.diff(times)).tolist()
            for i, r in enumerate(chunk):
                pose = stable and flag(r, 'pose_valid')
                r.update(tracklet_id=tid, tracklet_status='stable_anonymous' if stable else 'short_candidate',
                         tracklet_pose_valid=pose, tracklet_motion_valid=stable and i > 0)
                if stable:
                    r['tracklet_centroid_x_px'], r['tracklet_centroid_y_px'] = centers[i].tolist()
                    r['tracklet_speed_px_s'] = speeds[i]
                if pose:
                    scale = 1. if r.get('measurement_mode') == 'pixels' else scale_um_per_px
                    if scale and math.isfinite(scale):
                        suffix = 'px' if r.get('measurement_mode') == 'pixels' else 'um'
                        for target, source in [('tracklet_length_px', 'length_'), ('tracklet_mean_width_px', 'mean_width_')]:
                            value = r.get(source + suffix)
                            r[target] = float(value / scale) if value is not None and math.isfinite(value) else None
            summaries.append(dict(worm_id=worm, tracklet_id=tid, start_frame=chunk[0]['frame'],
                end_frame=chunk[-1]['frame'], frames=len(chunk), stable=stable,
                pose_frames=sum(r['tracklet_pose_valid'] for r in chunk),
                motion_frames=sum(r['tracklet_motion_valid'] for r in chunk),
                additional_pose_frames=sum(r['tracklet_pose_valid'] and not flag(r,'identity_valid') for r in chunk),
                link_to_seed='provisional_continuity' if all(flag(r,'identity_valid') for r in chunk) else 'unverified',
                biological_identity_confirmed=False))
    return dict(version=VERSION, minimum_samples=minimum, minimum_span_s=MIN_SPAN_S,
        scope='anonymous fragment only; not a confirmed individual trajectory',
        speed_method='unsmoothed backward centroid difference; first frame null',
        caveat='Offline continuity rule, not object classification or identity accuracy. Static false masks can pass.',
        tracklets=summaries)


def write(output, rows, report):
    (output/'tracklets.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    fields=['frame','worm_id','time_s']+[k for k in rows[0] if k.startswith('tracklet_')]
    with (output/'tracklet-metrics.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)

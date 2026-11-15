"""Point-track evidence for endpoint correspondence at fast turns."""
import json

import numpy as np
from scipy.spatial import cKDTree

def read_tracks(fitted, source):
    run = json.loads((fitted/'run.json').read_text())
    evidence = fitted/run['evidence_directory']
    meta = json.loads((evidence/'metadata.json').read_text())
    if (len(source['crop_xyxy']) != 4 or meta['crop_xyxy'] != source['crop_xyxy']
            or meta['frames'] != source['frames']):
        raise ValueError('Point tracking and video coordinates disagree')
    frames = [{direction: [] for direction in ('forward', 'backward')} for _ in range(source['frames'])]
    files = sorted(evidence.glob('tracks-*.npz'))
    if not files:
        raise ValueError('No TAPNext++ tracking windows saved')
    for file in files:
        with np.load(file, allow_pickle=False) as z:
            ids = z['frame_ids']
            origin = np.asarray(meta['tracking_roi'][:2]) + z['roi'][:2]
            descending = ids[0] > ids[-1]
            for key in ('forward', 'backward'):
                direction = ('forward' if key == 'backward' else 'backward') if descending else key
                anchor = int(ids[0] if key=='forward' else ids[-1])
                for kind, positions, visibility in [('query',key,key+'_visible'),
                        ('support',key+'_support_tracks',key+'_support_visible')]:
                    if positions not in z:
                        continue
                    points, flags = z[positions], z[visibility]
                    if points.shape != (*flags.shape,2) or len(flags) != len(ids):
                        raise ValueError(f'Malformed point array in {file.name}')
                    for frame, pts, vis in zip(ids,points,flags):
                        finite = np.isfinite(pts).all(axis=1)
                        if not finite.any():
                            continue
                        frames[frame][direction].append((file.stem, anchor, kind, np.flatnonzero(finite),
                                                         pts[finite]+origin, vis[finite]))
    if any(not (f['forward'] or f['backward']) for f in frames):
        raise ValueError('Missing TAPNext++ frame evidence')
    return frames


def reversal_votes(curve, anchor_curve, current, seeded, length):
    """Project identical query IDs onto both head-first arc-length coordinates."""
    _, _, _, ids, positions, visible = current
    _, _, _, seed_ids, seed_positions, seed_visible = seeded
    _, ci, ai = np.intersect1d(ids, seed_ids, return_indices=True)
    local_distance, s = cKDTree(curve).query(positions[ci])
    anchor_distance, s0 = cKDTree(anchor_curve).query(seed_positions[ai])
    good = visible[ci] & seed_visible[ai] & (local_distance < .04*length) & (anchor_distance < .04*length)
    s, s0 = s[good]/(len(curve)-1), s0[good]/(len(anchor_curve)-1)
    if len(s) < 8 or max(s.min(), s0.min()) > .25 or min(s.max(), s0.max()) < .75:
        return None
    keep = float(np.median(abs(s-s0)))
    swap = float(np.median(abs((1-s)-s0)))
    if swap > .15 or keep-swap < .20 or np.mean(abs(1-s-s0) < abs(s-s0)) < .75:
        return None
    return dict(visible_matched=int(len(s)), keep_arc_error=keep, swapped_arc_error=swap)


def reacquired_order(frame, candidate, following, history, tracks, length):
    """Challenge a geometry link when velocity and two successive query sets disagree.

    history contains oriented consecutive poses within the current endpoint-identity
    block. A query's original anatomical end is recovered only from its own seed.
    """
    if following is None or frame+1 >= len(tracks) or frame-2 not in history or frame-1 not in history:
        return None
    prediction = 2*history[frame-1] - history[frame-2]
    keep = np.linalg.norm(candidate-prediction, axis=1)
    swap = np.linalg.norm(candidate[::-1]-prediction, axis=1)
    if (min((keep-swap)[[0,-1]]) < .10*length or (keep-swap).mean() < .035*length):
        return None
    # Check the next frame only if its whole body is close and its order is clear.
    future = (following, following[::-1])
    costs = np.array([np.linalg.norm(q-candidate, axis=1).mean() for q in future])
    if costs.min() > .14*length or abs(costs[0]-costs[1]) < .12*length:
        return None
    next_curve = future[int(costs.argmin())]
    for direction, groups in tracks[frame].items():
        for current in groups:
            window, anchor, kind = current[:3]
            if kind != 'query' or anchor >= frame-1 or anchor not in history:
                continue
            def matching_group(i):
                return next((g for g in tracks[i][direction] if g[:3] == current[:3]), None)
            seed, next_group = matching_group(anchor), matching_group(frame+1)
            if seed is None or next_group is None:
                continue
            evidence = reversal_votes(candidate,history[anchor],current,seed,length)
            if evidence is None:
                continue
            next_evidence = reversal_votes(next_curve,history[anchor],next_group,seed,length)
            if next_evidence is not None:
                return dict(frame=frame, anchor=anchor, window=window, direction=direction,
                            current=evidence, following=next_evidence,
                            keep_prediction_px=keep[[0,-1]].tolist(),
                            swapped_prediction_px=swap[[0,-1]].tolist())
    return None

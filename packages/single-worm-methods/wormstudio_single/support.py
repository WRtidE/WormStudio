"""Deterministic auxiliary TAP queries; never exported as centerline points."""
import numpy as np


def support_points(points, mask, per_query=0, mode='local', radius=32., model_size=256):
    """Return local xy queries and their parent indices.

    Radius is the half-width of a square in model-input pixels, matching the
    official VOT local-grid convention. Body mode is our mask-constrained
    extension: select spread-out foreground locations inside that square.
    Nearby duplicate queries are omitted, so per_query is an upper bound.
    """
    if isinstance(per_query, bool) or not isinstance(per_query, (int, np.integer)) or not 0 <= per_query <= 64:
        raise ValueError('support count must be an integer from 0 to 64.')
    if mode not in ('local', 'body') or not np.isfinite(radius) or radius <= 0:
        raise ValueError('support mode must be local/body and radius must be positive.')
    points = np.asarray(points, dtype=float)
    mask = np.asarray(mask, dtype=bool)
    if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all() or mask.ndim != 2:
        raise ValueError('Support initialization requires finite xy points and a 2D mask.')
    if not per_query:
        return np.empty((0, 2), np.float32), np.empty(0, np.int32)
    h, w = mask.shape
    scale = np.array([w, h], dtype=float) / model_size
    query_model = points / scale
    selected = []; parents = []; occupied = list(query_model)
    cols = max(1, round(np.sqrt(per_query))) if mode == 'local' else max(24, int(np.ceil(np.sqrt(per_query)))*8)
    rows = int(np.ceil(per_query / cols)) if mode == 'local' else cols
    gx, gy = np.meshgrid((np.arange(cols)+.5)/cols*2*radius-radius,
                         (np.arange(rows)+.5)/rows*2*radius-radius)
    offsets = np.column_stack([gx.ravel(), gy.ravel()])
    for parent, center in enumerate(query_model):
        candidates = (offsets + center) * scale
        candidates = np.clip(candidates, [0., 0.], [w-1., h-1.])
        if mode == 'body':
            ij = np.rint(candidates).astype(int)
            candidates = candidates[mask[ij[:, 1], ij[:, 0]]]
        candidates = candidates / scale
        if not len(candidates): continue
        distance = np.min(np.sum((candidates[:, None]-np.asarray(occupied)[None])**2, axis=2), axis=1)
        for _ in range(per_query):
            eligible = np.flatnonzero(distance >= .75**2)
            if not len(eligible): break
            k = int(eligible[0]) if mode == 'local' else int(np.argmax(distance))
            point = candidates[k]
            selected.append(point*scale); parents.append(parent); occupied.append(point)
            distance = np.minimum(distance, np.sum((candidates-point)**2, axis=1))
    return np.asarray(selected, dtype=np.float32).reshape(-1, 2), np.asarray(parents, dtype=np.int32)

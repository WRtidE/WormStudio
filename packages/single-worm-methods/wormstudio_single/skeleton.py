"""Ordered skeleton adapter; the original algorithm functions are preserved."""
import math
import cv2
import numpy as np
from scipy import ndimage as ndi
from scipy.signal import savgol_filter
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize
from .common import resample
from .geometry import geometry_quality
RANDOM_SEED = 42
RANDOM_NODE_COUNT = 5
ORDERED_POINT_COUNT = 101
SMOOTH_WINDOW = 7
MIN_INSIDE = .97
MIN_SKELETON_COVERAGE = .90
MAX_TURN_DEGREES = 150

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

def body_path_source(geometry):
    """Classify the observed path while retaining the raw skeleton topology."""
    if not valid_line(geometry['ordered']):
        return None
    if geometry['topology'] == 'simple':
        return 'robust_medial'
    if geometry.get('short_spurs_only'):
        return 'medial_with_short_spurs'
    return None


def short_spurs_only(graph, coords, route, radii):
    """Check off-path branches in a connected, acyclic skeleton.

    Each branch must fit within one body radius of its attachment. Close to a
    tip the chosen arm must also be distinctly longer than the discarded arm.
    """
    if len(route) < .95 * len(coords):
        return False
    distance, _, attachment = dijkstra(graph, indices=route, min_only=True,
                                        return_predecessors=True)
    off_path = distance > 0
    junctions = attachment[off_path]
    along = np.r_[0., np.cumsum(np.linalg.norm(np.diff(coords[route], axis=0), axis=1))]
    to_tip = np.empty(len(coords))
    to_tip[route] = np.minimum(along, along[-1] - along)
    limit = np.minimum(np.median(radii), radii[junctions])
    return bool(np.all(distance[off_path] <= limit)
                and np.all(2 * distance[off_path] <= to_tip[junctions]))


def extract_niu(mask):
    """Exact skeletonize + seeded random nodes, with a separately named adapter.

    The adapter selects the longest finite endpoint geodesic in the largest
    connected skeleton component. It cannot infer an occluded tip or decide
    traversal order through a self-contact loop. Such cases remain flagged.
    """
    ys, xs = np.nonzero(mask)
    empty = dict(raw_xy=np.empty((0, 2), np.int32), nodes_xy=np.empty((0, 2), np.int32),
                 ordered=None, topology='empty', endpoints=0, branch_pixels=0,
                 components=0, cycles=0, endpoint_xy=np.empty((0, 2), np.int32), path_fraction=0., radius=1., centroid=[np.nan, np.nan])
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
    radii = ndi.distance_transform_edt(local)[sk]
    result = dict(raw_xy=xy, nodes_xy=nodes, ordered=None,
                  topology='simple' if simple else 'complex', endpoints=len(ends),
                  branch_pixels=int(np.sum(degree > 2)), components=count, cycles=cycles, endpoint_xy=xy[ends],
                  path_fraction=0., radius=float(np.median(radii)),
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
    if count == 1 and cycles == 0 and len(ends) > 2:
        result['short_spurs_only'] = short_spurs_only(graph, coords, route, radii)
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

def extract_body_geometry(mask):
    """Fit a medial path at two nearby scales when a contour forks.

    Keep the raw landmarks for coverage checks. A loop or disconnected mask
    goes directly to the contact solver. Only the candidate path is smoothed.
    """
    raw = extract_niu(mask)
    source = body_path_source(raw)
    if source or raw['components'] != 1 or raw['cycles'] or raw['endpoints'] < 3:
        return raw, source

    radius = raw['radius']
    y, x = np.nonzero(mask)
    offset = np.array([x.min(), y.min()])
    local = mask[y.min():y.max() + 1, x.min():x.max() + 1].astype(np.float32)
    pad = max(4, int(math.ceil(2.5 * radius)))
    local = np.pad(local, pad)
    original_tips = cKDTree(raw['endpoint_xy'])
    previous = None
    sigmas = sorted({max(.8, fraction * radius) for fraction in (.25, .40, .55)})
    for sigma in sigmas:
        smooth = cv2.GaussianBlur(local, (0, 0), sigma) > .5
        geometry = extract_niu(smooth)
        if body_path_source(geometry) is None:
            previous = None
            continue
        points = geometry['ordered'] + offset - pad
        if previous is not None:
            delta = np.linalg.norm(align(points, previous) - previous, axis=1)
            tip_distance, tip_index = original_tips.query(previous[[0, -1]])
            if (delta.mean() <= .5 * radius and delta.max() <= 1.5 * radius
                    and tip_index[0] != tip_index[1] and tip_distance.max() <= 1.5 * radius):
                # A thin, distant arm may disappear at both scales. Require every
                # observed terminal to stay close to the proposed body as well.
                farthest_tip = cKDTree(previous).query(raw['endpoint_xy'])[0].max()
                if farthest_tip <= 1.5 * radius:
                    support = line_metrics(previous - offset + pad, local,
                                           raw['raw_xy'] - offset + pad, radius)
                    if support['geometric_pass']:
                        return dict(raw, ordered=previous), 'scale_stable_medial'
        previous = points
    return raw, None


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

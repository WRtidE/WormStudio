"""Shared skeleton extraction, arc sampling, and volume geometry."""
import math

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree
from scipy.signal import savgol_filter
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from skimage.morphology import skeletonize


def resample(points, count=101):
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    keep = np.r_[True, np.diff(s) > 1e-8]
    if s[-1] < 1:
        raise ValueError('Degenerate centerline')
    return np.column_stack([np.interp(np.linspace(0, s[-1], count), s[keep], points[keep, j]) for j in (0, 1)])


def centerline(mask):
    """Longest geodesic between skeleton endpoints; distances include diagonals."""
    ys, xs = np.nonzero(mask)
    if len(xs) < 20:
        raise ValueError('Empty or very small mask')
    x0, y0 = max(0, xs.min()-3), max(0, ys.min()-3)
    local = mask[y0:ys.max()+4, x0:xs.max()+4]
    labels, n = ndimage.label(local)
    sizes = np.bincount(labels.ravel()); sizes[0] = 0
    local = labels == sizes.argmax()
    sk = skeletonize(local)
    coords = np.argwhere(sk)
    index = np.full(sk.shape, -1, int); index[sk] = np.arange(len(coords))
    rows, cols, weights = [], [], []
    for dy, dx in [(0, 1), (1, -1), (1, 0), (1, 1)]:
        q = coords + [dy, dx]
        valid = (q[:, 0]>=0)&(q[:, 0]<sk.shape[0])&(q[:, 1]>=0)&(q[:, 1]<sk.shape[1])
        a = np.flatnonzero(valid); b = index[q[valid,0], q[valid,1]]
        a, b = a[b>=0], b[b>=0]
        rows.extend(a); cols.extend(b); weights.extend([math.hypot(dx,dy)]*len(a))
        rows.extend(b); cols.extend(a); weights.extend([math.hypot(dx,dy)]*len(a))
    graph = coo_matrix((weights,(rows,cols)),shape=(len(coords),len(coords))).tocsr()
    ends = np.flatnonzero(np.diff(graph.indptr)==1)
    if len(ends)<2:
        raise ValueError('Closed/self-contact skeleton: no reliable pair of endpoints')
    distances, predecessors = dijkstra(graph, indices=ends, return_predecessors=True)
    end_dist = distances[:,ends]
    end_dist[~np.isfinite(end_dist)] = -1
    a,b = np.unravel_index(np.argmax(end_dist),end_dist.shape)
    node = int(ends[b]); route = [node]
    while node != ends[a]:
        node = int(predecessors[a,node])
        if node<0: raise ValueError('Disconnected centerline')
        route.append(node)
    points = coords[route][:,::-1].astype(float)
    # Extend both terminal tangents to the mask boundary to include the tips.
    for reverse in (False,True):
        if reverse: points=points[::-1]
        tangent=points[0]-points[min(8,len(points)-1)]
        tangent/=max(np.linalg.norm(tangent),1e-9)
        tip=points[0].copy()
        for step in np.arange(.5,35,.5):
            candidate=points[0]+step*tangent
            xi,yi=np.rint(candidate).astype(int)
            if not (0<=yi<local.shape[0] and 0<=xi<local.shape[1] and local[yi,xi]):break
            tip=candidate
        points=np.vstack([tip,points])
        if reverse: points=points[::-1]
    points=resample(points)
    smooth=savgol_filter(points,7,2,axis=0)
    smooth[0],smooth[-1]=points[0],points[-1]
    points=resample(smooth)
    dt=ndimage.distance_transform_edt(local)
    radius=ndimage.map_coordinates(dt,[points[:,1],points[:,0]],order=1,mode='constant')
    inside=ndimage.map_coordinates(local.astype(float),[points[:,1],points[:,0]],order=0,mode='constant')
    points += [x0,y0]
    return points,radius,dict(skeleton_endpoints=len(ends),
                            skeleton_path_fraction=len(route)/len(coords),
                            centerline_inside_fraction=float(inside.mean()))


def frustum_volume(points,radius):
    ds=np.linalg.norm(np.diff(points,axis=0),axis=1)
    return float(np.sum(np.pi*ds*(radius[:-1]**2+radius[:-1]*radius[1:]+radius[1:]**2)/3))


def silhouette_dimensions(mask, points, bounds):
    """Arc-average transverse width and integrate circular frusta, in pixel units."""
    x, y, w, h = bounds
    patch = np.pad(mask[y:y+h, x:x+w].astype(np.float32), 1)
    stations = points - [x-1, y-1]
    # A short chord stabilizes normals against a one-pixel staircase in the axis.
    index = np.arange(len(points))
    tangent = points[np.minimum(index+2, index[-1])] - points[np.maximum(index-2, 0)]
    norms = np.linalg.norm(tangent, axis=1)
    if np.any(norms == 0):
        return np.nan, np.nan
    normals = np.column_stack((-tangent[:, 1], tangent[:, 0])) / norms[:, None]
    center = ndimage.map_coordinates(patch, stations.T[::-1], order=1)
    if np.any(center[1:-1] < .5):
        return np.nan, np.nan
    # Trace both normals to the first half-intensity crossing. Nearby sections
    # of a curved body beyond that crossing do not contribute to the width.
    origins = np.tile(stations, (2, 1))
    rays = np.concatenate((normals, -normals))
    distances = np.zeros(len(rays))
    pending = np.flatnonzero(np.tile(center >= .5, 2))
    offsets = np.arange(33) / 2
    while len(pending):
        xy = origins[pending, None] + rays[pending, None] * offsets[None, :, None]
        values = ndimage.map_coordinates(patch, xy.transpose(2, 0, 1)[::-1], order=1)
        outside = values[:, 1:] < .5
        crossed = outside.any(axis=1)
        found = np.flatnonzero(crossed)
        before = outside[found].argmax(axis=1)
        a, b = values[found, before], values[found, before+1]
        distances[pending[found]] = offsets[before] + .5 * (a-.5) / (a-b)
        pending = pending[~crossed]
        offsets += 16
    width = distances.reshape(2, -1).sum(axis=0)
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    average = np.dot(steps, (width[1:] + width[:-1]) / 2) / steps.sum()
    return float(average), frustum_volume(points, width / 2)


def sampled(points):
    distance = np.r_[0., np.linalg.norm(np.diff(points, axis=0), axis=1).cumsum()]
    if not np.isfinite(distance).all() or distance[-1] < 1:
        return None
    keep = np.r_[True, np.diff(distance) > 0]
    return np.column_stack([np.interp(np.linspace(0, distance[-1], 101),
                                      distance[keep], axis[keep]) for axis in points.T])


def _free_tips(mask, length):
    """Return a padded skeleton, its local XY tips and their image offset."""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None, np.empty((0, 2), int), np.zeros(2, int)
    x, y = xs.min(), ys.min()
    patch = np.pad(mask[y:ys.max()+1, x:xs.max()+1], 1)
    components, _ = ndimage.label(patch, structure=np.ones((3, 3), bool))
    areas = np.bincount(components.ravel())[1:]
    if areas.max() < .75 * areas.sum():
        return None, np.empty((0, 2), int), np.zeros(2, int)
    skeleton = skeletonize(components == areas.argmax() + 1)
    degree = ndimage.convolve(skeleton.astype(np.uint8), np.ones((3, 3), np.uint8),
                              mode='constant') - skeleton
    tips = np.argwhere(skeleton & (degree == 1))
    forks = np.argwhere(skeleton & (degree > 2))
    if len(forks):
        tips = tips[cKDTree(forks).query(tips)[0] >= max(3., .03 * length)]
    offset = np.array([x-1, y-1])
    tips = tips[:, ::-1]
    xy = tips + offset
    margin = max(2., .01 * length)
    return skeleton, tips[np.all((xy >= margin) & (xy < np.array(mask.shape[::-1])-margin), axis=1)], offset


def visible_mask_tips(mask, length):
    """Find free skeleton tips on the main silhouette, away from forks and crop edges."""
    _, tips, offset = _free_tips(mask, length)
    return tips + offset


def visible_terminal_branches(mask, length, max_distance):
    """Trace inward from each free tip, stopping at the first fork or the distance limit."""
    skeleton, tips, offset = _free_tips(mask, length)
    branches = []
    for tip in tips:
        x, y = tip
        path = [(x,y)]
        previous = None
        distance = 0.
        while distance < max_distance:
            neighbors = [(x+dx,y+dy) for dy in (-1,0,1) for dx in (-1,0,1)
                         if (dy or dx) and skeleton[y+dy,x+dx] and (x+dx,y+dy) != previous]
            if len(neighbors) != 1:
                break
            previous = x,y
            x,y = neighbors[0]
            distance += math.hypot(x-previous[0],y-previous[1])
            path.append((x,y))
        branches.append(np.asarray(path) + offset)
    return branches

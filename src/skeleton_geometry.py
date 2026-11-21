"""Shared skeleton extraction, arc sampling, and volume geometry."""
import math

import numpy as np
from scipy import ndimage
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


def sampled(points):
    distance = np.r_[0., np.linalg.norm(np.diff(points, axis=0), axis=1).cumsum()]
    if not np.isfinite(distance).all() or distance[-1] < 1:
        return None
    keep = np.r_[True, np.diff(distance) > 0]
    return np.column_stack([np.interp(np.linspace(0, distance[-1], 101),
                                      distance[keep], axis[keep]) for axis in points.T])

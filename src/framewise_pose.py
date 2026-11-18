"""Single moving dark worm in a fixed field. All public coordinates are field-local XY."""
import cv2
import numpy as np

VERSION = 'registered-whole-worm-v2'


def arc_midpoint(line):
    """Interpolate at half the polyline length, including nonuniform samples."""
    points = np.asarray(line, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
        return None
    arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    if arc[-1] <= 0:
        return None
    return np.array([np.interp(arc[-1] / 2, arc, axis) for axis in points.T])


def observed_pose(mask):
    """Return a measured path only when supported by a continuous visible silhouette."""
    from analyze_phenotypes import centerline
    x, y, w, h = cv2.boundingRect(mask.astype(np.uint8))
    if min(w, h) < 3:
        return None, 'missing_mask'
    if x == 0 or y == 0 or x+w == mask.shape[1] or y+h == mask.shape[0]:
        return None, 'image_boundary_truncation'
    patch = mask[y:y+h, x:x+w].astype(np.uint8)
    ratio = min(1., 320 / max(w, h))
    if min(w,h)*ratio < 1:
        return None, 'unresolved_body_width'
    small = cv2.resize(patch, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_NEAREST_EXACT)
    padded = np.pad(small, 2)
    try:
        path, _, quality = centerline(padded > 0)
    except ValueError:
        return None, 'centerline_missing'
    if quality['skeleton_path_fraction'] < .80 or quality['centerline_inside_fraction'] < .97:
        return None, 'branched_or_contact_skeleton'
    if np.linalg.norm(path[0]-path[-1]) < .12*np.linalg.norm(np.diff(path, axis=0), axis=1).sum():
        return None, 'overlapping_tips'
    line = (path - 2 + .5) * [w / small.shape[1], h / small.shape[0]] + [x-.5, y-.5]
    # At a tapered tip the skeleton pixel is inside the resized mask, but its
    # mapped subpixel position can round just outside the native silhouette.
    tolerance = int(np.ceil(1 / ratio))
    for end in (0, -1):
        tx, ty = np.rint(line[end]).astype(int)
        if 0 <= ty < mask.shape[0] and 0 <= tx < mask.shape[1] and mask[ty,tx]:
            continue
        left, top = max(0,tx-tolerance), max(0,ty-tolerance)
        yy, xx = np.nonzero(mask[top:ty+tolerance+1, left:tx+tolerance+1])
        if not len(xx):
            return None, 'landmark_outside_mask'
        pixels = np.column_stack([xx+left, yy+top])
        distance = np.linalg.norm(pixels-line[end],axis=1)
        if distance.min() > tolerance:
            return None, 'landmark_outside_mask'
        line[end] = pixels[distance.argmin()]
    mid = arc_midpoint(line)
    index = np.rint(np.vstack([line[0], mid, line[-1]])).astype(int)
    if (index < 0).any() or (index >= mask.shape[::-1]).any() or not mask[index[:,1], index[:,0]].all():
        return None, 'landmark_outside_mask'
    return line, ''

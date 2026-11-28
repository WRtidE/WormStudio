"""Silhouette geometry primitives shared by the body-sequence solver."""
import cv2
import numpy as np
from scipy import ndimage as ndi

from centerline_topology import geometry_quality


def local_field(mask):
    left, top, width, height = cv2.boundingRect(mask.astype(np.uint8, copy=False))
    x, y = max(0, left - 8), max(0, top - 8)
    local = mask[y:top + height + 8, x:left + width + 8]
    return ndi.distance_transform_edt(local), np.array([x, y])


def sampled_mask_radii(mask, points, bounds, device):
    """Sample the exact Euclidean distance to background at subpixel body points."""
    x, y, w, h = bounds
    left, top = max(0, x-1), max(0, y-1)
    local = np.asarray(mask[top:y+h+1, left:x+w+1], dtype=bool)
    coords = (points - [left, top]).T[::-1]
    if device == 'cpu' or local.all():
        # SciPy defines distances to a virtual background pixel for a full mask.
        distances = ndi.distance_transform_edt(local)
    else:
        import torch
        # Only the four interpolation neighbours of each point need distances.
        # Their nearest background pixels lie on the outer rim of the silhouette.
        distances = np.zeros(local.shape, dtype=np.float64)
        corners = np.floor(coords.T).astype(np.int64)[:, None] + [[0,0], [0,1], [1,0], [1,1]]
        pixels = np.unique(np.clip(corners, 0, np.array(local.shape)-1).reshape(-1, 2), axis=0)
        pixels = pixels[local[tuple(pixels.T)]]
        if len(pixels):
            dilated = cv2.dilate(local.astype(np.uint8), np.ones((3,3), np.uint8))
            rim = np.argwhere(dilated.astype(bool) & ~local)
            if len(rim):
                target = torch.as_tensor(pixels, device=device)
                nearest = torch.full((len(pixels),), torch.iinfo(torch.int64).max, device=device)
                for start in range(0, len(rim), 1024):
                    edge = torch.as_tensor(rim[start:start+1024], device=device)
                    dy = target[:, None, 0] - edge[:, 0]
                    dx = target[:, None, 1] - edge[:, 1]
                    nearest = torch.minimum(nearest, (dx.square() + dy.square()).amin(1))
                distances[tuple(pixels.T)] = np.sqrt(nearest.cpu().numpy())
    return ndi.map_coordinates(distances, coords, order=1, mode='constant')


def candidate_score(candidate, mask, field, origin, length_ref, radius, tips, device='cpu'):
    p = candidate['points']; q = p - origin
    inside = ndi.map_coordinates(mask.astype(np.uint8), [p[:, 1], p[:, 0]], order=0, mode='constant').mean()
    length = float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    qa = geometry_quality(p,device=device)
    tipcost = 0
    if tips is not None:
        tipcost = min(np.linalg.norm(p[[0, -1]] - tips, axis=1).mean(),
                      np.linalg.norm(p[[0, -1]] - tips[::-1], axis=1).mean()) / radius
    # Penalize missed body coverage, not projected self-intersection.
    radius_points = ndi.map_coordinates(field, [q[:, 1], q[:, 0]], order=1, mode='constant')
    reconstruction = np.zeros(field.shape, np.uint8)
    for xy, r in zip(q, radius_points):
        cv2.circle(reconstruction, tuple(np.rint(xy).astype(int)), max(1, int(round(min(r, 1.3 * radius)))), 1, -1)
    truth = field > 0
    intersection = (truth & (reconstruction > 0)).sum()
    union = (truth | (reconstruction > 0)).sum()
    iou = float(intersection / max(union, 1))
    unary = (12 * (1 - inside) + 3 * (1 - iou) + 4 * abs(length / length_ref - 1)
             + .5 * min((qa['max_turn_5pct_deg'] / 90) ** 2, 16) + 2 * candidate['connection_cost']
             + .8 * min(tipcost, 5))
    candidate.update(unary=float(unary), inside_fraction=float(inside), reconstruction_iou=iou,
                     length_px=length, **qa)

"""Silhouette geometry primitives shared by the body-sequence solver."""
import cv2
import numpy as np
from scipy import ndimage as ndi

from pose.centerline_topology import geometry_quality


def local_field(mask):
    ys, xs = np.nonzero(mask)
    x = max(0, int(xs.min()) - 8); y = max(0, int(ys.min()) - 8)
    local = mask[y:int(ys.max()) + 9, x:int(xs.max()) + 9]
    return ndi.distance_transform_edt(local), np.array([x, y])


def candidate_score(candidate, mask, field, origin, length_ref, radius, tips):
    p = candidate['points']; q = p - origin
    inside = ndi.map_coordinates(mask.astype(np.uint8), [p[:, 1], p[:, 0]], order=0, mode='constant').mean()
    length = float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    qa = geometry_quality(p)
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

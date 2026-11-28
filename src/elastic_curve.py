"""Pillar-aware differentiable elastic centerline solver for coiled/self-crossing silhouettes."""
import cv2
import numpy as np
from scipy import ndimage as ndi

from skeleton_geometry import resample


def static_pillars(gray, worm_radius):
    """Detect static circular microfluidic pillars from the temporal median background."""
    if len(gray) < 3 or not np.isfinite(worm_radius) or worm_radius <= 0:
        return np.empty((0, 3), dtype=np.float32)
    step = max(1, len(gray) // 13)
    samples = [np.asarray(gray[i], dtype=np.uint8) for i in range(0, len(gray), step)[:13]]
    bg = np.median(np.stack(samples), axis=0).astype(np.uint8)
    h, w = bg.shape
    min_r = max(8, int(round(1.5 * worm_radius)))
    max_r = max(min_r + 4, int(round(5.5 * worm_radius)))
    if min(h, w) < 2 * min_r:
        return np.empty((0, 3), dtype=np.float32)
    blurred = cv2.GaussianBlur(bg, (5, 5), 1.0)
    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(16, int(round(2.4 * min_r))),
        param1=80, param2=28, minRadius=min_r, maxRadius=max_r,
    )
    if circles is None or len(circles[0]) < 3:
        return np.empty((0, 3), dtype=np.float32)
    pts = circles[0].astype(np.float32)
    r_med = float(np.median(pts[:, 2]))
    keep = (np.abs(pts[:, 2] - r_med) <= 0.18 * r_med)
    # Exclude border artifacts that clip filter gates or chip walls.
    margin = 1.15 * r_med
    keep &= (pts[:, 0] >= margin) & (pts[:, 0] <= w - margin) & (pts[:, 1] >= margin) & (pts[:, 1] <= h - margin)
    pts = pts[keep]
    if len(pts) < 3:
        return np.empty((0, 3), dtype=np.float32)
    return pts


def _resample_torch_count(points, count=51):
    return resample(np.asarray(points, dtype=float), count=count)


def _edge_distance_field(mask):
    """Positive distance outside the mask, negative inside (depth to boundary)."""
    inside = ndi.distance_transform_edt(mask).astype(np.float32)
    outside = ndi.distance_transform_edt(~mask).astype(np.float32)
    return outside, inside


def _bilinear_sample(field_2d, xy):
    """Differentiable bilinear sampling on a (H, W) float32 tensor without grid_sample."""
    import torch

    h, w = field_2d.shape
    x = xy[:, 0].clamp(0.0, float(w - 1))
    y = xy[:, 1].clamp(0.0, float(h - 1))
    x0 = torch.floor(x).long().clamp(0, max(0, w - 2))
    y0 = torch.floor(y).long().clamp(0, max(0, h - 2))
    x1 = (x0 + 1).clamp(max=w - 1)
    y1 = (y0 + 1).clamp(max=h - 1)
    fx = x - x0.float()
    fy = y - y0.float()

    v00 = field_2d[y0, x0]
    v10 = field_2d[y0, x1]
    v01 = field_2d[y1, x0]
    v11 = field_2d[y1, x1]
    return (
        (1.0 - fx) * (1.0 - fy) * v00
        + fx * (1.0 - fy) * v10
        + (1.0 - fx) * fy * v01
        + fx * fy * v11
    )


def deformable_fit(
    mask,
    init_points,
    length_ref,
    worm_radius,
    *,
    pillars=(),
    neighbor_points=None,
    device='cpu',
    steps=95,
):
    """Fit a smooth, arc-length-constrained centerline inside a coiled/crossing silhouette."""
    import torch

    ys, xs = np.nonzero(mask)
    if len(xs) < 20 or not np.isfinite(length_ref) or length_ref <= 0:
        return None

    r = max(2.0, float(worm_radius))
    pad = max(14, int(np.ceil(2.5 * r)))
    h_full, w_full = mask.shape
    x0 = max(0, int(xs.min()) - pad)
    y0 = max(0, int(ys.min()) - pad)
    x1 = min(w_full, int(xs.max()) + pad + 1)
    y1 = min(h_full, int(ys.max()) + pad + 1)
    local_mask = np.asarray(mask[y0:y1, x0:x1], dtype=bool)
    lh, lw = local_mask.shape
    if lh < 4 or lw < 4:
        return None

    outside_edt, inside_edt = _edge_distance_field(local_mask)

    # Subsample foreground pixels uniformly so coverage loss pulls curve into coiled loops.
    fg_yx = np.argwhere(local_mask)
    stride = max(1, len(fg_yx) // 180)
    fg_xy = fg_yx[::stride, ::-1].astype(np.float32)

    k_pts = 51
    seed = _resample_torch_count(init_points, count=k_pts).astype(np.float32) - np.array([x0, y0], dtype=np.float32)
    # Shift initialization toward current mask centroid if center jumped significantly,
    # keeping shape while bringing the curve inside the silhouette basin.
    mask_center = np.array([xs.mean() - x0, ys.mean() - y0], dtype=np.float32)
    seed_center = seed.mean(axis=0)
    shift = mask_center - seed_center
    if np.linalg.norm(shift) > 0.35 * r:
        seed = seed + 0.85 * shift

    torch_device = torch.device(device if device in ('cpu', 'cuda', 'mps') else 'cpu')
    # MPS requires float32 tensors throughout.
    out_t = torch.from_numpy(outside_edt).to(torch_device, dtype=torch.float32)
    in_t = torch.from_numpy(inside_edt).to(torch_device, dtype=torch.float32)
    fg_t = torch.from_numpy(fg_xy).to(torch_device, dtype=torch.float32)
    curve = torch.tensor(seed, device=torch_device, dtype=torch.float32, requires_grad=True)
    ref_t = torch.tensor(seed, device=torch_device, dtype=torch.float32)

    neighbor_t = None
    if neighbor_points is not None:
        nb = _resample_torch_count(neighbor_points, count=k_pts).astype(np.float32) - np.array([x0, y0], dtype=np.float32)
        neighbor_t = torch.from_numpy(nb).to(torch_device, dtype=torch.float32)

    local_pillars = None
    if len(pillars):
        p_arr = np.asarray(pillars, dtype=np.float32).copy()
        p_arr[:, 0] -= x0
        p_arr[:, 1] -= y0
        keep = (
            (p_arr[:, 0] + p_arr[:, 2] >= -pad) & (p_arr[:, 0] - p_arr[:, 2] <= lw + pad)
            & (p_arr[:, 1] + p_arr[:, 2] >= -pad) & (p_arr[:, 1] - p_arr[:, 2] <= lh + pad)
        )
        if keep.any():
            local_pillars = torch.from_numpy(p_arr[keep]).to(torch_device, dtype=torch.float32)

    optimizer = torch.optim.Adam([curve], lr=max(0.5, min(1.8, 0.14 * r)))
    scale_x = max(1.0, float(lw - 1))
    scale_y = max(1.0, float(lh - 1))

    for _ in range(steps):
        optimizer.zero_grad()
        dist_out = _bilinear_sample(out_t, curve)
        dist_in = _bilinear_sample(in_t, curve)
        out_bound = (
            torch.relu(-curve[:, 0]) + torch.relu(curve[:, 0] - scale_x)
            + torch.relu(-curve[:, 1]) + torch.relu(curve[:, 1] - scale_y)
        )
        loss_outside = ((dist_out + out_bound) / r).square().mean()
        loss_medial = -(torch.clamp(dist_in, max=1.4 * r) / r).mean()

        # Foreground coverage: every foreground pixel wants a nearby curve point.
        d2 = (fg_t[:, None, :] - curve[None, :, :]).square().sum(dim=-1)
        loss_cover = (d2.amin(dim=1) / (r * r)).mean()

        diffs = curve[1:] - curve[:-1]
        seg_lens = torch.sqrt((diffs * diffs).sum(dim=-1) + 1e-6)
        total_len = seg_lens.sum()
        mean_seg = total_len / (k_pts - 1)
        short_pen = torch.relu(0.76 * length_ref - total_len) / length_ref
        long_pen = torch.relu(total_len - 1.08 * length_ref) / length_ref
        loss_arc = (
            8.0 * (short_pen.square() + long_pen.square())
            + 0.45 * ((total_len - length_ref) / length_ref).square()
            + 0.45 * ((seg_lens - mean_seg) / r).square().mean()
        )

        unit_t = diffs / (seg_lens[:, None] + 1e-6)
        cos1 = (unit_t[:-1] * unit_t[1:]).sum(dim=-1)
        cos2 = (unit_t[:-2] * unit_t[2:]).sum(dim=-1)
        cos3 = (unit_t[:-3] * unit_t[3:]).sum(dim=-1)
        loss_turn = (
            10.0 * torch.relu(0.82 - cos1).square().mean()
            + 14.0 * torch.relu(0.50 - cos2).square().mean()
            + 18.0 * torch.relu(0.10 - cos3).square().mean()
        )

        second = curve[2:] - 2.0 * curve[1:-1] + curve[:-2]
        loss_bend = (second / r).square().mean()

        loss = 14.0 * loss_outside + 0.30 * loss_medial + 1.5 * loss_cover + loss_arc + 1.5 * loss_bend + loss_turn

        if local_pillars is not None:
            # Penalize curve points entering known static microfluidic pillars.
            p_dist = torch.sqrt(((curve[:, None, :] - local_pillars[None, :, :2]).square().sum(dim=-1)) + 1e-6)
            clearance = local_pillars[None, :, 2] - 0.15 * r
            penetration = torch.relu(clearance - p_dist) / r
            loss = loss + 6.0 * penetration.square().mean()

        if neighbor_t is not None:
            loss = loss + 0.08 * ((curve - neighbor_t) / r).square().mean()
        else:
            loss = loss + 0.04 * ((curve - ref_t) / r).square().mean()

        loss.backward()
        optimizer.step()

    result_local = curve.detach().cpu().numpy()
    if not np.isfinite(result_local).all():
        return None
    result_full = result_local + np.array([x0, y0], dtype=np.float32)
    try:
        from scipy.signal import savgol_filter
        if len(result_full) >= 7:
            result_full = savgol_filter(result_full.astype(float), window_length=7, polyorder=2, axis=0)
        return resample(result_full.astype(float), count=101)
    except ValueError:
        return None

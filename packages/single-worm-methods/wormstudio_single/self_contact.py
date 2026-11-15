"""Refit an ordered single-body pose through a bounded self-contact in mask coordinates."""

from dataclasses import dataclass, replace
from itertools import combinations, pairwise

import cv2
import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree, distance
from skimage.morphology import skeletonize

from .common import runs
from .contact import candidate_states
from .contact_bridge import split_tip_states
from .geometry import resample


@dataclass
class Observation:
    mask: np.ndarray
    offset: np.ndarray
    ridge: np.ndarray
    tips: np.ndarray
    joints: np.ndarray
    holes: np.ndarray
    valid: bool
    raw: np.ndarray | None = None
    residual: np.ndarray | None = None
    photo_offset: np.ndarray | None = None


def observe(mask, valid=True):
    """Represent a frame in a small padded box, retaining the camera coordinate transform."""
    x, y, w, h = cv2.boundingRect(mask.astype(np.uint8))
    local = np.pad(mask[y : y + h, x : x + w].astype(bool), 2)
    offset = np.array([x - 2, y - 2])
    ridge = skeletonize(local)
    degree = (
        ndimage.convolve(ridge.astype(int), np.ones((3, 3), int), mode="constant")
        - ridge
    )
    labels, n = ndimage.label(ridge & (degree > 2), np.ones((3, 3)))
    joints = np.array(
        [np.argwhere(labels == k).mean(0)[::-1] for k in range(1, n + 1)]
    ).reshape(-1, 2)
    tips = np.argwhere(ridge & (degree == 1))[:, ::-1]
    return Observation(
        local,
        offset,
        np.argwhere(ridge)[:, ::-1],
        tips,
        joints,
        ndimage.binary_fill_holes(local) & ~local,
        bool(valid and w and h),
    )


def length(p):
    return np.linalg.norm(np.diff(p, axis=0), axis=1).sum()


def sweep(p, center):
    v = p - center
    a = np.unwrap(np.arctan2(v[:, 1], v[:, 0]))
    return a[-1] - a[0]


def endpoint_rotation(a, b, center):
    u, v = a - center, b - center
    return np.arctan2(
        u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0], (u * v).sum(axis=-1)
    )


def common_background(frames, width):
    """Find a stationary background point inside the loop and clear on both boundaries."""
    origin = np.min([f.offset for f in frames], axis=0)
    limit = np.max([f.offset + f.mask.shape[::-1] for f in frames], axis=0)
    votes = np.zeros(tuple((limit - origin)[::-1]), np.int32)
    occupied = np.zeros_like(votes, bool)
    for i, f in enumerate(frames):
        x, y = f.offset - origin
        h, w = f.mask.shape
        region = np.s_[y : y + h, x : x + w]
        occupied[region] |= f.mask
        if 0 < i < len(frames) - 1:
            votes[region] += f.holes
    minimum_votes = max(1, int(np.ceil((len(frames) - 2) * 0.55)))
    common = (votes >= minimum_votes) & ~occupied
    labels, n = ndimage.label(common)
    centers = []
    for k in range(1, n + 1):
        region = labels == k
        if region.sum() < 0.6 * width**2:
            continue
        clearance = ndimage.distance_transform_edt(region)
        if clearance.max() >= 0.42 * width:
            y, x = np.unravel_index(clearance.argmax(), clearance.shape)
            centers.append(origin + [x, y])
    return centers


def lifted_routes(f, center, expected, width):
    """Shortest medial routes on angular sheets; crossing the ray updates the sheet."""
    tips = list(f.tips)
    joints = list(f.joints)
    # Too many independent forks have no single-junction interpretation.
    if len(tips) > 6 or len(joints) > 12:
        return []
    nodes = tips + joints
    if not nodes:
        # A ring with no visible seam supplies no observations of either tip.
        return []
    pixel = np.argwhere(f.mask)[:, ::-1]
    index = np.full(f.mask.shape, -1, int)
    index[pixel[:, 1], pixel[:, 0]] = np.arange(len(pixel))
    v = pixel + f.offset - center
    ang = np.mod(np.arctan2(v[:, 1], v[:, 0]), 2 * np.pi)
    node_indices = cKDTree(pixel).query(nodes)[1]
    tasks = []
    for a, b in combinations(range(len(nodes)), 2):
        if a >= len(tips) and len(tips):
            continue
        if np.linalg.norm(np.asarray(nodes[a]) - nodes[b]) < 0.14 * width:
            continue
        for i, j in ((a, b), (b, a)):
            sheet = round(
                (expected - (ang[node_indices[j]] - ang[node_indices[i]])) / (2 * np.pi)
            )
            tasks.append((i, j, sheet))
    if not tasks:
        return []
    radius = max(1, max(abs(k) for _, _, k in tasks))
    n = len(pixel)
    dt = ndimage.distance_transform_edt(f.mask)
    rho = 1 + 0.55 * width / (1 + dt) ** 1.25
    rows, cols, weights = [], [], []
    for dx, dy in ((1, 0), (0, 1), (1, 1), (-1, 1)):
        to = index[pixel[:, 1] + dy, pixel[:, 0] + dx]
        a = np.flatnonzero(to >= 0)
        b = to[a]
        step = -np.rint((ang[b] - ang[a]) / (2 * np.pi)).astype(int)
        cost = (
            0.5
            * np.hypot(dx, dy)
            * (rho[pixel[a, 1], pixel[a, 0]] + rho[pixel[b, 1], pixel[b, 0]])
        )
        for level in range(2 * radius + 1):
            keep = (level + step >= 0) & (level + step <= 2 * radius)
            rows.append(a[keep] + level * n)
            cols.append(b[keep] + (level + step[keep]) * n)
            weights.append(cost[keep])
    a, b, c = map(np.concatenate, (rows, cols, weights))
    shape = ((2 * radius + 1) * n,) * 2
    graph = coo_matrix((np.r_[c, c], (np.r_[a, b], np.r_[b, a])), shape=shape).tocsr()
    result = []
    for i in sorted({i for i, _, _ in tasks}):
        start = node_indices[i] + radius * n
        costs, prev = dijkstra(graph, indices=start, return_predecessors=True)
        for _, j, k in (t for t in tasks if t[0] == i):
            stop = node_indices[j] + (k + radius) * n
            if not np.isfinite(costs[stop]) or start == stop:
                continue
            path = [stop]
            while path[-1] != start:
                path.append(prev[path[-1]])
            raw = pixel[np.asarray(path[::-1]) % n].astype(float)
            smooth = ndimage.gaussian_filter1d(
                raw, max(0.6, 0.085 * width), axis=0, mode="nearest"
            )
            smooth[[0, -1]] = raw[[0, -1]]
            if length(smooth) > 1:
                result.append(
                    (
                        resample(smooth) + f.offset,
                        [i < len(tips), j < len(tips)],
                        "loop_route",
                    )
                )
    return result


def loop_review_states(frame, center, ref, width, extra=()):
    """Order-independent shape proposals, retaining alternatives for sequence fitting."""
    mask_frame = replace(frame, residual=None)
    states = []
    routes = lifted_routes(mask_frame, center, 2 * np.pi, width)
    for curve, visible, name in [*routes, *extra]:
        if abs(sweep(curve, center)) < np.pi:
            continue
        if sweep(curve, center) < 0:
            curve, visible = curve[::-1], visible[::-1]
        for path, tips in ((curve, visible), (curve[::-1], visible[::-1])):
            state = qualified(mask_frame, path, tips, name, ref, width)
            if state is None:
                continue
            state["cost"] += sum(not tip for tip in tips)
            states.append(state)
    return states


def review_interval(frames, curves, start, end, center, ref, width, guide_fit):
    """Fit display geometry across both open boundaries, allowing unknown end identity."""
    def boundary(i, curve):
        return qualified(replace(frames[i], residual=None), curve, [True, True],
                         "boundary", ref, width)

    first = boundary(start, curves[start])
    right = [boundary(end, p) for p in (curves[end], curves[end][::-1])]
    if first is None or any(state is None for state in right):
        return None
    groups = [[first]]
    for i in range(start + 1, end):
        f = frames[i]
        extra = []
        if guide_fit is not None:
            guide = guide_fit(f.mask, curves[start] - f.offset, curves[end] - f.offset,
                              (i - start) / (end - start))
            if guide is not None:
                curve = resample(guide) + f.offset
                seen = (distance.cdist(curve[[0, -1]] - f.offset, f.tips).min(1) < .5 * width
                        if len(f.tips) else [False, False])
                extra.append((curve, seen, "guided_fit"))
        states = loop_review_states(f, center, ref, width, extra)
        if not states:
            return None
        groups.append(states)
    groups.append(right)
    solution = choose_sequence(groups, ref, center)
    if solution is None:
        return None
    indices, _, _ = solution
    return [group[index] for group, index in zip(groups, indices)], bool(indices[-1])


def clear_frame(f, p, body_length, width):
    if (
        not f.valid
        or not np.isfinite(p).all()
        or not 0.72 < length(p) / body_length < 1.30
    ):
        return False
    if f.holes.sum() > 0.6 * width**2 or len(f.tips) < 2:
        return False
    matches = distance.cdist(p[[0, -1]] - f.offset, f.tips)
    if np.max(matches.min(1)) > width or np.unique(matches.argmin(1)).size != 2:
        return False
    # A tight U can have near tips without touching; only flag a nonlocal collision.
    sep = distance.cdist(p[::2], p[::2])
    return not np.any(np.triu(sep < 0.7 * width, k=9))


def extend_visible(f, p, width):
    """Extend a tapered mask terminal while successive strips have positive foreground contrast."""
    direction = p[-1] - p[-5]
    direction /= max(np.linalg.norm(direction), 1e-8)
    normal = direction[::-1] * [-1, 1]
    along = np.arange(1, max(2, round(2.8 * width)))
    across = np.linspace(-0.45 * width, 0.45 * width, 31)
    points = (
        p[-1]
        - f.photo_offset
        + along[:, None, None] * direction
        + across[None, :, None] * normal
    )
    contrast = ndimage.map_coordinates(
        f.residual, points.transpose(2, 0, 1)[::-1], order=1, mode="constant"
    )
    offset = 0.0
    extra = []
    miss = 0
    for step, values in zip(along, contrast):
        nearby = np.abs(across - offset) < max(1.25, 0.13 * width)
        weight = np.maximum(values - 6.0, 0) * np.exp(
            -0.5 * ((across - offset) / max(1.0, 0.15 * width)) ** 2
        )
        if values[nearby].mean() < 8 or weight.sum() < 12:
            miss += 1
            if miss >= 3:
                break
            continue
        miss = 0
        offset += np.clip(weight @ across / weight.sum() - offset, -0.4, 0.4)
        extra.append(p[-1] + step * direction + offset * normal)
    return resample(np.vstack([p, extra])) if extra else p


def appearance(f, p, width):
    if f.raw is None:
        return None
    tangent = np.gradient(p, axis=0)
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-8)
    normal = tangent[:, ::-1] * [-1, 1]
    points = (
        p[None]
        - f.photo_offset
        + np.array([-0.18, 0.0, 0.18])[:, None, None] * width * normal
    )
    profile = ndimage.map_coordinates(
        f.raw, points.transpose(2, 0, 1)[::-1], order=1, mode="nearest"
    )
    profile = ndimage.gaussian_filter1d(np.median(profile.astype(float), axis=0), 2)
    return np.clip(
        (profile - np.median(profile))
        / max(8.0, np.percentile(profile, 85) - np.percentile(profile, 15)),
        -2,
        2,
    )


def qualified(f, p, seen, source, ref, width):
    if not np.isfinite(p).all():
        return None
    total = length(p)
    if not 0.68 * ref <= total <= 1.32 * ref:
        return None
    local = p - f.offset
    dense = resample(local, max(101, int(total * 1.5)))
    inside = ndimage.map_coordinates(
        f.mask.astype(np.uint8), dense.T[::-1], order=0, mode="constant"
    ).mean()
    if inside < 0.975:
        return None
    tangent = p[5:] - p[:-5]
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-9)
    bends = np.arccos(np.clip((tangent[5:] * tangent[:-5]).sum(1), -1, 1))
    if bends.max() > np.deg2rad(140):
        return None
    residual = cKDTree(dense).query(f.ridge)[0]
    if np.mean(residual < 0.7 * width) < 0.80:
        return None
    visible = np.asarray(seen, bool).copy()
    for j, k in enumerate((0, -1)):
        if visible[j]:
            near = np.linalg.norm(f.tips - local[k], axis=1)
            nonlocal_body = p[14:] if j == 0 else p[:-14]
            visible[j] = (
                len(near) > 0
                and near.min() < 0.75 * width
                and np.linalg.norm(nonlocal_body - p[k], axis=1).min() > 1.05 * width
            )
    if source != "boundary" and f.residual is not None:
        if visible[0]:
            p = extend_visible(f, p[::-1], width)[::-1]
        if visible[1]:
            p = extend_visible(f, p, width)
        total = length(p)
        if total > 1.32 * ref:
            return None
        tangent = p[5:] - p[:-5]
        tangent /= np.maximum(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-9)
        bends = np.arccos(np.clip((tangent[5:] * tangent[:-5]).sum(1), -1, 1))
        if bends.max() > np.deg2rad(140):
            return None
    cost = (abs(total / ref - 1) / 0.13) ** 2 + 0.7 * np.mean(
        np.minimum(residual / width, 2)
    )
    cost += 0.65 * (bends.max() / np.pi) ** 2 + 5 * (1 - inside)
    return {
        "p": p,
        "visible": visible,
        "cost": float(cost),
        "name": source,
        "tangent": tangent,
        "profile": appearance(f, p, width),
    }


def proposals(f, prior, seen, ref, width, center, expected, guide=None):
    raw = []
    if np.isfinite(prior).all():
        raw += [(prior, seen, "input"), (prior[::-1], seen[::-1], "input")]
    if center is not None:
        raw += lifted_routes(f, center, expected, width)
    else:
        native = candidate_states(f.mask, ref) or split_tip_states(f.mask, ref)
        raw += [
            (s["points"] + f.offset, s["endpoint_observed"], s["name"]) for s in native
        ]
        if guide is not None:
            fit, left, right, fraction = guide
            path = fit(f.mask, left - f.offset, right - f.offset, fraction)
            if path is not None:
                p = resample(path) + f.offset
                for curve in (p, p[::-1]):
                    supported = (
                        distance.cdist(curve[[0, -1]] - f.offset, f.tips).min(1)
                        < 0.4 * width
                        if len(f.tips)
                        else [False, False]
                    )
                    raw.append((curve, supported, "guided_fit"))
    states = []
    for p, observed, source in raw:
        if center is not None and abs(sweep(p, center) - expected) > 1.5 * np.pi:
            continue
        state = qualified(f, p, observed, source, ref, width)
        if state is None:
            continue
        duplicate = next(
            (
                j
                for j, s in enumerate(states)
                if np.linalg.norm(s["p"] - state["p"], axis=1).max() < 0.25 * width
            ),
            None,
        )
        if duplicate is None:
            states.append(state)
        elif state["cost"] < states[duplicate]["cost"]:
            states[duplicate] = state
    return states


def choose_sequence(states, ref, center):
    points = [np.array([s["p"] for s in group]) for group in states]
    unary = [np.array([s["cost"] for s in group]) for group in states]
    for i, group in enumerate(states[1:-1], 1):
        a = states[0][0]["profile"]
        b = states[-1][0]["profile"]
        if a is not None and b is not None:
            t = i / (len(states) - 1)
            template = (1 - t) * a + t * b
            unary[i] += np.array(
                [1.5 * np.abs(s["profile"] - template).mean() for s in group]
            )
    edges = []
    for i, (a, b) in enumerate(pairwise(points)):
        delta = np.linalg.norm(a[:, None] - b[None], axis=-1) / (0.10 * ref)
        ta = np.array([s["tangent"] for s in states[i]])
        tb = np.array([s["tangent"] for s in states[i + 1]])
        change = np.square(ta[:, None] - tb[None]).mean(axis=(-1, -2))
        cost = delta.mean(-1) + 0.25 * delta[:, :, [0, -1]].mean(-1) + 1.4 * change
        # A unique graph route can still be physically discontinuous. Remove transitions
        # that would relocate an end or deform most of the body in a single frame.
        displacement = (a[:, None] - b[None]) / ref
        deformation = np.linalg.norm(
            displacement - displacement.mean(axis=2, keepdims=True), axis=-1
        )
        cost[
            (deformation.mean(-1) > 0.27)
            | (deformation[:, :, [0, -1]].max(-1) > 0.48)
            | (delta.mean(-1) > 5.5)
        ] = np.inf
        if center is not None:
            # Subtract the ends' actual angular displacement so crossing a
            # coordinate ray does not look like a change of physical loop order.
            angles = endpoint_rotation(a[:, None, [0, -1]], b[None, :, [0, -1]], center)
            diff = (
                np.array([sweep(p, center) for p in b])[None]
                - np.array([sweep(p, center) for p in a])[:, None]
            )
            cost[np.abs(diff - (angles[:, :, 1] - angles[:, :, 0])) > np.pi] = np.inf
        edges.append(cost)
    left = [unary[0]]
    back = []
    for edge, c in zip(edges, unary[1:]):
        scores = left[-1][:, None] + edge
        back.append(scores.argmin(0))
        left.append(scores.min(0) + c)
    end = int(left[-1].argmin())
    if not np.isfinite(left[-1][end]):
        return None
    selected = [end]
    for ptr in reversed(back):
        selected.append(int(ptr[selected[-1]]))
    selected.reverse()
    right = [np.zeros(len(unary[-1]))]
    for edge, c in zip(reversed(edges), reversed(unary[1:])):
        right.append((edge + (c + right[-1])[None]).min(1))
    margins = []
    for i, (choice, future) in enumerate(zip(selected, reversed(right))):
        total = left[i] + future
        displacement = np.linalg.norm(points[i] - points[i][choice], axis=-1)
        different = (displacement.mean(-1) > 0.055 * ref) | (
            displacement[:, [0, -1]].max(-1) > 0.12 * ref
        )
        margins.append(
            float(total[different].min() - total[choice]) if different.any() else np.inf
        )
    return selected, margins, float(left[-1][end])


def solve_interval(
    frames, curves, visible, start, end, ref, width, center, reverse_right, guide_fit
):
    left = curves[start]
    right = curves[end][::-1] if reverse_right else curves[end]
    states = []
    for i in range(start, end + 1):
        if i in (start, end):
            line = left if i == start else right
            seen = visible[i][::-1] if i == end and reverse_right else visible[i]
            state = qualified(frames[i], line, seen, "boundary", ref, width)
            choices = [state] if state is not None else []
        else:
            fraction = (i - start) / (end - start)
            expected = (
                ((1 - fraction) * sweep(left, center) + fraction * sweep(right, center))
                if center is not None
                else None
            )
            guide = (
                (guide_fit, left, right, fraction) if guide_fit is not None else None
            )
            choices = proposals(
                frames[i], curves[i], visible[i], ref, width, center, expected, guide
            )
        if not choices:
            return None
        states.append(choices)
    path = choose_sequence(states, ref, center)
    if path is None:
        return None
    ids, margins, cost = path
    return [s[k] for s, k in zip(states, ids)], margins, cost


def continued_order_stop(curves, valid, segments, start, reference):
    stop = start + 1
    while stop < len(valid):
        if segments is not None and segments[stop] != segments[start]:
            break
        if valid[stop]:
            stop += 1
            continue
        after = stop
        while after < len(valid) and not valid[after] and after - stop < 3:
            after += 1
        if after >= len(valid) or after - stop > 2 or not valid[after]:
            break
        if segments is not None and len(set(segments[stop : after + 1])) != 1:
            break
        a, b = curves[stop - 1], curves[after]
        shift = (b - a).mean(0)
        if not np.isfinite(shift).all() or np.linalg.norm(shift) > 0.55 * reference:
            break
        direct = np.linalg.norm(a + shift - b, axis=1)
        swapped = np.linalg.norm(a + shift - b[::-1], axis=1)
        if (
            direct.mean() > 0.16 * reference
            or direct[[0, -1]].max() > 0.30 * reference
            or swapped.mean() - direct.mean() < 0.10 * reference
        ):
            break
        stop = after + 1
    return stop


class InsufficientOpenPoses(ValueError):
    """The movie lacks enough separated poses to estimate the worm's body size."""


def reconstruct(frames, xy, visible, segments=None, protected=(), guide_fit=None):
    """Input curve order on the two clear boundaries defines correspondence (head or A)."""
    xy = np.asarray(xy, float)
    visible = np.asarray(visible, bool)
    if xy.shape != (len(frames), 101, 2) or visible.shape != (len(frames), 2):
        raise ValueError(
            "Expected one ordered 101-point curve and two visibility flags per mask."
        )
    if segments is not None and np.shape(segments) != (len(frames),):
        raise ValueError("Expected an identity segment for every frame.")
    sizes = [
        (length(p), f.mask.sum() / length(p))
        for f, p in zip(frames, xy)
        if f.valid
        and np.isfinite(p).all()
        and length(p) > 1
        and len(f.tips) >= 2
        and not f.holes.any()
    ]
    if len(sizes) < 3:
        raise InsufficientOpenPoses("At least three open poses are needed to estimate body size.")
    ref, width = np.median(sizes, axis=0)
    clear = np.array([clear_frame(f, p, ref, width) for f, p in zip(frames, xy)])
    result = xy.copy()
    observed = visible.copy()
    status = np.where(clear, "input", "review").astype("U20")
    events = []
    valid = np.array([f.valid for f in frames])
    status[~valid] = "invalid_mask"
    for group in runs(~clear & valid):
        start, end = int(group[0] - 1), int(group[-1] + 1)
        event = {
            "first": start + 1,
            "last": end - 1,
            "method": "temporal",
            "reason": None,
        }
        events.append(event)
        if start < 0 or end >= len(frames) or not clear[start] or not clear[end]:
            event["reason"] = "missing_boundary"
            continue
        if segments is not None and len(set(segments[start : end + 1])) != 1:
            event["reason"] = "identity_change"
            continue
        if any(start < i < end for i in protected):
            event["reason"] = "manual_anchor_in_event"
            continue
        centers = common_background(frames[start : end + 1], width)
        if len(centers) > 1:
            event["reason"] = "multiple_persistent_loops"
            continue
        center = centers[0] if centers else None
        if center is not None:
            event.update(method="loop", center_xy=center.tolist())
        option = solve_interval(
            frames, result, observed, start, end, ref, width, center, False, guide_fit
        )
        if option is None and center is not None:
            stop = continued_order_stop(result, valid, segments, end, ref)
            # A later loop must resolve its own branch ordering. A click beyond
            # that loop must not veto correction of the current open segment.
            stop = next((i for i in range(end + 1, stop)
                         if not clear[i] and frames[i].holes.sum() > 0.6 * width**2), stop)
            if not any(end <= i < stop for i in protected):
                # Correct a downstream parity error only if the input boundary order
                # actually violates winding conservation, not just because its fit failed.
                a, b = result[start], result[end]
                angles = endpoint_rotation(a[[0, -1]], b[[0, -1]], center)
                if (
                    sweep(a, center) * sweep(b, center) < 0
                    and abs(sweep(b, center) - sweep(a, center) - np.diff(angles)[0])
                    > np.pi
                ):
                    option = solve_interval(
                        frames,
                        result,
                        observed,
                        start,
                        end,
                        ref,
                        width,
                        center,
                        True,
                        guide_fit,
                    )
                    if option is not None:
                        result[end:stop] = result[end:stop, ::-1].copy()
                        observed[end:stop] = observed[end:stop, ::-1].copy()
                        status[end:stop] = np.where(
                            status[end:stop] == "input",
                            "input_reordered",
                            status[end:stop],
                        )
                        event["reordered_frames"] = [end, stop - 1]
        if option is None:
            event["reason"] = "no_supported_connection"
            continue
        path, margins, _cost = option
        for step, i in enumerate(group, 1):
            choice = path[step]
            result[i] = choice["p"]
            observed[i] = choice["visible"]
            supported = margins[step] >= 0.35 and choice["visible"].any()
            status[i] = "inferred" if supported else "review_candidate"
        event["review_frames"] = [
            int(i) for i in group if status[i] == "review_candidate"
        ]
    # Fill diagnostic geometry only after all sequence decisions, so a locally
    # plausible route cannot become the boundary of another measurement.
    for event in events:
        if event["reason"] != "no_supported_connection" or event["method"] != "loop":
            continue
        start, end = event["first"] - 1, event["last"] + 1
        review = review_interval(frames, result, start, end, np.asarray(event["center_xy"]),
                                 ref, width, guide_fit)
        if review is None:
            continue
        path, reversed_right = review
        event["review_anchors"] = [start, end]
        event["review_right_reversed"] = reversed_right
        for i, state in enumerate(path[1:-1], start + 1):
            result[i] = state["p"]
            status[i] = "loop_review"
            observed[i] = False
    observed[(status == "review") | ~valid] = False
    return (
        result,
        observed,
        status,
        events,
        {"length_px": float(ref), "width_px": float(width)},
    )

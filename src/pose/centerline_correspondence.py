"""Recover point order when a worm slides along a partly shared path in the image."""
import numpy as np

from head_tail.head_temporal import MAX_INTERVAL_S


def overlap_link(a, b):
    """Compare both directions at different arc-length offsets on the fitted midlines."""
    size = min(len(a), 51)
    if size < 7:
        return None
    sample = np.rint(np.linspace(0, len(a)-1, size)).astype(int)
    a, b = a[sample], b[sample]
    lengths = [np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in (a, b)]
    scale = min(lengths)
    if not np.isfinite(lengths).all() or scale <= 0 or scale < .75 * max(lengths):
        return None

    def tangents(p):
        t = np.gradient(p, axis=0)
        return t / np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)

    # Require at least 40% overlap. The offset is along the body; the
    # coordinates stay in the image so distinct nearby paths can be rejected.
    max_shift = int(.6 * (size-1))
    indexes = np.arange(size) + np.arange(-max_shift, max_shift+1)[:, None]
    inside = (indexes >= 0) & (indexes < size)
    count = inside.sum(axis=1)
    indexes = indexes.clip(0, size-1)
    ta, tb = tangents(a), tangents(b)
    scores = []
    for q, tangent in ((b, tb), (b[::-1], -tb[::-1])):
        delta = a - q[indexes]
        error = np.sqrt((np.sum(delta**2, axis=2) * inside).sum(axis=1) / count) / scale
        cosine = (np.sum(ta * tangent[indexes], axis=2) * inside).sum(axis=1) / count
        cost = error + .12 * (1 - cosine)
        k = int(cost.argmin())
        scores.append((cost[k], error[k], cosine[k], count[k]/size))
    flip = int(scores[1][0] < scores[0][0])
    cost, error, cosine, coverage = scores[flip]
    margin = scores[1-flip][0] - cost
    if error > .055 or cosine < .85 or margin < .08:
        return None
    return dict(flip=flip, rms_bl=float(error), overlap=float(coverage), margin=float(margin))


def curve_links(poses, usable, visible, identities, fps, bridgeable, anchors):
    """Return local anatomical correspondences, without assigning anatomy to either end."""
    links = {}
    if not np.isfinite(fps) or fps <= 0:
        return links
    clear = usable & visible.all(axis=1)
    ids = np.flatnonzero(clear)
    for i, j in zip(ids, ids[1:]):
        if (j-i)/fps > MAX_INTERVAL_S or not (identities[i:j+1] == identities[i]).all():
            continue
        gap = j > i+1
        if gap and (bridgeable is None or not bridgeable[i+1:j].all()
                    or usable[i+1:j].any() or any(i < anchor < j for anchor in anchors)):
            continue
        match = overlap_link(poses[i], poses[j])
        if match is None:
            continue
        refs = [int(i), int(j)]
        if gap:
            # Check the correspondence independently with a third clear frame.
            # The missing pose never supplies coordinates or receives a label.
            corroborated = []
            for k in (i-1, j+1):
                if not 0 <= k < len(poses) or not clear[k] or identities[k] != identities[i]:
                    continue
                first = links.get((k, i)) if k < i else overlap_link(poses[i], poses[k])
                second = overlap_link(poses[k], poses[j])
                if first is not None and second is not None:
                    corroborated.append(first['flip'] ^ second['flip'])
                    refs.append(int(k))
            if not corroborated or any(order != match['flip'] for order in corroborated):
                continue
        links[i, j] = dict(match, frames=sorted(refs))
    return links


def constrain_trajectory_proposals(rows, links, established):
    """Enforce physical-end continuity on labels added by the individual-frame pass."""
    chains = []
    for (i, j), match in links.items():  # curve_links emits disjoint chronological paths
        if not chains or chains[-1][-1][0] != i:
            chains.append([(i, 0)])
        chains[-1].append((j, chains[-1][-1][1] ^ match['flip']))
    for chain in chains:
        labelled = [(i, parity) for i, parity in chain if rows[i]['head_endpoint'] is not None]
        orders = {rows[i]['head_endpoint'] ^ parity for i, parity in labelled}
        if len(orders) < 2:
            continue
        fixed = [(i, rows[i]['head_endpoint'] ^ parity) for i, parity in labelled if established[i]]
        manual = [(i, direction) for i, direction in fixed if rows[i]['head_identity_confirmed']]
        fixed = manual or fixed
        fixed_orders = {direction for _, direction in fixed}
        for pos, (i, parity) in enumerate(chain):
            row = rows[i]
            old = row['head_endpoint']
            if old is None:
                continue
            old_reason = row['head_reason']
            if len(fixed_orders) == 1:
                seed, direction = fixed[0]
                head = direction ^ parity
                if head == old:
                    continue
                edge = (chain[pos-1][0], i) if seed < i else (i, chain[pos+1][0])
                row.update(head_endpoint=head, head_status='unconfirmed_temporal_candidate',
                           head_reason='centerline_overlap_orientation', head_temporal_seed_frame=int(seed),
                           head_trajectory_frames=links[edge]['frames'],
                           head_temporal_link_source='centerline_overlap',
                           head_evidence_score=rows[seed]['head_evidence_score'])
            else:
                row.update(head_endpoint=None, head_status='unconfirmed_A_B_endpoints',
                           head_reason='endpoint_continuity_conflict', head_temporal_seed_frame=None,
                           head_evidence_score=0.)
            row.update(head_local_endpoint=old, head_local_reason=old_reason, head_temporal_changed=True,
                       head_temporal_conflict=True, head_identity_confirmed=False)

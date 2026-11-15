"""Offline endpoint correspondence from short tracks on either side of a posture change."""
import numpy as np

from head_temporal import MAX_INTERVAL_S


def predict_order(curves, seen, history, target, fps, cfg):
    """Compare the two orders in target using visible samples in history (nearest last)."""
    history = [k for k in history if 0 < abs(k - target) / fps <= .6]
    if len(history) < 3:
        return None
    # Sample the measured body next to each tip too. If both tips disappear,
    # only the interior arms can support a retrospective ordering.
    interior = not seen[target].any()
    fractions = [.12, .20, .28] if interior else [0., .05, .10]
    samples = np.rint(np.array(fractions) * (curves.shape[1] - 1)).astype(int)
    arms = np.stack((samples, curves.shape[1] - 1 - samples))
    costs = np.full((2, 2), np.nan)
    used = set()
    for end in range(2):
        ids = [k for k in history if seen[k, end]][-5:]
        if len(ids) < 3 or abs(target - ids[-1]) / fps > MAX_INTERVAL_S:
            continue
        scale = np.median(np.linalg.norm(np.diff(curves[ids], axis=1), axis=2).sum(axis=1))
        centers = curves[ids].mean(axis=1)
        estimated_shift = curves[target].mean(axis=0) - centers[-1]
        if scale <= 0 or np.linalg.norm(estimated_shift) > .5 * scale:
            continue
        points = curves[ids][:, arms[end]]
        # Follow both the observed translation of the animal and the tip's velocity
        # relative to its body. Damping limits overshoot at a change of direction.
        relative = points - centers[:, None]
        velocity = np.median(np.diff(relative, axis=0) / np.diff(ids)[:, None, None], axis=0)
        estimate = points[-1] + estimated_shift + .5 * (target - ids[-1]) * velocity
        residual = np.linalg.norm(curves[target][arms] - estimate, axis=-1) / scale
        costs[end] = np.sqrt(np.mean(residual**2, axis=1))
        used.update(ids)
    measured = np.ones(2, bool) if interior else seen[target]
    costs[:, ~measured] = np.nan
    pairs = np.array([costs.diagonal(), costs[:, ::-1].diagonal()])
    counts = np.isfinite(pairs).sum(axis=1)
    if counts[0] == 0 or counts[0] != counts[1]:
        return None
    scores = np.nansum(pairs, axis=1) / counts
    order = int(scores[1] < scores[0])
    margin = scores[1-order] - scores[order]
    worst = np.nanmax(pairs[order])
    limit = cfg['max_step_bl']
    strict = worst <= limit
    # A bigger displacement is only a proposal for the opposite time direction
    # to verify. Each measured tip must prefer the same order by itself.
    if margin < cfg['min_assignment_margin_bl'] or (not strict and
            (counts[order] != 2 or worst > 2 * limit or margin < scores[order]
             or not (pairs[order] < pairs[1-order]).all())):
        return None
    return order, sorted(used), float(margin), strict


def consistent(votes):
    return (bool(votes) and len({v[0] for v in votes}) == 1
            and (len(votes) == 2 or votes[0][3]))


def fill_from_trajectories(rows, poses, valid, visible, identities, native_flip, fps, cfg, anchor_frames, links):
    """Link locally ordered blocks, then revisit individual obscured poses from both sides."""
    # Work in each block's consistent (arbitrary) order. All exported indices
    # are converted back to the original fitted midline's order.
    curves = poses.copy()
    seen = visible.copy()
    curves[native_flip] = poses[native_flip, ::-1]
    seen[native_flip] = visible[native_flip, ::-1]
    eligible = np.array([r['head_endpoint'] is not None or r['head_reason'] in
                         ('initial_taper_insufficient', 'automatic_detection_disabled',
                          'endpoints_too_close') for r in rows]) & valid
    blocks = []
    for i, row in enumerate(rows):
        if row['head_temporal_block'] < 0 or not eligible[i]:
            continue
        if not blocks or (row['head_temporal_block'] != rows[blocks[-1][-1]]['head_temporal_block']
                          or not eligible[blocks[-1][-1]:i+1].all()):
            blocks.append([])
        blocks[-1].append(i)
    # A short, unlabelled fragment cannot establish a three-observation trajectory.
    # Compare the clearer runs on either side first, then score the omitted poses.
    linked_boundaries = {k for left, right in zip(blocks, blocks[1:])
                         if (left[-1], right[0]) in links for k in (left[-1], right[0])}
    blocks = [g for g in blocks if len(g) >= 3 or rows[g[0]]['head_endpoint'] is not None
              or g[0] in linked_boundaries or g[-1] in linked_boundaries]

    def connected(i, j):
        return (j - i) / fps <= MAX_INTERVAL_S and eligible[i:j+1].all() and (identities[i:j+1] == identities[i]).all()

    def assign(i, end, evidence, reason):
        rows[i].update(head_endpoint=int(end) ^ int(native_flip[i]),
                       head_identity_confirmed=False, head_status='unconfirmed_temporal_candidate',
                       head_reason=reason, head_temporal_link_source='endpoint_trajectory',
                       head_trajectory_frames=sorted({k for _, ids, _, _ in evidence for k in ids}),
                       head_evidence_score=float(min(v[2] for v in evidence)))

    # Each edge compares past-to-present and future-to-past in their respective
    # block orders. A contradiction cuts the edge, preserving independent anchors.
    components = []
    for index, block in enumerate(blocks):
        evidence = []
        previous = blocks[index-1][-1] if index else None
        curve_match = links.get((previous, block[0]))
        if curve_match is not None:
            direction = curve_match['flip'] ^ int(native_flip[previous]) ^ int(native_flip[block[0]])
            evidence.append((direction, curve_match['frames'], curve_match['margin'], True))
        elif index and connected(previous, block[0]):
            left = blocks[index-1]
            n = max(3, int(np.ceil(.6 * fps)))
            for ref, target in ((left[-n:], block[0]), (block[:n][::-1], left[-1])):
                if seen[target].any():
                    vote = predict_order(curves, seen, ref, target, fps, cfg)
                    if vote is not None:
                        evidence.append(vote)
        skipped = index > 0 and block[0] > blocks[index-1][-1] + 1
        if consistent(evidence) and (not skipped or curve_match is not None or len(evidence) == 2):
            previous_parity = components[-1][-1][1]
            components[-1].append((block, previous_parity ^ evidence[0][0], evidence))
        else:
            components.append([(block, 0, [])])

    for component in components:
        seeds = {}
        for index, (block, parity, _) in enumerate(component):
            i = block[0]
            if rows[i]['head_endpoint'] is not None:
                seeds[index] = rows[i]['head_endpoint'] ^ int(native_flip[i]) ^ parity
        if not seeds:
            continue
        seed_indexes = np.array(list(seeds))
        for j, (block, parity, _) in enumerate(component):
            if j in seeds:
                continue
            pos = seed_indexes.searchsorted(j)
            left = seed_indexes[pos - 1] if pos else None
            right = seed_indexes[pos] if pos < len(seed_indexes) else None
            directions = {seeds[k] for k in (left, right) if k is not None}
            if len(directions) != 1:
                continue
            evidence = []
            if left is not None:
                evidence.extend(component[j][2])
            if right is not None:
                evidence.extend(component[j+1][2])
            end = directions.pop() ^ parity
            boundary_matches = []
            if left is not None:
                boundary_matches.append((component[j-1][0][-1], block[0]) in links)
            if right is not None:
                boundary_matches.append((block[-1], component[j+1][0][0]) in links)
            reason = 'centerline_overlap_orientation' if all(boundary_matches) else 'trajectory_block_orientation'
            for i in block:
                if seen[i].any():
                    assign(i, end, evidence, reason)
                    if reason == 'centerline_overlap_orientation':
                        rows[i]['head_temporal_link_source'] = 'centerline_overlap'

    # Use a frozen set of ordered, visible observations. Estimates inside the
    # contact never become observations for another frame in that same contact.
    known = np.array([r['head_endpoint'] is not None for r in rows])
    reverse = np.array([r['head_endpoint'] == 1 for r in rows])
    curves[known] = poses[known]
    seen[known] = visible[known]
    curves[reverse] = poses[reverse, ::-1]
    seen[reverse] = visible[reverse, ::-1]
    radius = int(np.ceil(.6 * fps))
    for i in np.flatnonzero(eligible & (~known | ~visible.all(axis=1))):
        if i in anchor_frames:
            continue
        votes = []
        for step in (-1, 1):
            refs = []
            for j in range(i+step, i+step*(radius+1), step):
                if not 0 <= j < len(rows) or not eligible[j] or identities[j] != identities[i]:
                    break
                if known[j] and seen[j].any():
                    refs.append(j)
            vote = predict_order(curves, seen, refs[::-1], i, fps, cfg)
            if vote is not None:
                votes.append(vote)
        if not consistent(votes):
            if known[i] and len(votes) == 2 and votes[0][0] != votes[1][0]:
                rows[i]['head_temporal_conflict'] = True
            continue
        if not seen[i].any() and len(votes) != 2:
            continue
        if known[i]:
            # Target is already in anatomical order here. A single direction
            # can corroborate it; changing it needs agreement from both sides.
            if votes[0][0] and len(votes) != 2:
                continue
            if votes[0][0]:
                corrected = 1 ^ int(reverse[i]) ^ int(native_flip[i])
                assign(i, corrected, votes, 'trajectory_frame_orientation')
                rows[i]['head_temporal_changed'] = True
            else:
                rows[i]['head_trajectory_frames'] = sorted({k for v in votes for k in v[1]})
        else:
            assign(i, votes[0][0], votes, 'trajectory_frame_orientation')
    from centerline_correspondence import constrain_trajectory_proposals
    constrain_trajectory_proposals(rows, links, known)

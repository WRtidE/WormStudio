"""Keep endpoint correspondence across measured and image-supported review poses."""
import json

import numpy as np

from head_tail.head_temporal import geometry_link, MAX_INTERVAL_S


def sampled(points):
    distance = np.r_[0., np.linalg.norm(np.diff(points, axis=0), axis=1).cumsum()]
    if not np.isfinite(distance).all() or distance[-1] < 1:
        return None
    keep = np.r_[True, np.diff(distance) > 0]
    return np.column_stack([np.interp(np.linspace(0, distance[-1], 101),
                                      distance[keep], axis[keep]) for axis in points.T])


def same_track(rows, a, b, single):
    span = rows[a:b + 1]
    if not all(r['source_valid'] and r['identity_valid'] for r in span):
        return False
    if all(r['identity_segment'] == span[0]['identity_segment'] for r in span):
        return True
    # SAM2 may split a single, continuously identified seed at a rejected mask.
    # Bridge only that short hole, never a re-identification or a user decision.
    return (single and b > a + 1
            and rows[a].get('identity_link_source') == rows[b].get('identity_link_source') == 'seed_continuous'
            and all(not r['mask_valid'] and not r.get('identity_link_source') for r in span[1:-1]))


def terminal_link(a, b):
    """Corroborate a turn using both terminal regions and the full midline."""
    length = min(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in (a, b))
    if min(np.linalg.norm(p[0] - p[-1]) for p in (a, b)) < .20 * length:
        return None
    costs = np.array([np.linalg.norm(a - q, axis=1) / length for q in (b, b[::-1])])
    flip = int(costs[1].mean() < costs[0].mean())
    best, other = costs[flip], costs[1 - flip]
    if best.mean() > .18 or other.mean() - best.mean() < .12:
        return None
    for region in (slice(0, 11), slice(-11, None)):
        if best[region].max() > .25 or np.min(other[region] - best[region]) < .15:
            return None
    return dict(flip=flip)


def contact_link(rows, a, b):
    """The contact solver stores interior curves in its left anchor's order."""
    if b != a + 1:
        return None
    event = None
    for i in (a, b):
        raw = rows[i].get('contact_bridge')
        if not raw:
            continue
        bridge = json.loads(raw) if isinstance(raw, str) else raw
        if (not bridge.get('tip_agreement') or (bridge.get('energy_gap') or 0.) < 1.
                or rows[i]['display_source'] not in ('formal','diagnostic_candidate','geometry_review')):
            return None
        if event is not None and event['anchors'] != bridge['anchors']:
            return None
        event = bridge
    if event is None:
        return None
    left, right = event['anchors']
    if (left <= a < b <= right and
            all(rows[i].get('contact_bridge') if left < i < right else rows[i]['display_source']=='formal'
                for i in (a, b))):
        return dict(flip=int(b == right and event['right_reversed']))
    return None


def hairpin_link(previous, current):
    """Match the two arms of a simple U bend even when the tips slide along them."""
    def bend(p):
        length = np.linalg.norm(np.diff(p, axis=0), axis=1).sum()
        inward = np.array([p[10] - p[0], p[-11] - p[-1]])
        norms = np.linalg.norm(inward, axis=1)
        if min(norms) < 1e-6 or length < 1:
            return None
        inward /= norms[:, None]
        if inward[0] @ inward[1] < .65:
            return None
        axis = inward.sum(axis=0)
        axis /= np.linalg.norm(axis)
        depth = p @ axis
        apex = p[depth.argmax()]
        normal = np.array([-axis[1], axis[0]])
        separation = (p[-1] - p[0]) @ normal
        steps = np.diff(p[::5], axis=0)
        turns = np.diff(np.unwrap(np.arctan2(steps[:, 1], steps[:, 0])))
        if (depth.max() - max(depth[0], depth[-1]) < .10 * length
                or abs(separation) < .10 * length or abs(turns.sum()) < np.deg2rad(125)
                or np.abs(turns).sum() > np.deg2rad(245)):
            return None
        return axis, apex, separation, length

    a, b = bend(previous), bend(current)
    if a is None or b is None:
        return None
    if a[0] @ b[0] < .85 or np.linalg.norm(a[1] - b[1]) > .15 * min(a[3], b[3]):
        return None
    return dict(flip=int(np.sign(a[2]) != np.sign(b[2])))


def fast_turn_link(previous, current, following):
    """Resolve a fast turn from both orders of the current and following poses."""
    if following is None:
        return None
    poses = (previous, current, following)
    length = min(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in poses)
    if length <= 0 or min(np.linalg.norm(p[0] - p[-1]) for p in poses) < .15 * length:
        return None

    def rms(delta):
        return np.sqrt(np.mean(np.sum(delta ** 2, axis=1))) / length

    scores = []
    for middle in (current, current[::-1]):
        first_step = middle - previous
        continuations = []
        for end in (following, following[::-1]):
            next_step = end - middle
            # Penalize abrupt reversals of the point velocities at the middle frame.
            continuations.append(rms(first_step) + rms(next_step) + .5 * rms(next_step - first_step))
        scores.append(min(continuations))
    flip = int(scores[1] < scores[0])
    chosen = current[::-1] if flip else current
    if (rms(chosen - previous) > .38 or scores[flip] > .92
            or scores[1 - flip] - scores[flip] < .09):
        return None
    return dict(flip=flip)


def order_endpoints(rows, heads, display, worm, fps, manual, single, profiles=None, parameters=None):
    """Record the displayed point order and update the matching head assignments.

    For measured poses the display and fit share their native coordinates.
    For refitted review poses the order refers only to the display geometry.
    A block carries parity only over unambiguous links. The first supported automatic
    seed, or the user's anchors, establish anatomy. Other blocks retain local A/B order.
    """
    blocks = []
    previous = None
    first_auto = None
    for i, (row, head) in enumerate(zip(rows, heads)):
        key = (worm, i)
        row.update(endpoint_order_block=None, endpoint_order_reversed=False)
        if (key not in display or row['display_source'] == 'temporal_mask_review'
                or not row['source_valid'] or not row['mask_valid']
                or not row['identity_valid']):
            continue
        raw_bridge = row.get('contact_bridge')
        if raw_bridge:
            bridge = json.loads(raw_bridge) if isinstance(raw_bridge, str) else raw_bridge
            if not bridge.get('tip_agreement') or (bridge.get('energy_gap') or 0.) < 1.:
                previous = None
                head.update(head_endpoint=None, head_identity_confirmed=False,
                            head_status='unconfirmed_A_B_endpoints', head_reason='endpoints_too_close')
                continue
        p = sampled(display[key])
        if p is None:
            continue
        parity = 0
        link = None
        if previous is not None:
            j, last, old_parity = previous
            if (i == j + 1 or (i - j) / fps <= MAX_INTERVAL_S) and same_track(rows, j, i, single):
                link = contact_link(rows, j, i) or geometry_link(last, p)
                if link is None and i == j + 1:
                    link = terminal_link(last, p)
                    if (link is None and row.get('centerline_topology') == 'simple'
                            and rows[j].get('centerline_topology') == 'simple'):
                        link = hairpin_link(last, p)
                    following = display.get((worm, i + 1))
                    if (link is None and following is not None and i + 1 < len(rows)
                            and rows[i + 1]['display_source'] != 'temporal_mask_review'
                            and rows[i + 1]['mask_valid'] and same_track(rows, i, i + 1, single)
                            and not rows[i + 1].get('contact_bridge')):
                        link = fast_turn_link(last, p, sampled(following))
                # Within an uninterrupted reliable segment the head detector also
                # tracks separated endpoints through rapid turns of the whole body.
                if (link is None and i == j + 1 and head['head_segment'] >= 0
                        and head['head_segment'] == heads[j]['head_segment']
                        and row['display_source'] == rows[j]['display_source'] == 'formal'):
                    link = dict(flip=head['head_input_flipped'] ^ heads[j]['head_input_flipped'])
                if link is not None:
                    parity = old_parity ^ int(link['flip'])
        if link is None:
            blocks.append([])
        blocks[-1].append((i, parity))
        previous = i, p, parity
        if (first_auto is None and row['display_source'] == 'formal'
                and head['head_endpoint'] is not None and not head['head_identity_confirmed']):
            first_auto = i

    for block_id, block in enumerate(blocks):
        confirmed = [(i, h ^ heads[i]['head_endpoint']) for i, h in block
                     if heads[i]['head_identity_confirmed'] and rows[i]['display_source'] == 'formal']
        # A validated short-gap link to a user seed is weaker than confirmation.
        inherited = [(i, h ^ heads[i]['head_endpoint']) for i, h in block
                     if heads[i]['head_reason'] == 'manual_seed_propagated'
                     and heads[i]['head_endpoint'] is not None and rows[i]['display_source'] == 'formal']
        seeds = confirmed or inherited
        if not seeds and not manual:
            seeds = [(i, h ^ heads[i]['head_endpoint']) for i, h in block if i == first_auto]
        conflict = len({vote for _, vote in seeds}) > 1 or any(
            heads[i]['head_anchor_conflict'] or heads[i]['head_reason'] in
            ('manual_anchor_unreliable', 'manual_point_not_near_endpoint') for i, _ in block)
        seed = None if conflict or not seeds else seeds[0]
        for i, parity in block:
            head = heads[i]
            row = rows[i]
            flip = parity ^ (seed[1] if seed else 0)
            row.update(endpoint_order_block=block_id, endpoint_order_reversed=bool(flip))
            if seed is None:
                if conflict:
                    head['head_anchor_conflict'] = True
                head.update(head_endpoint=None, head_identity_confirmed=False,
                            head_status='unconfirmed_A_B_endpoints')
                if conflict or head['head_reason'] not in ('posture_unreliable','endpoints_too_close',
                        'manual_anchor_unreliable','manual_point_not_near_endpoint','initial_taper_insufficient'):
                    head['head_reason'] = 'endpoint_anchor_conflict' if conflict else 'endpoint_order_unanchored'
                continue
            if head['head_endpoint'] == flip and (head['head_identity_confirmed'] or i == seed[0]):
                continue
            seed_frame = heads[seed[0]].get('head_temporal_seed_frame')
            head.update(head_endpoint=flip, head_identity_confirmed=False,
                        head_status='unconfirmed_temporal_candidate', head_reason='tracked_endpoint_order',
                        head_temporal_seed_frame=seed[0] if seed_frame is None else seed_frame,
                        head_evidence_score=heads[seed[0]]['head_evidence_score'])

    if manual and profiles is not None:
        recover_from_anchor_profiles(rows, heads, blocks, profiles, parameters)


def recover_from_anchor_profiles(rows, heads, blocks, profiles, parameters):
    """Compare disconnected blocks with a frozen template from the anchored track."""
    from head_tail.head_tail import appearance_fusion, DEFAULTS
    params = dict(DEFAULTS, **(parameters or {}))
    params['min_vote_fraction'] = max(.85, params['min_vote_fraction'])
    # Use only original-frame intensity profiles from measured poses. Review
    # geometry can inherit a direction but cannot vote on the template.
    snapshot = [dict(h, head_segment=-1, head_endpoint=None) for h in heads]
    for block in blocks:
        if any(heads[i]['head_anchor_conflict'] for i, _ in block):
            continue
        for i, parity in block:
            h = heads[i]
            snapshot[i].update(head_segment=rows[i]['endpoint_order_block'],
                               head_input_flipped=bool(parity), head_endpoint=h['head_endpoint'])
    appearance_fusion(snapshot, profiles, [r['identity_segment'] for r in rows], 'twardis', params)
    for block in blocks:
        if any(heads[i]['head_endpoint'] is not None or heads[i]['head_anchor_conflict'] for i, _ in block):
            continue
        for i, _ in block:
            vote = snapshot[i]
            if vote.get('head_reason') != 'appearance_temporal_reference' or vote['head_endpoint'] is None:
                continue
            for field in ('head_endpoint', 'head_reason', 'head_status', 'head_evidence_score',
                          'head_appearance_reference', 'head_appearance_reference_frames',
                          'head_appearance_score', 'head_appearance_votes'):
                heads[i][field] = vote[field]
            rows[i]['endpoint_order_reversed'] = bool(vote['head_endpoint'])

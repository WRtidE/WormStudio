"""Test the ordered 2-D posture of a short contact from its two clear boundaries."""
import numpy as np
from scipy import ndimage
from skimage.morphology import skeletonize

from .common import runs
from .contact import candidate_states, _transition
from .fusion import feasible_quality
from .fusion_v4 import VERSION as EVIDENCE_SOLVER
from .geometry import topology_candidates
from .metrics import crop_union, pose_supported
from .skeleton import skeleton_graph


def split_tip_states(mask, reference):
    """Try both interpretations of a short fork, testing each against the original mask."""
    skeleton = skeletonize(mask)
    if ndimage.label(skeleton, np.ones((3, 3)))[1] != 1:
        return []
    pixels, graph = skeleton_graph(skeleton)
    degrees = np.diff(graph.indptr)
    tips = np.flatnonzero(degrees == 1)
    if len(tips) != 3 or graph.nnz // 2 != len(pixels):
        return []
    radius = float(np.median(ndimage.distance_transform_edt(mask)[skeleton]))
    arms = []
    for tip in tips:
        path = [tip]
        node = tip
        previous = -1
        distance = 0.
        while node == tip or degrees[node] == 2:
            neighbors = graph.indices[graph.indptr[node]:graph.indptr[node + 1]]
            next_node = neighbors[neighbors != previous][0]
            distance += np.linalg.norm(pixels[next_node] - pixels[node])
            previous, node = node, next_node
            if degrees[node] != 2:
                break
            path.append(node)
        arms.append((tip, path, node, distance))
    pairs = [(a, b) for j, a in enumerate(arms) for b in arms[j+1:]
             if max(a[3], b[3]) <= 5 * radius
             and np.linalg.norm(pixels[a[2]] - pixels[b[2]]) <= radius
             and np.linalg.norm(pixels[a[0]] - pixels[b[0]]) <= 3 * radius]
    if len(pairs) != 1:
        return []
    states = []
    for tip, path, _, _ in pairs[0]:
        edited = skeleton.copy()
        edited[tuple(pixels[path].T)] = False
        proposals, _, _ = topology_candidates(mask, skeleton=edited)
        for proposal in proposals:
            line = proposal['points']
            quality = feasible_quality(line, mask, reference)
            if not quality['geometric_pass']:
                continue
            fork_end = np.linalg.norm(line[[0, -1]] - pixels[tip, ::-1], axis=1).argmin()
            observed = [True, True]
            observed[fork_end] = False
            for reverse in (False, True):
                states.append(dict(points=line[::-1] if reverse else line,
                    name='split_tip_' + proposal['name'], quality=quality,
                    endpoint_observed=observed[::-1] if reverse else observed))
    return states


def tangents(curves):
    vectors = curves[:, 5:] - curves[:, :-5]
    return vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-8)


def directional_costs(points, unary, reference, seed):
    """Costs for each ordered path, seeing only observations from one boundary."""
    costs = np.full(len(points[0]), np.inf)
    costs[seed] = 0.
    history = [costs]
    scale = .08 * reference
    for before, after, local in zip(points, points[1:], unary[1:]):
        change = _transition(before[:, None], after[None], scale)
        # Match tangent direction along the body as well as its position:
        # traversing a loop backwards can leave the two tips almost unchanged.
        bend = tangents(before)[:, None] - tangents(after)[None]
        change += np.square(bend).sum(axis=-1).mean(axis=-1)
        costs = (costs[:, None] + change).min(axis=0) + local
        history.append(costs)
    return history


def compare_paths(states, reference):
    points = [np.stack([s['points'] for s in choices]) for choices in states]
    # Cost in units of body length. Each boundary is tried in both orders at
    # the far end, so a skeletonizer reversing its output has no effect.
    unary = [np.array([(s['quality']['length_relative_deviation'] / .05)**2
                       + 4 * (1 - s['quality']['skeleton_coverage']) for s in choices])
             for choices in states]
    left = directional_costs(points, unary, reference, 0)
    right_order = int(left[-1].argmin())
    right = directional_costs(points[::-1], unary[::-1], reference, right_order)[::-1]
    orientation_gap = min(abs(np.diff(left[-1])).item(), abs(np.diff(right[0])).item())
    if right[0].argmin() != 0 or orientation_gap < 1.:
        return []
    checks = []
    for i in range(1, len(states)-1):
        total = left[i] + right[i] - unary[i]
        best = int(total.argmin())
        errors = np.linalg.norm(points[i] - points[i][best], axis=-1) / reference
        equivalent = (errors.mean(1) <= .03) & (errors[:, [0,-1]].max(1) <= .08)
        gap = float(total[~equivalent].min() - total[best]) if (~equivalent).any() else np.inf
        from_left, from_right = int(left[i].argmin()), int(right[i].argmin())
        # An observed tip may retain its identity even when two different
        # loop traversals fit the hidden part. Check only observed endpoints.
        tips = points[i][:, [0, -1]]
        shared_tip = False
        for end in (0, 1):
            if not all(states[i][j]['endpoint_observed'][end] for j in (best, from_left, from_right)):
                continue
            peers = tips[[from_left, from_right]]
            direct = np.linalg.norm(peers[:, end] - tips[best, end], axis=1)
            swapped = np.linalg.norm(peers[:, 1-end] - tips[best, end], axis=1)
            if direct.max() <= .03 * reference and (swapped - direct).min() >= .10 * reference:
                shared_tip = True
        checks.append(dict(choice=best, right_reversed=right_order, energy_gap=gap,
                           path_agreement=bool(equivalent[from_left] and equivalent[from_right]),
                           tip_agreement=shared_tip))
    return checks


def refine_contacts(curves, qa, masks, quality, baseline, meta):
    """Re-fit bounded self-contact runs using only original, accepted simple anchors."""
    reference = meta.get('reference_length_px')
    if not reference or not meta.get('fps') or not meta.get('anchors'):
        return
    identities = meta.get('contact_identity_segments')
    contact = [q['topology'] in ('paired_contact', 'unresolved_contact') for q in quality]
    for group in runs(contact):
        start, stop = int(group[0])-1, int(group[-1])+1
        if start < 0 or stop >= len(curves) or (stop-start)/meta['fps'] > 1.:
            continue
        span = slice(start, stop+1)
        if not all(q['mask_valid'] for q in quality[span]):
            continue
        if identities is not None and (identities[start] is None or len(set(identities[span])) != 1):
            continue
        if not all(qa[i].get('solver') == EVIDENCE_SOLVER for i in range(start, stop+1)):
            continue
        states = []
        x, y, x1, y1 = crop_union(masks[span], pad=4)
        offset = np.array([x, y])
        for i in range(start, stop+1):
            mask = masks[i, y:y1, x:x1]
            if i in (start, stop):
                p = curves[i]
                diff = min(np.linalg.norm(order - baseline[i], axis=1).mean() for order in (p, p[::-1]))
                if (not pose_supported(quality[i], qa[i], diff, reference)
                        or np.linalg.norm(p[0]-p[-1]) < .10*reference):
                    break
                choices = [dict(points=order-offset, quality=qa[i]) for order in (p, p[::-1])]
            else:
                choices = candidate_states(mask, reference) or split_tip_states(mask, reference)
            if not choices:
                break
            states.append(choices)
        if len(states) != stop-start+1:
            continue
        checks = compare_paths(states, reference)
        if not checks:
            continue
        path = [states[0][0]['points']]
        path.extend(s[c['choice']]['points'] for s, c in zip(states[1:-1], checks))
        path.append(states[-1][checks[-1]['right_reversed']]['points'])
        steps = np.linalg.norm(np.diff(path, axis=0), axis=-1) / reference
        seen = [[True, True]] + [s[c['choice']]['endpoint_observed']
                                 for s, c in zip(states[1:-1], checks)] + [[True, True]]
        common = np.logical_and(seen[:-1], seen[1:])
        common[~common.any(axis=1)] = True
        if np.any(steps.mean(axis=1) > .15) or np.any(steps[:, [0,-1]][common] > .25):
            continue
        for i, choices, check in zip(group, states[1:-1], checks):
            state = choices[check.pop('choice')]
            p = state['points'] + offset
            corroborated = check['path_agreement'] and check['energy_gap'] >= 1.
            # A formal contact posture also requires both tips directly visible
            # and agreement with the original TAP/image solver.
            old = curves[i]
            difference = min(np.linalg.norm(order-p, axis=1).mean() for order in (old, old[::-1]))
            measured = (corroborated and check['tip_agreement'] and all(state['endpoint_observed'])
                        and all(qa[i].get('endpoint_observed', [False, False]))
                        and difference <= .03 * reference)
            upstream = {k: qa[i].pop(k) for k in ('candidate', 'candidate_count', 'event_cost',
                        'tap_candidate_cost', 'alternative_candidate', 'alternative_energy_gap',
                        'abstention_reason') if k in qa[i]}
            qa[i].update(state['quality'], candidate=state['name'], endpoint_observed=state['endpoint_observed'],
                         candidate_count=len(choices), upstream_selection=upstream,
                         status='CONTACT_SUPPORTED' if measured else 'TEMPORAL_RECONSTRUCTION_REVIEW',
                         origin='two_sided_contact', contact_bridge=dict(check, anchors=[start, stop],
                         path_supported=corroborated, measured=bool(measured)))
            curves[i] = p

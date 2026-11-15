"""Size-calibrated, single-worm endpoint review in analysis-crop pixel coordinates."""
import csv
from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage
from skimage.morphology import skeletonize

from endpoint_order import fast_turn_link, geometry_link, hairpin_link, sampled, terminal_link
from head_tail import DEFAULTS, taper_votes
from quality_schema import flag

VERSION = "body-size-endpoints-v2-query-order"


@dataclass(frozen=True)
class BodyScale:
    length_px: float
    width_px: float
    samples: int
    source: str

    @property
    def tip_radius(self):
        return max(2., min(.04 * self.length_px, .85 * self.width_px))

    @property
    def branch_length(self):
        return max(2., min(.025 * self.length_px, .45 * self.width_px))


def clear_mask(guard):
    return (guard['area_px'] > 0 and not guard['source_frame_invalid']
            and not guard['contact_ids'] and not guard['fragmented'] and not guard['boundary_truncated'])


def arc_length(curve):
    return float(np.linalg.norm(np.diff(curve, axis=0), axis=1).sum())


def estimate_scale(rows, curves, guards):
    pools = {'measured_simple': [], 'mask_supported': []}
    for row, curve, guard in zip(rows, curves, guards):
        if curve is None or not clear_mask(guard) or not guard['mask_valid']:
            continue
        if (not flag(row, 'source_valid') or (row.get('display_inside_fraction') or 0) < .97
                or row.get('contact_bridge')):
            continue
        length = arc_length(curve)
        if length <= 0 or np.linalg.norm(curve[0] - curve[-1]) < .25 * length:
            continue
        width = guard['area_px'] / length
        if row['display_source'] == 'formal' and row.get('centerline_topology') == 'simple':
            pools['measured_simple'].append((length, width))
        pools['mask_supported'].append((length, width))
    for source, values in pools.items():
        if len(values) >= 3:
            lengths, widths = np.median(values, axis=0)
            return BodyScale(float(lengths), float(widths), len(values), source)
    return None


def mask_terminals(mask, scale):
    """A free endpoint needs a distinct terminal skeleton branch of sufficient length."""
    x, y, w, h = cv2.boundingRect(mask.astype(np.uint8))
    if not w:
        return [], None, None
    local = np.pad(mask[y:y+h, x:x+w].astype(bool), 1)
    ridge = skeletonize(local)
    degree = ndimage.convolve(ridge.astype(np.uint8), np.ones((3, 3), np.uint8),
                              mode='constant') - ridge
    distance_map = ndimage.distance_transform_edt(local)
    terminals = []
    for sy, sx in np.argwhere(ridge & (degree == 1)):
        current = (sy, sx)
        previous = None
        length = 0.
        while length < scale.branch_length:
            cy, cx = current
            neighbours = [(cy + dy, cx + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                          if (dy or dx) and ridge[cy+dy, cx+dx] and (cy+dy, cx+dx) != previous]
            if len(neighbours) != 1:
                break
            new = neighbours[0]
            length += np.hypot(new[0] - cy, new[1] - cx)
            previous, current = current, new
        if length >= scale.branch_length and distance_map[sy, sx] <= .58 * scale.width_px:
            terminals.append((sx+x-1, sy+y-1))
    return terminals, distance_map, [x-1, y-1]


def tip_evidence(terminals, distance_map, origin, curve, scale):
    if distance_map is None:
        return [False, False], None
    coords = np.rint(curve - origin).astype(int)
    coords = np.clip(coords, [0, 0], np.array(distance_map.shape[::-1])-1)
    profile = distance_map[coords[:, 1], coords[:, 0]]
    if not terminals:
        return [False, False], profile
    dist = np.linalg.norm(curve[[0,-1],None] - np.asarray(terminals), axis=2)
    nearest = dist.argmin(axis=1)
    visible = dist.min(axis=1) <= scale.tip_radius
    for i, body in enumerate((curve[15:], curve[:-15])):
        tip = curve[[0,-1][i]]
        visible[i] &= np.min(np.linalg.norm(body-tip, axis=1)) > 1.25 * scale.width_px
    if visible.all() and nearest[0] == nearest[1]:
        visible[:] = False
    return visible.tolist(), profile


def link_order(previous, curve, following, visible, scale, dt, strict):
    length = scale.length_px
    forward, reverse = (np.linalg.norm(previous - q, axis=1) for q in (curve, curve[::-1]))
    costs = np.sqrt([np.mean(forward**2), np.mean(reverse**2)])
    winner = int(costs[1] < costs[0])
    step_limit = min(.55, 5.04 * dt) * length
    assigned = reverse if winner else forward
    # Remove the rigid translation for a fast swim. Both *visible* tips and the
    # entire translated body must independently give the same assignment.
    shift = curve.mean(0) - previous.mean(0)
    translated = previous + shift
    centered = np.array([np.linalg.norm(translated-q, axis=1) for q in (curve, curve[::-1])])
    centered_rms = np.sqrt((centered**2).mean(axis=1))
    side = int(centered_rms[1] < centered_rms[0])
    seen = np.array(visible[::-1] if side else visible)
    if (any(seen) and np.linalg.norm(shift) <= min(.75,7.8*dt)*length
            and centered_rms[side] < .10*length
            and centered_rms[1-side] - centered_rms[side] > .12*length
            and np.all(centered[side,[0,-1]][seen] <= .18*length)
            and np.all((centered[1-side,[0,-1]] - centered[side,[0,-1]])[seen] > .15*length)):
        return side, 'translated_body_and_tips'
    if np.mean(assigned) > step_limit:
        return None
    # Source or motion reviews can retain provisional order only with independent
    # end correspondence and a direction advantage over the opposite assignment.
    if strict:
        if not all(visible) or abs(np.diff(costs)[0]) < .07 * length:
            return None
        good = assigned[[0,-1]]
        other = np.linalg.norm(previous[[0,-1]] - (curve if winner else curve[::-1])[[0,-1]],axis=1)
        if good.max() > step_limit or np.any(other-good < .07 * length):
            return None
        return winner, 'both_tips_and_body'
    for method in (geometry_link, terminal_link, hairpin_link):
        link = method(previous, curve)
        if link is not None:
            return int(link['flip']), method.__name__
    link = fast_turn_link(previous, curve, following)
    if link is not None:
        return int(link['flip']), 'fast_turn_link'
    if (any(visible) and abs(np.diff(costs)[0]) >= .08 * length and
            costs[winner] <= .26 * length):
        q = curve[::-1] if winner else curve
        old_ends = previous[[0,-1]]
        good = np.linalg.norm(old_ends-q[[0,-1]],axis=1)
        other = np.linalg.norm(old_ends-q[[-1,0]],axis=1)
        seen = np.asarray(visible[::-1] if winner else visible)
        if np.all(good[seen] <= step_limit) and np.all(other[seen]-good[seen] >= .10 * length):
            return winner, 'visible_tip_and_body'
    return None


def match_partial(terminals, last_seen, frame, fps, scale):
    """Carry only individually supported endpoint positions across brief missing poses."""
    if not terminals or not all(last_seen):
        return {}
    tips = np.asarray(terminals)
    ends = np.asarray([xy for _, xy in last_seen])
    distances = np.linalg.norm(ends[:,None]-tips, axis=2)
    matches = {}
    for side, (last_frame, _) in enumerate(last_seen):
        dt = (frame-last_frame)/fps
        if dt > .5 or dt <= 0:
            continue
        order = distances[side].argsort()
        j = order[0]
        d = distances[side,j]
        if (d <= min(.24, 2.4*dt)*scale.length_px and
                distances[1-side,j]-d >= .12*scale.length_px and
                (len(order) == 1 or distances[side,order[1]]-d >= scale.tip_radius)):
            matches[side] = tips[j]
    return matches


def review(rows, display, guards, get_mask, fps, tracks=None):
    if len(rows) != len(guards) or any(r['frame'] != i or g['frame'] != i
                                      for i, (r, g) in enumerate(zip(rows, guards))):
        raise ValueError('Expected one complete, frame-ordered worm track')
    worm = rows[0]['worm_id']
    curves = [sampled(display[worm,i]) if (worm,i) in display else None for i in range(len(rows))]
    scale = estimate_scale(rows, curves, guards)
    records, blocks = [], []
    ordered_history, corrections = {}, []
    previous = None
    previous_index = None
    last_seen = [None, None]
    votes, eligible, manual_votes = Counter(), Counter(), Counter()
    vote_frames = {}
    for i, (row, raw, guard) in enumerate(zip(rows, curves, guards)):
        rec = dict(frame=i, time_s=row.get('time_s', i/fps), centerline_source=row['display_source'],
                   block=None, link='', visible_ends=0, orientation='', first_label='', second_label='',
                   first_x=None, first_y=None, second_x=None, second_y=None)
        records.append(rec)
        if (scale is None or not clear_mask(guard) or not flag(row,'source_valid') or
                row.get('identity_link_source') in ('human_different','human_unknown','stale_review')):
            previous = None
            last_seen = [None, None]
            continue
        terminals, distance_map, origin = mask_terminals(get_mask(worm,i), scale)
        if raw is None or not .65 * scale.length_px <= arc_length(raw) <= 1.35 * scale.length_px:
            matches = match_partial(terminals, last_seen, i, fps, scale) if previous is not None else {}
            if matches:
                rec.update(block=len(blocks)-1, link='mask_tip_only', visible_ends=len(matches))
                blocks[-1].append(i)
                for side, point in matches.items():
                    which = ('first','second')[side]
                    rec[which+'_x'], rec[which+'_y'] = map(float, point)
                    last_seen[side] = (i, point)
            continue
        visible, profile = tip_evidence(terminals, distance_map, origin, raw, scale)
        link = None
        if previous is not None:
            gap = i-previous_index
            dt = rec['time_s'] - records[previous_index]['time_s']
            if 0 < dt and (gap == 1 or (gap <= 3 and dt <= .25 + 1e-6)):
                span = guards[previous_index:i+1]
                strict = (gap > 1 or not all(g['mask_valid'] and g['identity_valid'] for g in span)
                          or any(g['identity_segment'] != span[0]['identity_segment'] for g in span))
                following = curves[i+1] if i+1 < len(curves) and clear_mask(guards[i+1]) else None
                link = link_order(previous, raw, following, visible, scale, dt, strict)
        if (link is None and previous is not None and i-previous_index > 1
                and records[i-1]['link'] == 'mask_tip_only'):
            ends = raw[[0,-1]]
            kept = {}
            for side, point in match_partial(list(ends), last_seen, i, fps, scale).items():
                nearest = int(np.linalg.norm(ends-point,axis=1).argmin())
                if visible[nearest]:
                    kept[side] = side ^ nearest
            if kept and len(set(kept.values())) == 1:
                link = next(iter(kept.values())), 'continued_mask_tip'
        if link is None:
            last_seen = [None, None]
            if not any(visible):
                previous = None
                continue
            # Deterministic local A/B seed. An interrupted block gets its own ID.
            flip = int(tuple(raw[-1]) < tuple(raw[0]))
            blocks.append([])
            ordered_history.clear()
            reason = 'local_seed'
        else:
            flip, reason = link
        q = raw[::-1] if flip else raw
        if (tracks is not None and link is not None and i+1 < len(curves)
                and clear_mask(guards[i+1]) and flag(rows[i+1], 'source_valid')
                and not flag(row, 'head_identity_confirmed')):
            from track_order_evidence import reacquired_order
            check = reacquired_order(i, q, curves[i+1], ordered_history, tracks, scale.length_px)
            if check is not None:
                flip = 1-flip
                q = q[::-1]
                reason = 'query_order_and_velocity'
                corrections.append(check)
        ordered_history[i] = q
        visible = visible[::-1] if flip else visible
        block = len(blocks)-1
        blocks[block].append(i)
        rec.update(block=block, link=reason, visible_ends=sum(visible))
        for side,(which, pt, seen) in enumerate(zip(('first','second'), q[[0,-1]], visible)):
            if seen:
                rec[which+'_x'], rec[which+'_y'] = map(float, pt)
                last_seen[side] = (i,pt)
        if (row['display_source'] == 'formal' and flag(row,'head_identity_confirmed')
                and row.get('head_x_crop_px') is not None and all(visible)):
            point = [row['head_x_crop_px'], row['head_y_crop_px']]
            distances = np.linalg.norm(q[[0,-1]]-point,axis=1)
            if distances.min() <= scale.tip_radius and abs(np.diff(distances)[0]) > scale.tip_radius:
                manual_votes[block,int(distances.argmin())] += 1
        if (profile is not None and all(visible) and row['display_source'] == 'formal'
                and row.get('centerline_topology') == 'simple'):
            eligible[block] += 1
            for side, _ in taper_votes(profile[None], [0], [flip], DEFAULTS):
                votes[block,side] += 1
                vote_frames.setdefault(block, []).append(i)
        previous, previous_index = q, i
    intervals = []
    for block, indexes in enumerate(blocks):
        totals = [votes[block,side] for side in (0,1)]
        manual = [manual_votes[block,side] for side in (0,1)]
        side, source = None, 'unassigned'
        if sum(manual) and min(manual) == 0:
            side, source = int(np.argmax(manual)), 'manual_reference'
        elif (not sum(manual) and sum(totals) >= 5
              and (vote_frames[block][-1]-vote_frames[block][0])/fps >= .5
              and max(totals) >= .80 * sum(totals)):
            side, source = int(np.argmax(totals)), 'provisional_taper'
        labels = ('A','B') if side is None else (('H','T') if source=='manual_reference' else ('H?','T?'))
        if side == 1:
            labels = labels[::-1]
        for i in indexes:
            item = records[i]
            item['orientation'] = source
            for which, label in zip(('first','second'), labels):
                if item[which+'_x'] is not None:
                    item[which+'_label'] = label
        intervals.append(dict(block=block, start=indexes[0], end=indexes[-1], linked_frames=len(indexes),
                              orientation=source, eligible_taper_frames=eligible[block], taper_votes=totals))
    info = None if scale is None else dict(**asdict(scale), tip_radius_px=scale.tip_radius,
                                          min_branch_px=scale.branch_length)
    return records, dict(frames=len(rows), centerlines=sum(c is not None for c in curves),
                         visible_ends=dict(Counter(r['visible_ends'] for r in records)),
                         body_scale=info, intervals=intervals, order_corrections=corrections, coordinates='analysis crop pixels')


def review_output(folder, rows, display, guards, get_mask, source, sample, track_folder=None):
    from track_order_evidence import read_tracks
    if track_folder is None:
        track_folder = Path(folder).parent.parent/'centerlines-tapnext'/f"worm-{rows[0]['worm_id']}"
    tracks = read_tracks(track_folder, source) if (track_folder/'run.json').is_file() else None
    records, summary = review(rows, display, guards, get_mask, source['fps'], tracks)
    summary.update(version=VERSION, sample=sample, video=source['video'], fps=source['fps'],
                   crop_xyxy=source['crop_xyxy'], centerline_sources=dict(Counter(r['display_source'] for r in rows)))
    save_review(folder, records, summary)
    return records, summary


def save_review(folder, records, summary):
    folder = Path(folder)
    folder.mkdir(exist_ok=True, parents=True)
    (folder/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    with (folder/'frames.csv').open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)

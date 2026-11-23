"""Additional posture measurements and summaries in the run's working length unit."""
import json
from itertools import groupby

import numpy as np

from quality_schema import flag, permissions


VERSION = 'wormstudio-phenotypes-v6'


def measure_posture(row, points, scale):
    """Use observed simple postures. The definitions are invariant to endpoint order."""
    row.update(length_width_ratio=np.nan, body_extension=np.nan, midbody_abs_curvature_per_um=np.nan)
    if (points is None or not permissions(row)['posture'] or row.get('contact_bridge')
            or row.get('centerline_topology', 'simple') != 'simple'):
        return
    width = row.get('mean_width_um', np.nan)
    body_length = row.get('length_um', np.nan)
    if np.isfinite(width) and width > 0 and np.isfinite(body_length):
        row['length_width_ratio'] = float(body_length / width)
    uncertain = row.get('uncertain_point_ranges') or []
    if isinstance(uncertain, str):
        uncertain = json.loads(uncertain)
    arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    length = arc[-1]
    if not np.isfinite(length) or length <= 0:
        return
    if not uncertain and not flag(row, 'endpoint_a_inferred') and not flag(row, 'endpoint_b_inferred'):
        row['body_extension'] = float(np.clip(np.linalg.norm(points[-1] - points[0]) / length, 0, 1))
    # Turning angles at 40, 45, ..., 60% of arc length, using a 5% baseline.
    stations = length * np.arange(35, 66, 5) / 100
    if any(arc[lo] <= stations[-1] and arc[hi] >= stations[0] for lo, hi in uncertain):
        return
    samples = np.column_stack([np.interp(stations, arc, axis) for axis in points.T])
    steps = np.diff(samples, axis=0)
    if np.any(np.linalg.norm(steps, axis=1) <= np.finfo(float).eps * length):
        return
    a, b = steps[:-1], steps[1:]
    cross = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    angles = np.arctan2(np.abs(cross), np.sum(a * b, axis=1))
    row['midbody_abs_curvature_per_um'] = float(angles.mean() / (0.05 * length * scale))


def trajectory_statistics(smoothed, times, trajectory_runs):
    """Pool segment chords and paths without bridging gaps or identity changes."""
    distance = displacement = duration = 0.
    frames = 0
    segments = []
    for indices in trajectory_runs:
        if len(indices) < 2:
            continue
        points = smoothed[indices]
        distance += float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
        chord = float(np.linalg.norm(points[-1] - points[0]))
        displacement += chord
        segments.append(dict(start_frame=int(indices[0]), end_frame=int(indices[-1]),
                             start_s=float(times[indices[0]]), end_s=float(times[indices[-1]]),
                             net_displacement=chord))
        duration += float(times[indices[-1]] - times[indices[0]])
        frames += len(indices)
    return dict(trajectory_segments=segments, segment_net_displacement_sum_um=displacement if frames else None,
                path_length_um=distance if frames else None,
                path_straightness=min(1., displacement / distance) if distance > 0 else None,
                path_valid_frames=frames, path_valid_duration_s=duration)


def behavior_statistics(rows, times, trajectory_runs):
    """Integrate frame labels to adjacent midpoints inside known, continuous windows."""
    observation = {'activity': 0., 'direction': 0.}
    durations = {'moving': 0., 'stationary': 0., 'forward': 0., 'backward': 0.}
    stops = reversals = 0
    events = []
    states = [row['movement_state'] for row in rows]
    direction_states = {'forward', 'backward', 'stationary', 'trunk_stationary', 'lateral_or_turning', 'circling_candidate'}
    for domain in observation:
        keys = []
        for row, state in zip(rows, states):
            if domain == 'activity':
                keys.append(True if state != 'unknown' else None)
            elif (state in direction_states and flag(row, 'head_metrics_valid')
                  and flag(row, 'head_identity_confirmed')):
                keys.append(row['head_segment'])
            else:
                keys.append(None)
        for trajectory in trajectory_runs:
            for key, part in groupby(trajectory, key=keys.__getitem__):
                indices = list(part)
                if key is None or len(indices) < 2:
                    continue
                start, end = indices[0], indices[-1]
                observation[domain] += float(times[end] - times[start])
                def label(i):
                    if domain == 'activity':
                        return 'stationary' if states[i] in ('stationary','trunk_stationary') else 'moving'
                    return states[i]
                previous = None
                for state, bout in groupby(indices, key=label):
                    frames = list(bout)
                    first, last = frames[0], frames[-1]
                    left_open, right_open = first == start, last == end
                    onset = times[first] if left_open else (times[first-1] + times[first]) / 2
                    offset = times[last] if right_open else (times[last] + times[last+1]) / 2
                    seconds = float(offset - onset)
                    kind = None
                    if domain == 'activity':
                        durations[state] += seconds
                        if state == 'stationary':
                            stops += int(not left_open)
                            kind = 'pause'
                    elif state in ('forward', 'backward'):
                        durations[state] += seconds
                        if state == 'backward' and previous == 'forward':
                            reversals += 1
                            kind = 'reversal'
                    if kind:
                        events.append(dict(event=kind, start_frame=int(first), end_frame=int(last),
                                           start_s=float(onset), end_s=float(offset), duration_s=seconds,
                                           left_censored=bool(left_open), right_censored=bool(right_open)))
                    previous = state
    active, directed = observation['activity'], observation['direction']
    return dict(activity_observed_s=active, direction_observed_s=directed,
                moving_duration_s=durations['moving'] if active else None,
                stationary_duration_s=durations['stationary'] if active else None,
                pause_onset_count=stops if active else None,
                forward_duration_s=durations['forward'] if directed else None,
                backward_duration_s=durations['backward'] if directed else None,
                reversal_count=reversals if directed else None, behavior_events=events)

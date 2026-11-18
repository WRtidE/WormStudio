"""Associate observed channel crossings with the active MOG2 tracker."""
from collections import defaultdict, deque
from dataclasses import dataclass
from math import log, sqrt

import numpy as np
from scipy.optimize import linear_sum_assignment


@dataclass
class ChannelTrack:
    lane: int
    history: deque
    last_frame: int
    previous_frame: int | None = None
    velocity_y: float = 0.0
    area: float = 0.0


class ChannelTracker:
    """Associate observations within each physical channel, retaining short detection gaps."""

    def __init__(self, config):
        self.config = config
        self.tracks = {}
        self.next_id = 0

    def update(self, centroids, bounds, frame, areas=None):
        # Two missing observations are recoverable. Predictions never become observations.
        self.tracks = {wid: t for wid, t in self.tracks.items() if frame - t.last_frame <= 3}
        areas = areas or {}
        by_lane = defaultdict(list)
        for x, y in sorted(centroids):
            for lane, (left, right) in enumerate(bounds):
                if left <= x <= right:
                    by_lane[lane].append((x, y))
                    break

        visible = {}
        for lane, points in by_lane.items():
            ids = [wid for wid, track in self.tracks.items() if track.lane == lane]
            matches = self._match(ids, points, frame, areas)
            for j, point in enumerate(points):
                wid = matches.get(j)
                if wid is None:
                    wid = self.next_id
                    self.next_id += 1
                    track = ChannelTrack(lane, deque(maxlen=self.config.track_history_len), frame)
                    self.tracks[wid] = track
                else:
                    track = self.tracks[wid]
                    track.previous_frame = track.last_frame
                    track.velocity_y = (point[1] - track.history[-1][1]) / (frame - track.last_frame)
                track.area = areas.get(point, 0.0)
                track.last_frame = frame
                track.history.append(point)
                visible[wid] = track.history
        return visible

    def _match(self, ids, points, frame, areas):
        if not ids:
            return {}
        # One private dummy column per track lets implausible matches stay unmatched.
        costs = np.full((len(ids), len(points) + len(ids)), 1e6)
        max_dx = self.config.max_dist_x
        max_dy = self.config.max_dist_y
        for i, wid in enumerate(ids):
            track = self.tracks[wid]
            x, y = track.history[-1]
            dt = frame - track.last_frame
            predicted_y = y + dt * track.velocity_y
            # Allow a faster step only when prior motion supports it.
            step_limit = max(max_dy, min(1.5 * max_dy, 2.5 * abs(track.velocity_y)))
            costs[i, len(points) + i] = 1.7
            for j, (cx, cy) in enumerate(points):
                dx, dy = abs(cx - x), abs(cy - y)
                area = areas.get((cx, cy), 0.0)
                limit = step_limit
                if (len(ids) == len(points) == 1 and dt == 1 and len(track.history) == 1
                        and dx <= max_dx / 2 and track.area and track.area / 3 <= area <= 3 * track.area):
                    # A fast transit may be visible only twice. Allow one longer
                    # step with an unambiguous, similarly sized observation.
                    limit = 1.25 * max_dy
                if dx >= max_dx or dy >= limit * dt:
                    continue
                if dt > 1 and len(track.history) < 2 and dy > 0.15 * max_dy:
                    continue
                # When a target disappears, require stronger evidence to reconnect it.
                residual = abs(cy - predicted_y)
                scale = limit if len(track.history) == 1 else max_dy
                motion = (0.8 * residual / scale + 0.2 * dy / (scale * dt)
                          + 0.25 * dx / max_dx + 0.16 * (dt - 1))
                if motion >= 1.0:
                    continue
                # A fragment in a joined pair may move backwards as the two bodies
                # separate. Continue the approaching track along its observed direction.
                velocity = track.velocity_y
                progress = (cy - y) * np.sign(velocity) / dt
                reversal = max(0.0, 0.4 * abs(velocity) - progress) / max_dy
                size_change = abs(log(area / track.area)) if area and track.area else 0.0
                costs[i, j] = motion + min(0.35, reversal) + 0.18 * min(size_change, 1.5)
        if len(points) == 1 and len(ids) == 2 and np.all(costs[:, 0] < 1.7):
            # Two nearby fragments can reunite on the next frame. If the new
            # contour contains their combined area, preserve the established ID.
            a, b = sorted(range(2), key=lambda i: len(self.tracks[ids[i]].history))
            new, prior = self.tracks[ids[a]], self.tracks[ids[b]]
            combined = new.area + prior.area
            if (len(new.history) == 1 and len(prior.history) > 1
                    and new.last_frame == prior.last_frame == frame - 1
                    and combined and .75 * combined <= areas.get(points[0], 0) <= 1.6 * combined
                    and abs(new.history[-1][1] - prior.history[-1][1]) <= 2.5 * sqrt(combined)):
                costs[a, 0] += 0.3
        rows, cols = linear_sum_assignment(costs)
        return {int(j): ids[i] for i, j in zip(rows, cols) if j < len(points) and costs[i, j] < 1.7}

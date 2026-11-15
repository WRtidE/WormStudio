"""Short, wall-aligned image history for checking incomplete tripwire observations."""
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Body:
    x: int
    y: int
    lower_y: int
    area: float


class CrossingEvidence:
    def __init__(self, config):
        self.config = config
        self.frames = deque(maxlen=6)
        self.cache = {}

    def clear(self):
        self.frames.clear()
        self.cache.clear()

    def observe(self, strip, bounds, top, line, offset):
        radius = self.config.max_dist_y
        start = max(top, line - radius)
        stop = min(top + len(strip), line + radius + 1)
        self.frames.append((strip[start-top:stop-top].copy(), bounds, start, offset))
        self.cache.clear()

    def bodies(self, lane, age=0):
        key = (lane, age)
        if key not in self.cache:
            self.cache[key] = self._measure(lane, age)
        return self.cache[key]

    def _measure(self, lane, age):
        # Darkening relative to both earlier exposures rejects a departing body's
        # bright afterimage. Sample each exposure relative to its own channel walls.
        if len(self.frames) < 5 + age:
            return []
        margin = max(2, self.config.wall_mask_margin)
        samples = []
        for delay in (0, 2, 4):
            image, bounds, top, offset = self.frames[-1-age-delay]
            if lane >= len(bounds):
                return []
            left, right = bounds[lane]
            lo, hi = left + margin - offset, right - margin - offset
            if lo < 0 or hi > image.shape[1] or hi <= lo:
                return []
            samples.append(image[:, lo:hi])
            if delay == 0:
                x0, y0 = left + margin, top
        now, a, b = samples
        if not now.size or a.shape != now.shape or b.shape != now.shape:
            return []
        dark = cv2.subtract(cv2.min(a, b), now)
        # A broad exposure change supplies no local motion evidence.
        floor = max(6, int(np.median(dark[::8, ::3])) + 6)
        mask = cv2.compare(dark, floor, cv2.CMP_GT)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        bodies = []
        for contour in contours:
            m = cv2.moments(contour)
            area = m['m00']
            if not self.config.min_area <= area <= self.config.max_area:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if h < w or y == 0 or y + h == now.shape[0]:
                continue
            # The 90th percentile of occupied pixels is insensitive to a single
            # stray tip. Use the same body landmark on both sides of the line.
            local = np.zeros((h, w), np.uint8)
            cv2.drawContours(local, [contour], -1, 1, cv2.FILLED, offset=(-x, -y))
            mass = local.sum(axis=1).cumsum()
            lower = int(np.searchsorted(mass, .9 * mass[-1]))
            bodies.append(Body(x0 + int(m['m10'] / area), y0 + int(m['m01'] / area),
                               y0 + y + lower, area))
        return bodies

    def upper_observations(self, tracker, centroids, bounds, frame, line, areas):
        """Fill a missing upper centroid only for a directly observed lower approach."""
        supplemented = set()
        for track in tracker.tracks.values():
            x, y = track.history[-1]
            if track.last_frame != frame - 1 or not line < y < line + self.config.max_dist_y:
                continue
            lane = track.lane
            if lane >= len(bounds):
                continue
            left, right = bounds[lane]
            candidates = [b for b in self.bodies(lane) if b.y < line and
                          y - b.y < self.config.max_dist_y and abs(b.x - x) < self.config.max_dist_x and
                          track.area / 3 <= b.area <= 3 * track.area]
            if len(candidates) != 1:
                continue
            b = candidates[0]
            # A long, already detected body may yield two dark lobes. Its visible
            # centroid between the approach and the candidate explains that motion.
            width = right - left
            if any(left <= px <= right and b.y - width < py < y + width for px, py in centroids):
                continue
            centroids.append((b.x, b.y))
            areas[b.x, b.y] = b.area
            supplemented.add((b.x, b.y))
        return supplemented

    def crossing(self, track, lane, line, arm):
        """Recover an observed lower centroid or a body's lower-edge crossing."""
        x, y = track.history[-1]
        if y >= line or len(track.history) > 2:
            return None
        width = self.frames[-1][1][lane][1] - self.frames[-1][1][lane][0]
        current = [b for b in self.bodies(lane) if abs(b.y-y) < width and
                   abs(b.x-x) < self.config.max_dist_x]
        if len(current) != 1:
            return None
        curr = current[0]
        before = self.bodies(lane, 1)
        if len(track.history) == 1:
            possible = [b for b in before if line + arm <= b.y < line + self.config.max_dist_y and
                        width < b.y - curr.y < self.config.max_dist_y and abs(b.x-curr.x) < self.config.max_dist_x
                        and .1 * curr.area <= b.area <= 4 * curr.area]
            if len(possible) == 1 and curr.y <= line - arm:
                b = possible[0]
                return (b.x, b.y), (curr.x, curr.y), 'temporal_centroid'
        else:
            px, py = track.history[-2]
            possible = [b for b in before if abs(b.y-py) < width and abs(b.x-px) < self.config.max_dist_x]
            if len(possible) == 1:
                b = possible[0]
                if (py < line and b.y < line and b.lower_y >= line + arm and curr.lower_y < line - arm
                        and width < b.y - curr.y < self.config.max_dist_y
                        and abs(b.x-curr.x) < self.config.max_dist_x and .25*b.area <= curr.area <= 4*b.area):
                    return (b.x, b.lower_y), (curr.x, curr.lower_y), 'lower_body'
        return None

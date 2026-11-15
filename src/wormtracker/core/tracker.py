"""追踪模块

MOG2 通道内运动匹配、旧版贪心追踪接口以及 YOLO 记忆追踪器。
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from math import log, sqrt

import numpy as np
from scipy.optimize import linear_sum_assignment


# ==========================================================
# MOG2 贪心追踪器
# ==========================================================

def update_tracks(
    current_centroids: list,
    active_worms: dict,
    next_worm_id: int,
    cross_cooldown: dict,
    max_dist_x: int = 25,
    max_dist_y: int = 300,
    track_history_len: int = 100,
) -> tuple:
    """MOG2 模式的垂直优先贪心追踪。

    策略：
    - X 轴严格限制 (max_dist_x)，防止跨通道误匹配
    - Y 轴极度宽容 (max_dist_y)，适应高速运动

    Args:
        current_centroids: [(cx, cy), ...] 当前帧检测到的质心
        active_worms: {id: deque([(cx,cy), ...])} 历史活跃轨迹
        next_worm_id: 下一个可分配的轨迹 ID
        cross_cooldown: {id: remaining} 越线冷却状态
        max_dist_x: 最大水平位移
        max_dist_y: 最大垂直位移
        track_history_len: 轨迹历史最大长度

    Returns:
        (new_active_worms, next_worm_id, movements, cross_cooldown)
    """
    new_active_worms = {}
    movements = []

    # 清理过期冷却
    expired_ids = [wid for wid, cd in cross_cooldown.items() if cd <= 0]
    for wid in expired_ids:
        del cross_cooldown[wid]
    for wid in cross_cooldown:
        cross_cooldown[wid] -= 1

    unmatched_worms = dict(active_worms)

    for cx, cy in current_centroids:
        matched_id, min_dist = None, float("inf")

        for wid, history in unmatched_worms.items():
            last_cx, last_cy = history[-1]
            dx = abs(cx - last_cx)
            dy = abs(cy - last_cy)

            if dx < max_dist_x and dy < max_dist_y:
                if dy < min_dist:
                    min_dist = dy
                    matched_id = wid

        if matched_id is not None:
            movements.append(
                (matched_id, unmatched_worms[matched_id][-1][1], cx, cy)
            )
            history = unmatched_worms[matched_id]
            history.append((cx, cy))
            new_active_worms[matched_id] = history
            del unmatched_worms[matched_id]
        else:
            history = deque(maxlen=track_history_len)
            history.append((cx, cy))
            new_active_worms[next_worm_id] = history
            cross_cooldown[next_worm_id] = 0
            next_worm_id += 1

    return new_active_worms, next_worm_id, movements, cross_cooldown


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


# ==========================================================
# YOLO 记忆追踪器
# ==========================================================

class TrackState:
    """单个目标的追踪状态 (YOLO 模式)"""

    def __init__(self, track_id: int, point: tuple):
        self.id = track_id
        self.history = [point]
        self.lost_count = 0


class MemoryTracker:
    """线虫专用记忆追踪器 (YOLO 模式)。

    使用欧氏距离 + X/Y 独立上限进行匹配，
    丢失目标最多保留 max_age 帧后清理。
    """

    def __init__(
        self,
        max_age: int = 10,
        max_dist_x: int = 80,
        max_dist_y: int = 150,
    ):
        self.max_age = max_age
        self.max_dist_x = max_dist_x
        self.max_dist_y = max_dist_y
        self.tracks: list[TrackState] = []
        self.next_id = 0

    def update(self, centroids: list) -> list[TrackState]:
        """用当前帧质心更新所有轨迹。

        Args:
            centroids: [(cx, cy), ...]

        Returns:
            活跃的 TrackState 列表
        """
        unmatched = list(centroids)

        # 1. 匹配现有轨迹
        for t in self.tracks:
            best_idx = None
            min_dist = float("inf")
            last_pt = t.history[-1]

            for i, pt in enumerate(unmatched):
                dx = abs(pt[0] - last_pt[0])
                dy = abs(pt[1] - last_pt[1])
                dist = (dx ** 2 + dy ** 2) ** 0.5

                if dist < 120 and dx < self.max_dist_x and dy < self.max_dist_y:
                    if dist < min_dist:
                        min_dist = dist
                        best_idx = i

            if best_idx is not None:
                t.history.append(unmatched[best_idx])
                if len(t.history) > 50:
                    t.history.pop(0)
                t.lost_count = 0
                unmatched.pop(best_idx)
            else:
                t.lost_count += 1

        # 2. 为新质心创建轨迹
        for pt in unmatched:
            self.tracks.append(TrackState(self.next_id, pt))
            self.next_id += 1

        # 3. 清理过期轨迹
        self.tracks = [t for t in self.tracks if t.lost_count <= self.max_age]
        return self.tracks

    def clear(self):
        """清空追踪器 (熔断时调用)"""
        self.tracks = []

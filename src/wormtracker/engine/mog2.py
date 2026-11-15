"""MOG2 检测引擎

基于 OpenCV MOG2 背景减除 + 形态学聚类的传统视觉后端。
支持熔断保护、Y 轴 Mask 噪点屏蔽。
"""

from collections import defaultdict
from typing import Optional

import cv2
import numpy as np

from wormtracker.engine.base import BaseEngine, FrameResult
from wormtracker.core.tracker import ChannelTracker
from wormtracker.core.crossing_evidence import CrossingEvidence
from wormtracker.core.counter import count_crossings
from wormtracker.config import WormTrackerConfig, get_config


class MOG2Engine(BaseEngine):
    """MOG2 背景减除检测引擎"""

    def __init__(self, config: Optional[WormTrackerConfig] = None, *, retain_masks=True):
        self.config = config or get_config()
        self._backSub: Optional[cv2.BackgroundSubtractor] = None
        self._tracker = ChannelTracker(self.config)
        self._armed = set()
        self._evidence = CrossingEvidence(self.config)
        self._areas = {}
        self._primed = False
        self.retain_masks = retain_masks

    def prime(self, background: np.ndarray) -> None:
        """Initialize from the temporal median of startup samples before counting frame one."""
        self._ensure_backsub(background.shape)
        self._backSub.apply(self._strip(background, 0), learningRate=1.0)
        self._primed = True

    def _ensure_backsub(self, frame_shape: tuple) -> None:
        if self._backSub is None:
            self._backSub = cv2.createBackgroundSubtractorMOG2(
                history=self.config.bg_history,
                varThreshold=self.config.var_threshold,
                detectShadows=False,
            )

    # ---- 公共接口 ----

    def _strip(self, frame, offset):
        h = frame.shape[0]
        top = int(h * self.config.mask_top_ratio)
        bottom = max(top + 1, int(h * self.config.mask_bottom_ratio))
        gray = frame[top:bottom]
        if gray.ndim == 3:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        if offset:
            transform = np.float32([[1, 0, -offset], [0, 1, 0]])
            blur = cv2.warpAffine(blur, transform, (blur.shape[1], blur.shape[0]),
                                  flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_REPLICATE)
        return blur

    def warmup(self, frame: np.ndarray, offset=0) -> None:
        """Learn background in chip coordinates, including during recovery."""
        self._ensure_backsub(frame.shape)
        self._backSub.apply(self._strip(frame, offset), learningRate=0.3)

    def reset(self) -> None:
        """熔断时清空追踪器"""
        self._tracker.tracks.clear()
        self._armed.clear()
        self._evidence.clear()

    def process_frame(
        self,
        frame: np.ndarray,
        frame_idx: int,
        grid_data: dict,
        counts: dict,
        cross_cooldown: dict,
    ) -> FrameResult:
        self._ensure_backsub(frame.shape)

        centroids = []
        fg_mask = None
        raw_fg = None
        noise_ratio = 0.0
        is_panic = False

        if not self._primed and frame_idx <= self.config.init_frame_index:
            # 预热期：不检测，纯背景学习
            # 前 10 帧双重学习，快速建立初始背景，高学习率快速遗忘初始静止物体
            if frame_idx <= 10:
                self.warmup(frame)
                self.warmup(frame)
            self.warmup(frame)
            return FrameResult(
                centroids=[],
                active_worms={},
                fg_mask=np.zeros(frame.shape[:2], np.uint8) if self.retain_masks else None,
                raw_fg=raw_fg,
                noise_ratio=0.0,
            )

        # MOG2 检测
        centroids, fg_mask, noise_ratio, raw_fg = self._detect(frame, grid_data)

        # 噪点熔断检查 — 触发时跳过追踪和计数
        if noise_ratio > self.config.panic_noise_ratio:
            is_panic = True

        if is_panic:
            return FrameResult(
                centroids=centroids,
                active_worms={},
                fg_mask=fg_mask,
                raw_fg=raw_fg,
                noise_ratio=noise_ratio,
                is_panic=True,
            )

        # 追踪更新
        bounds = grid_data["channel_bounds"]
        line = int(frame.shape[0] * self.config.tripwire_ratio)
        arm = min(max(2, round(frame.shape[0] * 0.003)),
                  int(frame.shape[0] * (self.config.mask_bottom_ratio - self.config.tripwire_ratio)))
        supplemented = self._evidence.upper_observations(
            self._tracker, centroids, bounds, frame_idx, line, self._areas)
        active = self._tracker.update(centroids, bounds, frame_idx, self._areas)
        self._armed.intersection_update(self._tracker.tracks)
        for wid in list(cross_cooldown):
            if wid not in self._tracker.tracks or cross_cooldown[wid] <= 1:
                del cross_cooldown[wid]
            else:
                cross_cooldown[wid] -= 1

        # 越线计数
        events = []
        counts, cross_cooldown, crossing_channels = count_crossings(
            active,
            counts,
            grid_data["channel_bounds"],
            line,
            cross_cooldown,
            cross_debounce=self.config.cross_debounce,
            events=events,
            armed=self._armed,
            arm_distance=arm,
        )

        for event in events:
            if (event["x"], event["y"]) in supplemented:
                event["marker"] = "temporal_centroid"

        if len(self._evidence.frames) == self._evidence.frames.maxlen:
            for wid in active:
                track = self._tracker.tracks[wid]
                lane = track.lane
                if wid in cross_cooldown or track.history[-1] in supplemented:
                    continue
                if len(track.history) == 2 and track.previous_frame != frame_idx - 1:
                    continue
                if any(t.lane == lane and other != wid and other in cross_cooldown
                       and t.last_frame >= frame_idx-1 for other, t in self._tracker.tracks.items()):
                    continue
                recovered = self._evidence.crossing(track, lane, line, arm)
                if recovered:
                    prev, current, marker = recovered
                    count_crossings({wid: (prev, current)}, counts, bounds, line, cross_cooldown,
                                    cross_debounce=self.config.cross_debounce, events=events)
                    if wid in cross_cooldown:
                        events[-1].update(marker=marker, prev_frame=frame_idx-1)
                        crossing_channels.add(events[-1]['channel'])
                        self._armed.discard(wid)
        for event in events:
            previous = self._tracker.tracks[event["track"]].previous_frame
            if "prev_frame" not in event and previous != frame_idx - 1:
                event["prev_frame"] = previous

        return FrameResult(
            centroids=centroids,
            active_worms=active,
            fg_mask=fg_mask,
            raw_fg=raw_fg,
            noise_ratio=noise_ratio,
            is_panic=False,
            crossing_channels=frozenset(crossing_channels),
            crossing_events=events,
        )

    # ==========================================================
    # 检测流水线
    # ==========================================================

    def _detect(self, frame: np.ndarray, grid_data: dict) -> tuple:
        """MOG2 检测流水线。

        流水线顺序：
        预处理 → MOG2 → 墙壁遮罩 → Y轴遮罩 → 开运算 → 闭运算×2
        → 墙壁遮罩(二次) → 噪点率 → 轮廓提取 → 聚类合并
        """
        h, w = frame.shape[:2]

        top = int(h * self.config.mask_top_ratio)
        offset = grid_data.get("offset_x", 0)
        strip = self._strip(frame, offset)
        self._evidence.observe(strip, grid_data["channel_bounds"], top, int(h * self.config.tripwire_ratio), offset)
        raw_fg = self._backSub.apply(strip, learningRate=0.003)
        if offset:
            transform = np.float32([[1, 0, offset], [0, 1, 0]])
            raw_fg = cv2.warpAffine(raw_fg, transform, (w, strip.shape[0]), flags=cv2.INTER_NEAREST)
        wall_mask = self._build_wall_mask(grid_data, strip.shape[0], w)
        if wall_mask is not None:
            raw_fg = cv2.bitwise_and(raw_fg, wall_mask)
        fg = self._apply_morphology(raw_fg)
        if wall_mask is not None:
            fg = cv2.bitwise_and(fg, wall_mask)
        noise_ratio = cv2.countNonZero(fg) / (h * w)
        centroids = self._extract_centroids(fg, grid_data["channel_bounds"], top)
        if not self.retain_masks:
            return centroids, None, noise_ratio, None
        bottom = h - top - strip.shape[0]
        return (centroids, cv2.copyMakeBorder(fg, top, bottom, 0, 0, cv2.BORDER_CONSTANT), noise_ratio,
                cv2.copyMakeBorder(raw_fg, top, bottom, 0, 0, cv2.BORDER_CONSTANT))

    # ---- 子步骤 ----

    def _build_wall_mask(
        self, grid_data: dict, h: int, w: int
    ) -> Optional[np.ndarray]:
        """从 channel_bounds 反推墙壁区域遮罩。

        通道之外的区域均为墙壁（左墙、通道间墙、右墙）。
        遮罩中墙壁区域 = 0，通道区域 = 255。

        Returns:
            wall_mask 或 None（无 channel_bounds 时）
        """
        channel_bounds = grid_data.get("channel_bounds", [])
        if not channel_bounds:
            return None

        wall_mask = np.ones((h, w), dtype=np.uint8) * 255
        margin = max(
            getattr(self.config, "wall_mask_margin", 3),
            getattr(self.config, "wall_peak_half_width", 2),
        )

        # 左侧墙壁
        first_ch_left = channel_bounds[0][0]
        if first_ch_left > 0:
            wall_mask[:, : min(w, first_ch_left + margin)] = 0

        # 通道间墙壁
        for i in range(len(channel_bounds) - 1):
            gap_start = max(0, channel_bounds[i][1] - margin)
            gap_end = min(w, channel_bounds[i + 1][0] + margin)
            if gap_end > gap_start:
                wall_mask[:, gap_start:gap_end] = 0

        # 右侧墙壁
        last_ch_right = channel_bounds[-1][1]
        if last_ch_right < w:
            wall_mask[:, max(0, last_ch_right - margin) :] = 0

        return wall_mask

    def _apply_morphology(self, fg: np.ndarray) -> np.ndarray:
        """形态学：开运算(去竖线噪声) → 闭运算×2(连接虫体断点)。"""
        # 开运算：去除墙壁边缘残留的细竖线噪声
        kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 5))
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel_open)

        # 闭运算：连接虫体碎片
        kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel_close)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel_close)

        return fg

    def _extract_centroids(self, fg: np.ndarray, bounds: list, top_offset=0) -> list:
        """Attach small, nearby contour fragments to a larger body in the same channel."""
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        parts = defaultdict(list)
        for contour in contours:
            m = cv2.moments(contour)
            area = m["m00"]
            if area < max(1, self.config.min_area / 3):
                continue
            x, y = m["m10"] / area, m["m01"] / area
            for lane, (left, right) in enumerate(bounds):
                if left <= x <= right:
                    parts[lane].append((area, x, y, cv2.boundingRect(contour)))
                    break

        centroids = []
        self._areas = {}
        for lane, pieces in parts.items():
            # Largest contours anchor the groups. Never join through a chain of fragments.
            pieces.sort(reverse=True)
            used = set()
            width = bounds[lane][1] - bounds[lane][0]
            gap = max(2, min(12, round(width * 0.2)))
            for i, (area, x, y, (left, top, w, h)) in enumerate(pieces):
                if i in used:
                    continue
                total, sx, sy = area, x * area, y * area
                for j in range(i + 1, len(pieces)):
                    a, cx, cy, (other_left, other_top, cw, ch) = pieces[j]
                    if j in used or a > 0.55 * area:
                        continue
                    dx = max(left - (other_left + cw), other_left - (left + w), 0)
                    dy = max(top - (other_top + ch), other_top - (top + h), 0)
                    if dx + dy <= gap and (dx == 0 or dy == 0):
                        total += a
                        sx += cx * a
                        sy += cy * a
                        used.add(j)
                if total >= self.config.min_area:
                    point = (int(sx / total), int(sy / total) + top_offset)
                    centroids.append(point)
                    self._areas[point] = total
        return centroids

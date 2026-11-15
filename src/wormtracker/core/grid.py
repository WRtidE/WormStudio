"""网格提取模块

动态计算微流控芯片的物理通道边界。利用 Sobel 边缘检测 + 垂直投影
定位通道壁，结合通道数和墙壁比例等距划分网格坐标。

支持实时竖线峰值检测：在 Sobel 垂直投影上搜索每一道墙壁的局部峰值，
用图像实际特征修正纯几何等距插值的位置偏差。
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# 峰值检测辅助
# ---------------------------------------------------------------------------

def _find_projection_peaks(
    signal: np.ndarray,
    min_distance: int = 5,
    min_prominence: float = 0.0,
) -> list:
    """在 1D 信号中检测局部峰值（不依赖 scipy）。

    峰值定义：signal[i] > signal[i-1] 且 signal[i] >= signal[i+1]。
    返回按 x 坐标升序排列的峰值索引列表。

    Args:
        signal: 1D numpy 数组
        min_distance: 峰值间最小间距（像素）
        min_prominence: 最小相对显著度 (0~1)，相对于 signal 最大值

    Returns:
        峰值索引列表（已排序）
    """
    n = len(signal)
    if n < 3:
        return []

    # 找出所有局部极大值
    candidates = []
    for i in range(1, n - 1):
        if signal[i] > signal[i - 1] and signal[i] >= signal[i + 1]:
            candidates.append((i, float(signal[i])))

    if not candidates:
        return []

    # 按高度降序排列，贪心过滤间距
    candidates.sort(key=lambda x: x[1], reverse=True)
    sig_max = candidates[0][1] if candidates else 1.0
    prominence_thresh = sig_max * min_prominence

    selected: list[int] = []
    for idx, val in candidates:
        if val < prominence_thresh:
            continue
        if all(abs(idx - s) >= min_distance for s in selected):
            selected.append(idx)

    selected.sort()
    return selected


def _bidirectional_walk(
    peak_x: list[int],
    num_channels: int,
    est_ch_w: float,
    wall_ratio: float,
    est_w_w: float,
    wall_peak_half_width: int = 2,
) -> Optional[tuple]:
    """双向行走：从两端各分配一半通道。

    从最左峰开始，交替分配 墙壁→通道→墙壁→通道...，走 num_channels/2 个通道。
    从最右峰开始同理反向。中间如有剩余间隙则忽略。

    比全局间隙分类更鲁棒，因为两端信号最清晰。

    Returns:
        (channel_bounds, wall_bounds, channel_width, wall_width) 或 None
    """
    half = num_channels // 2
    if len(peak_x) < half * 2 + 2:  # 至少需要 half 通道 + 2*边墙 + 1中间墙
        return None

    min_channel_px = max(5.0, est_ch_w * 0.45)
    max_wall_px = est_w_w * 3.0  # 连续墙间隙上限

    # ── 从左向右走 ──
    left_wall_centers = [peak_x[0]]  # 第一道墙
    left_channel_bounds = []
    i = 0
    while len(left_channel_bounds) < half and i < len(peak_x) - 2:
        # 跳过连续墙壁间隙，找下一个通道间隙
        found_channel = False
        for j in range(i + 1, len(peak_x) - 1):
            gap = peak_x[j] - left_wall_centers[-1]
            if gap >= min_channel_px:
                # 找到通道 → 记录墙壁、通道、下一个墙壁
                right_wall = peak_x[j]
                ww = gap * wall_ratio / (1 + wall_ratio)
                cw = gap - ww
                wl = int(left_wall_centers[-1])
                wr = int(left_wall_centers[-1] + ww)
                cl = int(left_wall_centers[-1] + ww)
                cr = int(left_wall_centers[-1] + ww + cw)
                left_channel_bounds.append((cl, cr))
                left_wall_centers.append(right_wall)
                i = j
                found_channel = True
                break
        if not found_channel:
            break

    # ── 从右向左走 ──
    right_wall_centers = [peak_x[-1]]  # 最后一道墙
    right_channel_bounds = []
    i = len(peak_x) - 1
    while len(right_channel_bounds) < half and i > 1:
        found_channel = False
        for j in range(i - 1, 0, -1):
            gap = right_wall_centers[-1] - peak_x[j]
            if gap >= min_channel_px:
                left_wall = peak_x[j]
                ww = gap * wall_ratio / (1 + wall_ratio)
                cw = gap - ww
                wr = int(right_wall_centers[-1])
                wl = int(right_wall_centers[-1] - ww)
                cr = int(right_wall_centers[-1] - ww)
                cl = int(right_wall_centers[-1] - ww - cw)
                right_channel_bounds.append((cl, cr))
                right_wall_centers.append(left_wall)
                i = j
                found_channel = True
                break
        if not found_channel:
            break

    if len(left_channel_bounds) != half or len(right_channel_bounds) != half:
        return None

    # 翻转右侧（从左到右排列）
    right_channel_bounds.reverse()

    # 合并：左侧通道 + 右侧通道
    all_channel_bounds = left_channel_bounds + right_channel_bounds

    # 重建完整墙壁列表
    all_walls = list(left_wall_centers)  # 左侧墙壁（含最左）
    # 左侧最后一个墙壁与右侧第一个返回墙壁之间可能有多个峰，跳过
    all_walls.append(right_wall_centers[-2])  # 右侧倒数第二个墙（即中间分界墙）
    all_walls.extend(reversed(right_wall_centers[:-1]))  # 右侧剩余墙

    # 从墙壁中心 + 通道重建 wall_bounds（宽度由 wall_peak_half_width 控制）
    hw = wall_peak_half_width
    wall_bounds = [(int(w - hw), int(w + hw)) for w in all_walls]

    avg_cw = float(np.mean([r - l for l, r in all_channel_bounds]))
    avg_ww = float(hw * 2)
    return all_channel_bounds, wall_bounds, avg_cw, avg_ww


def _classify_gaps(
    peak_x: list[int],
    num_channels: int,
    est_ch_w: float,
) -> Optional[list[bool]]:
    """将相邻峰间间隙分类为「通道间隙」或「墙壁间隙」。（保留作回退）"""
    if len(peak_x) < 3:
        return None
    gaps = [peak_x[i + 1] - peak_x[i] for i in range(len(peak_x) - 1)]
    min_channel_px = max(5.0, est_ch_w * 0.5)
    channel_candidates = [(g, i) for i, g in enumerate(gaps) if g >= min_channel_px]
    if len(channel_candidates) < num_channels:
        return None
    channel_candidates.sort(key=lambda x: x[0], reverse=True)
    top_indices = {idx for _, idx in channel_candidates[:num_channels]}
    return [i in top_indices for i in range(len(gaps))]


# ==========================================================
# 敏感投影重算
# ==========================================================

def _recompute_projection(
    frame: np.ndarray,
    crop_top: float = 0.3,
    crop_bottom: float = 0.7,
    edge_thresh: int = 20,
    smooth_size: int = 11,
) -> np.ndarray:
    """在整帧上算一版更敏感的 Sobel 垂直投影（全宽坐标）。"""
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sy = int(h * crop_top)
    ey = int(h * crop_bottom)
    roi = gray[sy:ey, :]
    sobelx = cv2.Sobel(roi, cv2.CV_64F, 1, 0, ksize=3)
    sobel_abs = np.uint8(np.absolute(sobelx))
    _, thresh = cv2.threshold(sobel_abs, edge_thresh, 255, cv2.THRESH_BINARY)
    col_sums = np.sum(thresh, axis=0)
    return np.convolve(col_sums, np.ones(smooth_size) / smooth_size, mode="same")


def _refine_walls_from_peaks(
    frame: np.ndarray,
    roi_left: int,
    roi_right: int,
    num_channels: int,
    wall_ratio: float,
    wall_peak_half_width: int = 2,
) -> Optional[tuple]:
    """用敏感投影取信号最强的 64 个峰作为墙壁中心，间隙分类生成网格。"""
    num_walls = num_channels + 1
    valid_width = roi_right - roi_left
    denominator = num_channels + (num_channels + 1) * wall_ratio
    est_ch_w = valid_width / denominator
    est_w_w = est_ch_w * wall_ratio
    est_wall_spacing = est_ch_w + est_w_w

    # 敏感投影 + 找峰
    signal = _recompute_projection(frame, edge_thresh=20)
    min_dist = max(2, int(est_wall_spacing * 0.06))
    all_peaks = _find_projection_peaks(signal, min_distance=min_dist, min_prominence=0.03)

    # 取绝对信号最强的 64 个峰
    TOP_N = 64
    MIN_SIGNAL = 50000
    scored = [(p, float(signal[p])) for p in all_peaks if signal[p] >= MIN_SIGNAL]
    scored.sort(key=lambda x: x[1], reverse=True)
    peaks = [p for p, _ in scored[:TOP_N]]

    if len(peaks) < num_walls:
        return None

    peaks.sort()
    peak_x_all = [int(p) for p in peaks]  # 全宽坐标

    # 模式：偶数-奇数 = 通道，共 32 对
    if len(peak_x_all) >= num_channels * 2:
        channel_bounds = []
        for i in range(num_channels):
            w_left = peak_x_all[i * 2]      # 左墙
            w_right = peak_x_all[i * 2 + 1]  # 右墙
            channel_bounds.append((w_left, w_right))
        avg_cw = float(np.mean([r - l for l, r in channel_bounds]))
        # 墙壁：峰本身 ± half_width
        hw = wall_peak_half_width
        wall_bounds = [(int(px - hw), int(px + hw)) for px in peak_x_all[:num_channels * 2]]
        wall_bounds.append((int(peak_x_all[num_channels * 2 - 1]) + hw,
                            int(peak_x_all[num_channels * 2 - 1]) + hw * 2))
        return channel_bounds, wall_bounds, avg_cw, float(hw * 2)

    # 回退
    return _fallback_equidistant(
        peak_x_all, roi_left, num_channels, wall_ratio,
        est_ch_w, est_w_w, est_wall_spacing,
        wall_peak_half_width=wall_peak_half_width,
    )


def _classify_gaps_by_absolute(
    peak_x: list[int],
    num_channels: int,
) -> Optional[list[bool]]:
    """按绝对间隙阈值分类：≥39px = 通道，<39px = 墙壁。

    要求恰好 num_channels 个通道间隙。
    """
    if len(peak_x) < 3:
        return None
    gaps = [peak_x[i + 1] - peak_x[i] for i in range(len(peak_x) - 1)]
    is_channel = [g >= 39 for g in gaps]
    n_ch = sum(is_channel)
    if n_ch == num_channels:
        return is_channel
    return None


def _fallback_equidistant(
    peak_x_all: list[int],
    roi_left: int,
    num_channels: int,
    wall_ratio: float,
    est_ch_w: float,
    est_w_w: float,
    est_wall_spacing: float,
    wall_peak_half_width: int = 2,
) -> Optional[tuple]:
    """回退方案：用等距期望位置匹配最近峰值。"""
    num_walls = num_channels + 1

    peak_scored = [(px, 1.0) for px in peak_x_all]

    expected = []
    cx = float(roi_left) + est_w_w / 2.0
    for _ in range(num_walls):
        expected.append(cx)
        cx += est_wall_spacing

    used = [False] * len(peak_scored)
    wall_centers = []
    for ec in expected:
        best_idx, best_dist = -1, float("inf")
        for j, (px, _pv) in enumerate(peak_scored):
            if used[j]:
                continue
            d = abs(px - ec)
            if d < best_dist:
                best_dist, best_idx = d, j
        if best_idx >= 0 and best_dist < est_wall_spacing * 0.5:
            wall_centers.append(peak_scored[best_idx][0])
            used[best_idx] = True
        else:
            wall_centers.append(int(ec))

    wall_centers.sort()
    return _build_grid_from_centers(
        wall_centers, num_channels, wall_ratio, est_ch_w, est_w_w,
        wall_peak_half_width=wall_peak_half_width,
    )


def _build_grid_from_centers(
    wall_centers: list[int],
    num_channels: int,
    wall_ratio: float,
    est_ch_w: float,
    est_w_w: float,
    wall_peak_half_width: int = 2,
) -> Optional[tuple]:
    """从墙壁中心列表重建网格（等距分配版本）。"""
    hw = wall_peak_half_width
    channel_bounds, wall_bounds = [], []
    for i in range(num_channels):
        w_left = wall_centers[i]
        w_right = wall_centers[i + 1]
        mid_gap = w_right - w_left
        if mid_gap <= 0:
            return None
        ww = mid_gap * wall_ratio / (1 + wall_ratio)
        cw = mid_gap - ww
        wall_bounds.append((int(w_left - hw), int(w_left + hw)))
        cl, cr = int(w_left + ww), int(w_left + ww + cw)
        channel_bounds.append((cl, cr))

    wall_bounds.append((int(wall_centers[-1] - hw), int(wall_centers[-1] + hw)))

    avg_cw = float(np.mean([r - l for l, r in channel_bounds])) if channel_bounds else est_ch_w
    avg_ww = float(hw * 2)
    return channel_bounds, wall_bounds, avg_cw, avg_ww


def _build_grid_from_peaks(
    peak_x: list[int],
    gap_is_channel: list[bool],
    num_channels: int,
    wall_ratio: float,
    est_ch_w: float,
    est_w_w: float,
) -> Optional[tuple]:
    """从峰值 + 间隙分类直接重建网格。

    模型：
    - 每个峰 = 墙壁中心
    - gap_is_channel[i] = True → peak[i] 与 peak[i+1] 之间是通道
    - gap_is_channel[i] = False → 紧邻墙壁，无通道

    通道边界由通道间隙两侧的峰确定，墙壁边界从峰向两侧扩展。
    """
    # 收集通道间隙的索引
    channel_gap_indices = [i for i, is_ch in enumerate(gap_is_channel) if is_ch]
    if len(channel_gap_indices) != num_channels:
        return None

    # 从所有墙壁间隙估计实际墙壁宽度
    wall_gaps = [peak_x[i + 1] - peak_x[i]
                 for i, is_ch in enumerate(gap_is_channel) if not is_ch]
    actual_wall_width = float(np.median(wall_gaps)) if wall_gaps else est_w_w

    channel_bounds, wall_bounds = [], []

    # 处理每个通道间隙：峰 i → 通道 → 峰 i+1
    for gi in channel_gap_indices:
        w_center_left = peak_x[gi]
        w_center_right = peak_x[gi + 1]
        gap = w_center_right - w_center_left

        # 墙壁向通道内部各占 wall_ratio 比例
        half_wall = actual_wall_width / 2.0

        # 左墙：从 w_center_left 向右延伸 half_wall
        wl_left = int(w_center_left)
        wr_left = int(w_center_left + half_wall)

        # 通道：在左右墙之间
        cl = int(w_center_left + half_wall)
        cr = int(w_center_right - half_wall)

        # 右墙：从 w_center_right-half_wall 到 w_center_right
        wl_right = int(w_center_right - half_wall)
        wr_right = int(w_center_right)

        wall_bounds.append((wl_left, wr_left))
        channel_bounds.append((cl, cr))
        # 右墙的 wall_bounds 会在下一个循环（或最后）补齐，避免重复

    # 最后一个墙壁（最右峰）
    last_center = peak_x[channel_gap_indices[-1] + 1]
    last_hw = actual_wall_width / 2.0
    wall_bounds.append((int(last_center), int(last_center + last_hw)))

    avg_cw = float(np.mean([r - l for l, r in channel_bounds])) if channel_bounds else est_ch_w
    avg_ww = float(np.mean([r - l for l, r in wall_bounds])) if wall_bounds else est_w_w
    return channel_bounds, wall_bounds, avg_cw, avg_ww


def _merge_close_peaks(
    signal: np.ndarray,
    peak_indices: list[int],
    merge_distance: int,
) -> list[int]:
    """合并距离小于 merge_distance 的相邻峰。

    取合并后团簇内信号最强者的位置（而非中点），
    因为墙壁中心并不总是恰好在双峰中点。

    Args:
        signal: 1D 信号数组
        peak_indices: 已排序的峰值索引列表
        merge_distance: 合并阈值（像素）

    Returns:
        合并后的峰值索引列表（已排序）
    """
    if len(peak_indices) <= 1:
        return list(peak_indices)

    merged: list[int] = []
    cluster_start = peak_indices[0]
    cluster_best_idx = peak_indices[0]
    cluster_best_val = signal[peak_indices[0]]

    for i in range(1, len(peak_indices)):
        if peak_indices[i] - cluster_start <= merge_distance:
            # 同一团簇：更新最强峰
            if signal[peak_indices[i]] > cluster_best_val:
                cluster_best_idx = peak_indices[i]
                cluster_best_val = signal[peak_indices[i]]
        else:
            # 团簇结束
            merged.append(cluster_best_idx)
            cluster_start = peak_indices[i]
            cluster_best_idx = peak_indices[i]
            cluster_best_val = signal[peak_indices[i]]

    merged.append(cluster_best_idx)
    return merged


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def get_interpolated_grid(
    frame: np.ndarray,
    num_channels: int,
    x_margin_ratio: tuple = (0.05, 0.15),
    crop_ratio: tuple = (0.3, 0.7),
    edge_thresh: int = 30,
    signal_thresh_ratio: float = 0.15,
    wall_ratio: float = 0.2,
    refine_walls: bool = True,
    wall_peak_half_width: int = 2,
) -> dict:
    """提取绝对物理网格坐标。

    当 refine_walls=True 时，会在 Sobel 垂直投影上搜索每道墙壁的
    局部峰值来修正纯几何插值的位置偏差。

    Args:
        frame: 原始视频帧 (BGR)
        num_channels: 物理通道数量
        x_margin_ratio: (左裁剪比, 右裁剪比)
        crop_ratio: (顶部裁剪比, 底部裁剪比) 用于边缘检测的中央ROI
        edge_thresh: Sobel 二值化阈值
        signal_thresh_ratio: 峰值信号阈值比例
        wall_ratio: 墙壁宽度占通道宽度的比例
        refine_walls: 是否启用实时竖线峰值检测修正
        wall_peak_half_width: 峰值精修模式下墙壁红线半宽 (px)

    Returns:
        dict: {
            'roi_left', 'roi_right': ROI 左右边界 (绝对像素)
            'channel_width': 单个通道宽度 (像素)
            'wall_width': 墙壁宽度 (像素)
            'channel_bounds': [(left, right), ...] 各通道边界
            'wall_bounds': [(left, right), ...] 各墙壁边界
        }

    Raises:
        RuntimeError: 无法找到有效的边界锚点
    """
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    start_x = int(w * x_margin_ratio[0])
    end_x = int(w * (1 - x_margin_ratio[1]))
    start_y = int(h * crop_ratio[0])
    end_y = int(h * crop_ratio[1])

    mid_roi = gray[start_y:end_y, start_x:end_x]

    # Sobel 垂直边缘检测
    sobelx = cv2.Sobel(mid_roi, cv2.CV_64F, 1, 0, ksize=3)
    sobel_abs = np.uint8(np.absolute(sobelx))
    _, thresh = cv2.threshold(sobel_abs, edge_thresh, 255, cv2.THRESH_BINARY)

    # 垂直投影 + 平滑
    col_sums = np.sum(thresh, axis=0)
    smoothed = np.convolve(col_sums, np.ones(15) / 15, mode="same")

    # 定位有效边界
    threshold_val = np.max(smoothed) * signal_thresh_ratio
    valid_x_indices = np.where(smoothed > threshold_val)[0]

    if len(valid_x_indices) == 0:
        raise RuntimeError("未能找到边界锚点")

    roi_left = int(valid_x_indices[0]) + start_x
    roi_right = int(valid_x_indices[-1]) + start_x
    valid_width = roi_right - roi_left

    denominator = num_channels + (num_channels + 1) * wall_ratio
    channel_width = valid_width / denominator
    wall_width = channel_width * wall_ratio

    # ---- 实时竖线峰值检测 ----
    if refine_walls and wall_ratio > 0:
        refined = _refine_walls_from_peaks(
            frame, roi_left, roi_right,
            num_channels, wall_ratio,
            wall_peak_half_width=wall_peak_half_width,
        )
        if refined is not None:
            channel_bounds, wall_bounds, channel_width, wall_width = refined
            return {
                "roi_left": roi_left,
                "roi_right": roi_right,
                "channel_width": channel_width,
                "wall_width": wall_width,
                "channel_bounds": channel_bounds,
                "wall_bounds": wall_bounds,
            }

    # ---- 回退：等距划分（墙宽由 wall_peak_half_width 控制）----
    hw = wall_peak_half_width
    channel_bounds, wall_bounds = [], []
    current_x = float(roi_left)

    for _ in range(num_channels):
        wall_center = int(current_x + wall_width / 2)
        wall_bounds.append((wall_center - hw, wall_center + hw))
        current_x += wall_width
        c_left, c_right = int(current_x), int(current_x + channel_width)
        channel_bounds.append((c_left, c_right))
        current_x += channel_width

    last_wall_center = int(current_x + wall_width / 2)
    wall_bounds.append((last_wall_center - hw, last_wall_center + hw))

    return {
        "roi_left": roi_left,
        "roi_right": roi_right,
        "channel_width": channel_width,
        "wall_width": wall_width,
        "channel_bounds": channel_bounds,
        "wall_bounds": wall_bounds,
    }

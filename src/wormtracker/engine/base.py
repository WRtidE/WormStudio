"""引擎抽象基类

定义 WormTracker 检测后端的统一接口。所有引擎必须实现:
- process_frame: 单帧处理流水线
- reset: 重置内部状态 (熔断时调用)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class FrameResult:
    """单帧处理结果"""
    centroids: list                          # [(cx, cy), ...] 质心列表
    active_worms: dict                       # {id: deque([...])} 活跃轨迹
    fg_mask: Optional[np.ndarray] = None     # 前景/检测遮罩
    raw_fg: Optional[np.ndarray] = None      # 原始前景 (MOG2 用于缩略图)
    noise_ratio: float = 0.0                 # 全局噪点比例
    is_panic: bool = False                   # 是否触发熔断
    crossing_channels: frozenset = frozenset()  # 本帧穿越 tripwire 的通道 ID
    crossing_events: list = field(default_factory=list)


class BaseEngine(ABC):
    """检测引擎抽象基类"""

    @abstractmethod
    def process_frame(
        self,
        frame: np.ndarray,
        frame_idx: int,
        grid_data: dict,
        counts: dict,
        cross_cooldown: dict,
    ) -> FrameResult:
        """处理单帧，执行检测与追踪的全流水线。

        Args:
            frame: 当前视频帧 (BGR)
            frame_idx: 帧序号 (从1开始)
            grid_data: get_interpolated_grid 的返回字典
            counts: {channel_label: count} 当前累计计数
            cross_cooldown: {worm_id: remaining_frames} 越线冷却

        Returns:
            FrameResult 包含检测质心、追踪状态、异常标志
        """
        ...

    @abstractmethod
    def reset(self) -> None:
        """重置引擎内部状态 (追踪器、背景模型等)"""
        ...

    @abstractmethod
    def warmup(self, frame: np.ndarray) -> None:
        """预热帧处理 (初始化期间调用，不计入追踪)"""
        ...

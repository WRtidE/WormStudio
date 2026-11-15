"""WormTracker 配置管理模块

支持 YAML 文件读写、预设管理，以及向后兼容旧代码的全局常量导出。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

import yaml


# ==========================================================
# 配置数据类
# ==========================================================

@dataclass
class WormTrackerConfig:
    """WormTracker 完整配置"""
    # ---- 后端选择 ----
    backend: str = "mog2"  # "mog2"

    # ---- 物理映射 ----
    num_channels: int = 32
    x_margin_left: float = 0.05
    x_margin_right: float = 0.15
    wall_ratio: float = 0.08
    wall_refine: bool = True

    # ---- 检测判定 ----
    tripwire_ratio: float = 0.65
    min_area: int = 15
    max_area: int = 4500
    mask_top_ratio: float = 0.35
    mask_bottom_ratio: float = 0.85

    # ---- MOG2 背景建模 ----
    bg_history: int = 200  # 降低以更快遗忘初始帧中的静止物体
    var_threshold: int = 16
    init_frame_index: int = 20  # 预热帧数 (降低以更早开始检测)

    # ---- 追踪策略 (MOG2) ----
    max_dist_x: int = 25
    max_dist_y: int = 300
    track_history_len: int = 100
    cross_debounce: int = 10

    # ---- 熔断保护 (MOG2) ----
    panic_noise_ratio: float = 0.015
    grid_mutation_tolerance: float = 0.15
    cooldown_frames: int = 30

    # ---- 墙壁遮罩 ----
    wall_mask_margin: int = 3  # 墙壁向内侵蚀像素，防止光照变化导致墙壁伪影
    wall_peak_half_width: int = 15  # 峰值精修模式下墙壁红线半宽 (px)，全宽 = half_width * 2

    # ---- 输出 ----
    export_format: str = "xlsx"  # "xlsx" | "csv"

    # ==========================================================
    # 序列化
    # ==========================================================

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WormTrackerConfig":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)

    @classmethod
    def from_yaml(cls, path: str) -> "WormTrackerConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    def to_yaml(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.to_dict(), f, allow_unicode=True,
                           default_flow_style=False, sort_keys=False)

    # ==========================================================
    # 便捷属性: 返回元组格式以兼容旧代码
    # ==========================================================

    @property
    def x_margin(self) -> tuple:
        return (self.x_margin_left, self.x_margin_right)


# ==========================================================
# 全局单例
# ==========================================================

_config: Optional[WormTrackerConfig] = None


def _get_resource_path(relative_path: str) -> str:
    """获取资源文件路径，兼容开发环境和 PyInstaller 打包环境。"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后，资源在 sys._MEIPASS 下
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(__file__))
    return os.path.join(base, relative_path)


def get_config() -> WormTrackerConfig:
    """获取全局配置。首次调用时尝试从默认路径加载。"""
    global _config
    if _config is None:
        default_path = _get_resource_path("config.yaml")
        if os.path.exists(default_path):
            _config = WormTrackerConfig.from_yaml(default_path)
        else:
            _config = WormTrackerConfig()
    return _config


def set_config(cfg: WormTrackerConfig) -> None:
    global _config
    _config = cfg


def load_config(path: str) -> WormTrackerConfig:
    cfg = WormTrackerConfig.from_yaml(path)
    set_config(cfg)
    return cfg


def save_config(path: str, cfg: Optional[WormTrackerConfig] = None) -> None:
    (cfg or get_config()).to_yaml(path)


# ==========================================================
# 向后兼容: 导出为模块级常量，方便旧代码逐步迁移
# ==========================================================

def _cfg():
    return get_config()

# 动态属性代理，让 `from wormtracker.config import NUM_CHANNELS` 仍然可用
def __getattr__(name: str):
    """支持模块级常量访问 (兼容旧版 counter.py 的 from config import XXX)"""
    c = _cfg()
    _map = {
        "NUM_CHANNELS": c.num_channels,
        "X_MARGIN": c.x_margin,
        "WALL_RATIO": c.wall_ratio,
        "TRIPWIRE_RATIO": c.tripwire_ratio,
        "ROI_MASK_TOP_RATIO": c.mask_top_ratio,
        "ROI_MASK_BOTTOM_RATIO": c.mask_bottom_ratio,
        "MIN_AREA": c.min_area,
        "MAX_AREA": c.max_area,
        "BG_HISTORY": c.bg_history,
        "INIT_FRAME_INDEX": c.init_frame_index,
        "CROSS_DEBOUNCE": c.cross_debounce,
        "TRACK_HISTORY_LEN": c.track_history_len,
        "MAX_DIST_X": c.max_dist_x,
        "MAX_DIST_Y": c.max_dist_y,
        "PANIC_NOISE_RATIO": c.panic_noise_ratio,
        "GRID_MUTATION_TOLERANCE": c.grid_mutation_tolerance,
        "COOLDOWN_FRAMES": c.cooldown_frames,
    }
    if name in _map:
        return _map[name]
    raise AttributeError(f"module 'wormtracker.config' has no attribute '{name}'")

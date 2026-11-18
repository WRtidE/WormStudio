"""Compatibility re-export; device policy lives in platforms.device."""
from platforms.device import device_label, inference_context, release_device_cache, selected_device

__all__ = ['selected_device', 'inference_context', 'device_label', 'release_device_cache']

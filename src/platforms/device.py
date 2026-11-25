"""Shared device policy inherited by analysis subprocesses."""
import os
from contextlib import nullcontext


def opencv_gpu_name():
    """Select an OpenCL GPU in the calling video worker, independently of PyTorch."""
    if os.environ.get('WORMSTUDIO_DEVICE', '').lower() == 'cpu':
        return None
    import cv2
    try:
        if cv2.ocl.useOpenCL():
            device = cv2.ocl.Device_getDefault()
            if device.type() & cv2.ocl.Device_TYPE_GPU:
                return device.name()
    except cv2.error:
        # An installed OpenCL driver can still fail to initialize its context.
        return None
    return None


def selected_device():
    import torch
    mode = os.environ.get('WORMSTUDIO_DEVICE', 'auto').lower()
    if mode not in ('auto', 'cpu', 'cuda', 'mps'):
        raise ValueError('WORMSTUDIO_DEVICE must be auto, cpu, cuda or mps')
    if mode == 'cpu':
        return 'cpu'
    if mode == 'mps':
        if torch.backends.mps.is_available():
            return 'mps'
        raise RuntimeError('Metal GPU 不可用，请使用 CPU 启动入口。')
    if torch.cuda.is_available():
        return 'cuda'
    if mode == 'cuda':
        raise RuntimeError('GPU 不可用，请使用 CPU 启动入口。')
    if torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'


def inference_context(device):
    import torch
    if device in ('cpu', 'mps'):
        return nullcontext()
    return torch.autocast('cuda', dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16)


def device_label(device):
    import torch
    if device == 'mps':
        return 'Apple Metal GPU（MPS）'
    return torch.cuda.get_device_name() if device == 'cuda' else 'CPU（处理速度较慢）'


def release_device_cache(device=None):
    """Return allocator cache to the OS for the device actually in use.

    ``torch.cuda.empty_cache()`` is a no-op on Metal, so the previous call sites
    never released anything on macOS. CPU needs no action.
    """
    import torch
    mps = getattr(getattr(torch, 'backends', None), 'mps', None)
    if device is None:
        if torch.cuda.is_available():
            device = 'cuda'
        elif mps is not None and mps.is_available():
            device = 'mps'
        else:
            device = 'cpu'
    if device == 'cuda' and torch.cuda.is_available():
        torch.cuda.empty_cache()
    elif device == 'mps' and mps is not None and mps.is_available():
        torch.mps.empty_cache()

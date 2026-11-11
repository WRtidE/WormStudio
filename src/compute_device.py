"""Shared device policy inherited by analysis subprocesses."""
import os
from contextlib import nullcontext

def selected_device():
    import torch
    mode=os.environ.get('WORMSTUDIO_DEVICE','auto').lower()
    if mode not in ('auto','cpu','cuda','mps'):raise ValueError('WORMSTUDIO_DEVICE must be auto, cpu, cuda or mps')
    if mode=='cpu':return 'cpu'
    if mode=='mps':
        if torch.backends.mps.is_available():return 'mps'
        raise RuntimeError('Metal GPU 不可用，请使用 CPU 启动入口。')
    if torch.cuda.is_available():return 'cuda'
    if mode=='cuda':raise RuntimeError('GPU 不可用，请使用 CPU 启动入口。')
    return 'cpu'

def inference_context(device):
    import torch
    if device in ('cpu','mps'):return nullcontext()
    return torch.autocast('cuda',dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16)

def device_label(device):
    import torch
    if device=='mps':return 'Apple Metal GPU（试验模式，待实机验收）'
    return torch.cuda.get_device_name() if device=='cuda' else 'CPU（处理速度较慢）'

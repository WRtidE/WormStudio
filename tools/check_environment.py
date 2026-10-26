"""Check installed dependencies and inference assets without loading a GPU model."""
import argparse
import importlib.util
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--code-only',action='store_true');args=parser.parse_args()
    missing=[name for name in ('numpy','scipy','cv2','skimage','matplotlib','yaml','imageio_ffmpeg','torch','torchvision','einops') if importlib.util.find_spec(name) is None]
    for name in missing:print('Missing dependency:',name)
    if args.code_only:return int(bool(missing))
    from resource_paths import sam2_checkpoint
    sam=sam2_checkpoint('sam2_hiera_large.pt')
    if not sam.is_file():missing.append('SAM2 checkpoint');print('Missing SAM2 checkpoint:',sam)
    if importlib.util.find_spec('sam2') is None:missing.append('SAM2 source');print('SAM2 source is not installed; see docs/INSTALL.md')
    try:
        import torch
        if not torch.cuda.is_available():missing.append('CUDA');print('CUDA is unavailable; full inference requires a supported NVIDIA setup')
        else:print('GPU:',torch.cuda.get_device_name(0))
    except ImportError:pass
    from tapnext_support import availability
    status=availability();print('TAPNext++:',status['detail'])
    if not status['available']:missing.append('TAPNext++')
    return int(bool(missing))


if __name__=='__main__':raise SystemExit(main())

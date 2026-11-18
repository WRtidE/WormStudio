"""Package the published Windows base installer and verified optional CUDA wheels separately."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from zipfile import ZIP_STORED, ZipFile

if __package__:
    from .windows_gpu import CHUNK, GPU_PARTS, PART_BYTES, WHEELS
else:
    from windows_gpu import CHUNK, GPU_PARTS, PART_BYTES, WHEELS


HERE = Path(__file__).resolve().parent
BASE_HASH = '43fc8da8a3cf502335a9174d7c44f9c51b7ebc13859df553a3ff43c32b3bfdaf'


def pack_gpu_parts(output, wheels):
    """Split the pinned torch wheel across two ordinary stored ZIPs."""
    torch, vision = WHEELS
    for name, digest in WHEELS.items():
        with (wheels / name).open('rb') as src:
            if hashlib.file_digest(src, 'sha256').hexdigest() != digest:
                raise ValueError(f'SHA-256 mismatch: {name}')
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in GPU_PARTS):
        raise FileExistsError('GPU parts already exist in the output directory')
    with tempfile.TemporaryDirectory(dir=output) as temp, (wheels / torch).open('rb') as src:
        first, second = (Path(temp) / name for name in GPU_PARTS)
        with ZipFile(first, 'x', ZIP_STORED) as part, part.open(torch + '.001', 'w') as target:
            remaining = PART_BYTES
            while remaining:
                block = src.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError('The pinned torch wheel is smaller than the first part')
                target.write(block)
                remaining -= len(block)
        with ZipFile(second, 'x', ZIP_STORED) as part:
            with part.open(torch + '.002', 'w') as target:
                shutil.copyfileobj(src, target, CHUNK)
            part.write(wheels / vision, vision)
        if max(first.stat().st_size, second.stat().st_size) >= 2**31:
            raise ValueError('A part exceeds the GitHub release asset size limit')
        for path in (first, second):
            (output / path.name).hardlink_to(path)


def pack(destination, files, checked, instructions):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp) / destination.name
        with ZipFile(staged, 'x', ZIP_STORED) as archive:
            for name, path in files.items():
                if path.suffix == '.cmd':
                    archive.writestr(name, path.read_text(encoding='ascii').replace('\n', '\r\n').encode('ascii'))
                else:
                    archive.write(path, name)
            for name, (path, expected) in checked.items():
                digest = hashlib.sha256()
                with path.open('rb') as source, archive.open(name, 'w', force_zip64=True) as target:
                    while chunk := source.read(4 * 1024**2):
                        target.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError(f'SHA-256 mismatch: {path.name}')
            archive.writestr('使用说明.txt', instructions.encode('utf-8-sig'))
        # Publish only the finished, verified archive. Fail if another build got there first.
        if os.name == 'nt':
            staged.rename(destination)
        else:
            destination.hardlink_to(staged)
    print(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', type=Path, help='Published v1.0.0 WormStudio-Windows-x64.exe')
    parser.add_argument('--wheels', type=Path, help='Directory containing the two official Windows CUDA wheels')
    parser.add_argument('--split-gpu', action='store_true', help='Use --wheels to generate two GitHub release assets')
    parser.add_argument('--detector', action='store_true', help='Small standalone GPU check for existing desktop users')
    args = parser.parse_args()
    if args.split_gpu and not args.wheels:
        parser.error('--split-gpu requires --wheels')
    if not (args.base or args.wheels or args.detector):
        parser.error('Supply --base, --wheels or --detector')
    detector = {'GPU.cmd': HERE / 'enable-gpu.cmd', 'windows_gpu.py': HERE / 'windows_gpu.py'}
    if args.detector:
        pack(args.output / 'WormStudio-Windows-GPU检测.zip', detector, {},
             '请先完整解压，双击 GPU.cmd。工具自动检查已安装的 WormStudio 能否直接用 GPU 计算。'
             '如果需要选择位置，请选择平时使用的 WormStudio.exe。检测无需下载安装组件。\r\n')
    if args.base:
        pack(args.output / 'WormStudio-Windows-v1.0.0-基础安装.zip',
             {'安装 WormStudio.cmd': HERE / 'install-base-v1.0.0.cmd', **detector},
             {'installer/WormStudio-Windows-x64.exe': (args.base, BASE_HASH)},
             'Windows x64。完整解压后双击“安装 WormStudio.cmd”，即可安装带模型的 CPU 版。'
             '安装成功后自动检测本机加速能力。已装好软件请直接双击 GPU.cmd。仅在检测提示缺少组件且需要加速时，领取 NVIDIA 加速包。\r\n')
    if args.split_gpu:
        pack_gpu_parts(args.output, args.wheels)
    elif args.wheels:
        pack(args.output / 'WormStudio-Windows-NVIDIA-cu128-离线加速.zip',
             {'启用 NVIDIA 加速.cmd': HERE / 'enable-gpu.cmd', 'windows_gpu.py': HERE / 'windows_gpu.py'},
             {'wheels/' + name: (args.wheels / name, digest) for name, digest in WHEELS.items()},
             '需要 Windows x64 版 WormStudio 和可用的 NVIDIA 驱动。完整解压，结束分析并退出工作台，'
             '双击“启用 NVIDIA 加速.cmd”。如需选择软件位置，请选择已安装的 WormStudio.exe。'
             '看到显卡名称后重新打开工作台。组件可以从共享盘或 U 盘安装，软件所在磁盘请预留 6 GiB。\r\n')


if __name__ == '__main__':
    main()

"""Build on an Apple Silicon Mac using a dedicated, installed Conda environment."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def require_native_mac(system=None, machine=None):
    if (system or platform.system()) != 'Darwin' or (machine or platform.machine()) != 'arm64':
        raise RuntimeError('必须在 Apple 芯片 Mac 的原生 ARM64 Python 中构建，不能使用 Windows 环境或 Rosetta。')


def command_text(mode):
    if mode not in ('cpu', 'mps'):
        raise ValueError(mode)
    return ('#!/bin/bash\ncd -- "$(dirname -- "$0")" || exit 1\n'
            'if [ ! -x "runtime/bin/python" ]; then\n'
            '  echo "运行环境缺失，请取得完整的 Apple 芯片 Mac 软件包。"\n'
            '  read -r -p "按回车关闭"; exit 1\nfi\n'
            f'"./runtime/bin/python" -X utf8 "./launch.py" {mode}\n'
            'status=$?\nif [ "$status" -ne 0 ]; then read -r -p "运行失败，请查看 logs。按回车关闭"; fi\nexit "$status"\n')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(8*1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def build(output, models):
    require_native_mac()
    import conda_pack
    import sam2
    import torch
    if not (Path(sys.prefix)/'conda-meta').is_dir():
        raise RuntimeError('请在专用 Conda 环境内构建。')
    if sys.version_info[:2] != (3, 11) or torch.__version__.split('+')[0] != '2.7.0':
        raise RuntimeError('此构建配方要求 Python 3.11 和 PyTorch 2.7.0。')
    output = output.resolve()
    if output.exists() or output == ROOT or output.is_relative_to(ROOT):
        raise ValueError('请指定源码目录之外、尚不存在的交付目录。')
    manifest = json.loads((ROOT/'docs/models.json').read_text(encoding='utf-8'))
    for item in manifest.values():
        path = models/item['filename']
        if not path.is_file() or digest(path) != item['sha256']:
            raise ValueError('模型缺失或校验失败：'+str(path))
    from export_source import source_files
    files = source_files()
    output.mkdir(parents=True)
    app = output/'app'
    for source in files:
        target = app/source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (app/'models').mkdir()
    for item in manifest.values():
        shutil.copy2(models/item['filename'], app/'models'/item['filename'])
    # Installed SAM2 must be a regular installation, never an external editable checkout.
    sam_path = Path(sam2.__file__).resolve().parent
    if not sam_path.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError('SAM2 必须非 editable 安装到当前环境；不能依赖外部源码目录。')
    shutil.copytree(sam_path, app/'resources/sam2/sam2', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT/'tools/portable_launcher.py', output/'launch.py')
    shutil.copy2(ROOT/'docs/MAC_FIRST_ANALYSIS.html', output/'先读我.html')
    from create_practice_video import create
    create(output/'examples')
    for name, mode in [('启动软件.command', 'cpu'), ('试验性Metal加速.command', 'mps')]:
        script = output/name
        script.write_text(command_text(mode), encoding='utf-8', newline='\n')
        script.chmod(0o755)
    conda_pack.pack(prefix=sys.prefix, output=str(output/'runtime'), format='no-archive')
    env = os.environ.copy()
    env.update(PYTHONPATH=str(app/'resources/sam2'), WORMSTUDIO_DEVICE='cpu')
    subprocess.run([str(output/'runtime/bin/python'), 'tools/check_environment.py'], cwd=app, env=env, check=True)
    (output/'BUILD_STATUS.json').write_text(json.dumps(dict(
        platform=platform.platform(), architecture=platform.machine(), torch=torch.__version__,
        environment_check='passed', end_to_end_analysis='NOT_YET_VALIDATED',
        note='必须进行 CPU/MPS 实际视频验收，环境检查通过不等于推理通过。'), ensure_ascii=False, indent=2), encoding='utf-8')
    print('环境检查通过。请按 docs/MACOS.md 完成视频验收后再交付：', output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--models', required=True, type=Path)
    args = parser.parse_args()
    build(args.output, args.models)

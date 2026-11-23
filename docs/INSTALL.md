# 安装

源码开发使用 **uv + 项目内 `.venv`**。安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)（0.11.2 或更新版本）和 Git，在含有 `pyproject.toml` 的仓库根目录打开终端。首次安装需要网络；uv 根据 `.python-version` 准备 Python 3.11.15，无需先激活 Conda。

支持的源码环境为 Windows x64、Linux x64 和 Apple 芯片 macOS。Mac 的开发、桌面打包与实机验证说明见 [Mac 开发指南](MACOS.md)。

## 1. 安装项目环境

Windows NVIDIA CUDA 12.8 环境：

```powershell
uv sync --locked --extra cuda --managed-python
```

需要减小下载体积时，可选择同版 PyTorch 的 CUDA 12.6 构建：

```powershell
uv sync --locked --extra cuda126 --managed-python
```

Windows CPython 3.11 的官方 torch wheel：CUDA 12.8 为约 3.11 GiB，CUDA 12.6 为约 2.58 GiB，少约 541 MiB（17%）；这是 torch 下载包的差异，不是整个环境的磁盘节省量。两者都是 PyTorch 2.7.0 / torchvision 0.22.0。CUDA 12.6 对显卡的支持范围与 12.8 不完全相同，新显卡及实际模型推理应单独验证。此选项已解析进锁文件，实机验证范围见 [验证记录](VALIDATION.md)。

Windows / Linux CPU 环境，以及 Apple 芯片 Mac：

```powershell
uv sync --locked --extra cpu --managed-python
```

`cpu`、`cuda`、`cuda126` 三种选项互斥，不使用 `--all-extras`。Mac 使用 `cpu` 选项安装 PyPI 的原生 PyTorch，这并不关闭 MPS；需要强制 CPU 时设置 `WORMSTUDIO_DEVICE=cpu`。CUDA 包仍需要兼容的 NVIDIA 驱动，模型推理需单独验证。

`pyproject.toml` 是源码开发依赖的维护入口，`uv.lock` 记录各平台与 CPU/CUDA 分支的完整解析版本及下载摘要。安装使用 `--locked`，配置与锁文件不一致时会报错。PyTorch 继续使用 2.7.0 / torchvision 0.22.0；CPU 和 CUDA 包分别来自官方专用索引，其它依赖使用 PyPI。配置方式参考 [uv 的 PyTorch 集成说明](https://docs.astral.sh/uv/guides/integration/pytorch/)。

SAM2 会从固定提交 `2b90b9f5ceec907a1c18123530e92e794ad901a4` 自动构建并安装，无需手动克隆或 editable 安装。构建使用所选环境的 PyTorch，配置中的 `SAM2_BUILD_CUDA=0` 跳过可选 CUDA 扩展；主模型仍可使用 GPU，部分扩展后处理不可用。项目使用 SAM2 Hiera 权重，不混用 SAM2.1 权重。

## 2. 下载权重

```powershell
uv run --no-sync tools/download_models.py --model all --output models
```

下载器从记录的官方地址下载 SAM2 Large，并核对 SHA-256。已有文件哈希不符时拒绝覆盖。模型来源和可选配置见 [模型说明](MODELS.md)。仅运行源码测试不需要下载权重。

## 3. 检查和启动

```powershell
uv pip check
uv run --no-sync tools/check_environment.py
uv run --no-sync studio.py
```

先完成上面的同步，再使用 `uv run --no-sync` 保持已选择的 CPU/CUDA 环境。需要更新依赖时重新执行相应的 `uv sync --locked --extra cpu`、`--extra cuda` 或 `--extra cuda126`。直接执行没有选项的 `uv run` / `uv sync` 不会记住上次的 extra，可能改变 PyTorch 安装来源。

Windows 也可直接双击 `start.bat`。首次打开工作台会下载并校验当前系统对应的固定 Electron 版本，缓存到 `.electron-runtime`。离线部署可预先准备该缓存，或用 `WORM_STUDIO_ELECTRON` 指定已安装的 Electron。批处理入口为 `uv run --no-sync worm_pipeline.py --help`；仅检查代码依赖使用 `uv run --no-sync tools/check_environment.py --code-only`。测试命令见 [开发与验证](DEVELOPMENT.md)。

## 本机配置与旧环境

`uv run` 使用项目 `.venv`，不需要 `conda activate`。现有 Conda 环境可以保留作回退；`environment.yml` 和 `requirements*.txt` 仅用于旧安装方式、既有离线打包流程，不再作为源码开发的依赖维护入口。

如果以前创建过 `config/runtime.local.cmd`、`config/python_path.txt`，或设置过 `WORM_STUDIO_PYTHON`，需要检查其中是否仍指定旧解释器；显式配置继续具有最高优先级。移除旧覆盖或将其改为项目 `.venv` 的解释器后，新的运行环境才会生效。这些文件只用于本机，不提交或导出。

Windows 双击启动的查找顺序为：`WORM_STUDIO_PYTHON`、非空 `config/python_path.txt`、项目 `.venv\Scripts\python.exe`、已激活的 venv / Conda、PATH 中的 Python。配置文件使用 UTF-8（可带 BOM），路径无需引号。直接运行 Python 入口时默认使用当前解释器，并遵循同样的显式本机覆盖。

资源优先使用项目 `models/` 和 `resources/`。高级覆盖变量为 `WORM_STUDIO_RESOURCES`、`WORMSTUDIO_SAM2_CHECKPOINT`。代码、视频、模型和历史结果仍分别存放；同步环境只管理 `.venv` 中的依赖。

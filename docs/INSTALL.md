# 安装

Windows 源码版采用 x64 Python 3.11，支持 CPU 或 NVIDIA CUDA。解压到当前用户可写的目录，在含有 `start.bat` 的仓库根目录打开 PowerShell。以下安装过程需要网络和 Git。

## 1. Python环境

```powershell
conda env create -f environment.yml
conda activate WormStudio
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements-cuda.txt
python -m pip install -r requirements.txt
```

使用标准 Python 的用户可以直接建立项目内的虚拟环境。以下为 CPU 安装命令，CUDA 用户将 `requirements-cpu.txt` 替换为 `requirements-cuda.txt`。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements-cpu.txt
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

后续步骤中的 `python` 均指该环境的解释器。使用项目内虚拟环境时，可以直接将每条命令中的 `python` 写为 `.\.venv\Scripts\python.exe`。运行包版本保留本机回归所用版本；它不是全平台、带哈希的完整依赖锁。PyTorch 2.7.0 / torchvision 0.22.0 / CUDA 12.8 的组合来源于[官方历史版本表](https://pytorch.org/get-started/previous-versions/)。不同驱动环境需使用相互匹配的官方包并重新验证。

仅运行CPU测试时，用 `requirements-cpu.txt` 替代 CUDA 文件。CPU 与 GPU 的真实模型推理需分别验证。

## 2. SAM2源码

```powershell
git clone https://github.com/facebookresearch/sam2.git resources/sam2
git -C resources/sam2 checkout 2b90b9f5ceec907a1c18123530e92e794ad901a4
$env:SAM2_BUILD_CUDA = '0'
python -m pip install --no-build-isolation --no-deps -r requirements-sam2.txt
Remove-Item Env:SAM2_BUILD_CUDA
```

固定提交与[已使用的官方SAM2源码](https://github.com/facebookresearch/sam2/tree/2b90b9f5ceec907a1c18123530e92e794ad901a4)一致。`SAM2_BUILD_CUDA=0`跳过可选扩展编译，主模型仍通过GPU运行；部分扩展后处理不可用。这里使用 SAM2 Hiera 权重，不混用 SAM2.1 权重。

## 3. 权重

```powershell
python tools/download_models.py --model all --output models
```

下载器从记录的官方地址下载 SAM2 Large，并核对 SHA-256。已有文件哈希不符时拒绝覆盖。当前方法只需要 SAM2 Large 与仓库内源码。

模型来源和可选 Tiny / WormTracer 配置见 [模型说明](MODELS.md)。

## 4. 检查和启动

```powershell
python -m pip check
python tools/check_environment.py
python studio.py
```

工作台在 Electron 窗口中打开，从界面导入视频。Windows、macOS 和 Linux 的源码入口均为 `python studio.py`。首次启动会按当前系统和架构下载并校验固定版本的 Electron，缓存到项目的 `.electron-runtime`。离线部署可以预先带上对应的缓存，或用 `WORM_STUDIO_ELECTRON` 指定已安装的 Electron 可执行文件。`python worm_pipeline.py --help` 查看批处理参数。仅检查代码依赖使用 `python tools/check_environment.py --code-only`。

## 本机配置

Windows 已有一套可运行的 Python、Electron 和模型时，可将相应环境变量写在 `config/runtime.local.cmd` 中。`start.bat` 在本次启动时读取它。该文件只在本机使用，源码 ZIP 会排除它。

默认使用当前 `sys.executable`。如需双击启动固定环境，可设置 `WORM_STUDIO_PYTHON`，或创建忽略提交的 `config/python_path.txt`，文件中仅写解释器绝对路径。Windows 双击启动的查找顺序为环境变量、非空本机文件、已激活的 venv 或 Conda 环境、项目内的 `.venv\Scripts\python.exe`，最后是 PATH 中的 Python。配置文件使用 UTF-8（可带 BOM），路径无需引号。旧工作区配置继续兼容。

资源优先使用项目 `models/` 和 `resources/`。高级覆盖变量：`WORM_STUDIO_RESOURCES`、`WORMSTUDIO_SAM2_CHECKPOINT`。参考 [模型说明](MODELS.md)。

本轮做了已有环境的干净源码迁移回归；没有声称在全新电脑完成依赖下载、GPU安装和全流程推理。远程CI结果以上传后的实际运行状态为准。

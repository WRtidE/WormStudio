# Apple 芯片 macOS 适配

这里是 GitHub 源码仓库。桌面开发和 Finder 应用共用同一个 WebKit 窗口入口，识别仍由本地 Python 后端完成。离线版包含权重和运行环境；Git 仓库不包含它们。当前已在 Windows 校验 Mac 分发组件，并验证练习视频的 CPU 后端；Mac 上的 GUI 和端到端流程仍待验收。

## 从 GitHub 克隆后开始开发

目标为 Apple 芯片、macOS 14+。先安装原生 Python 3.11 和 Git。克隆仓库，在含 `studio.py` 的根目录打开终端，执行：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -c "import platform; assert platform.machine() == 'arm64', 'Use native ARM64 Python'"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r tools/macos/pins.txt
python tools/macos/run_dev.py
```

此时可以修改、启动和检查界面。要点选分割并完成分析，退出窗口，在同一环境准备 SAM2 和模型（只需一次）：

```bash
mkdir -p resources
git clone https://github.com/facebookresearch/sam2.git resources/sam2
git -C resources/sam2 checkout 2b90b9f5ceec907a1c18123530e92e794ad901a4
SAM2_BUILD_CUDA=0 python -m pip install --no-build-isolation --no-deps ./resources/sam2
python tools/download_models.py --model all --output models
python -m pip check
WORMSTUDIO_DEVICE=cpu python tools/check_environment.py
python -c "from tools.create_practice_video import create; create('input_videos/practice')"
python tools/macos/run_dev.py
```

用文件导入选择 `input_videos/practice/practice.avi`；选择完整画面和像素单位，在深色虫体内点一下，确认轮廓并开始处理。重复开发时只需激活环境并运行桌面入口，不要重复生成同名练习视频。开发版的数据写在仓库的 `input_videos/`、`outputs/`、`config/*.local.json`；已安装的应用则保存在用户的 Application Support 目录。

| 要改的部分 | 入口 |
| --- | --- |
| 窗口、菜单与启动 | [desktop.py](../tools/macos/desktop.py)、[开发入口](../tools/macos/run_dev.py) |
| 工作台页面和交互 | [web 目录](../web) |
| 分析流程与子进程 | [worm_pipeline.py](../worm_pipeline.py)、[src 目录](../src) |
| 单虫跟踪和指标 | [single-worm-methods](../packages/single-worm-methods) |

运行 `python run_tests.py`。单虫子包测试另用 `PYTHONPATH=packages/single-worm-methods python -m unittest discover -s packages/single-worm-methods/tests`。前端测试需要 Node：`node --test tests/*.js tests/*.cjs`。上传 GitHub 后也可在 Actions 手动运行 **Mac development smoke test**：该任务只检查依赖、原生模块和平台路由，不下载权重，不能代替实际视频验收。

## 重建 Finder 离线应用

以下在已完成上述准备的 Mac 上执行。从固定上游发布下载 CPython，打包器会再次验证其 SHA-256。wheel 获取脚本按清单选取支持 macOS 14 arm64 的文件，并校验 PyPI 摘要。纯 Python 源码包在构建机上生成 wheel。

```bash
mkdir -p .mac-build/wheels dist
curl -fL --retry 3 'https://github.com/astral-sh/python-build-standalone/releases/download/20260325/cpython-3.11.15%2B20260325-aarch64-apple-darwin-install_only_stripped.tar.gz' -o .mac-build/python.tar.gz
python tools/macos/fetch_wheels.py tools/macos/pins.txt .mac-build/wheels
python tools/macos/package_app.py .mac-build models resources/sam2 input_videos/practice/practice.avi dist/WormStudio-Mac-arm64.zip
```

解压后将 `WormStudio.app` 放入应用程序并打开；第一次使用时离线安装组件。关闭后重新打开，检查同一视频、标注和结果仍可访问。试用包尚未签名或公证。打包器不会覆盖已有输出；重新构建时指定新的 ZIP 文件名。

## 另一种构建方式：Conda 便携目录（维护者用）

以下命令在源码根目录执行；需要原生 ARM64 Conda 和 Git。

```bash
conda create -n WormStudioMac python=3.11 pip -y
conda activate WormStudioMac
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements-macos.txt
python -m pip install conda-pack
mkdir -p resources
git clone https://github.com/facebookresearch/sam2.git resources/sam2
git -C resources/sam2 checkout 2b90b9f5ceec907a1c18123530e92e794ad901a4
SAM2_BUILD_CUDA=0 python -m pip install --no-build-isolation --no-deps ./resources/sam2
python tools/download_models.py --model all --output models
python -m pip check
WORMSTUDIO_DEVICE=cpu python tools/check_environment.py
python run_tests.py
python tools/build_macos.py --models models --output ../WormStudio-Mac-arm64
```

必须非 editable 安装 SAM2。构建器拒绝 Windows、Intel/Rosetta、错误 Python/PyTorch 版本、缺失或损坏的权重；不会覆盖已有输出。依赖沿用项目固定版本，但 Mac 原生 wheel 可用性仍需在上述安装中验证；安装失败应记录并重新验证兼容版本，不能宣称打包通过。

## 实机验收后再交付

把构建目录复制到一个包含空格/中文的新路径，退出 Conda，然后双击启动软件.command。首次启动修复便携环境路径；将目录放到最终位置后再首次运行。无需让实验人员安装 Python 或运行安装命令。默认 CPU；试验性Metal加速.command 是显式 MPS 入口，不会被 auto 自动启用。MPS 使用 float32，启用 PyTorch 不支持算子的 CPU 回退；这不能保证所有算子或整段流程兼容。

验收步骤：导入合成练习视频，点选分割并确认，使用像素单位，完成 SAM2/TAPNext/指标/视频导出，重新解码导出视频、读取 CSV，关闭后重开历史结果。然后用真实视频短片分别验证 CPU 和 MPS；记录 Mac 型号、内存、macOS/Python/PyTorch 版本、耗时、结果差异。只有端到端实际完成才能把 BUILD_STATUS.json 中 NOT_YET_VALIDATED 改为通过。

MPS 报错时关闭软件，再用默认 CPU 入口启动；不在已运行任务中静默改变设备。MPS 未通过时只交付 CPU 入口。长视频的内存和耗时需另行测量。

此段的 `.command` 和 Metal 入口仅适用于 Conda 便携目录。上面的 Finder 应用默认使用 CPU。

官方依据：[PyTorch 历史安装版本](https://pytorch.org/get-started/previous-versions/)、[SAM2 macOS CPU/MPS demo](https://github.com/facebookresearch/sam2/blob/main/demo/README.md)、[MPS 环境变量](https://docs.pytorch.org/docs/2.15/mps_environment_variables.html)。上游支持不等于本项目整套流程已通过实机验证。

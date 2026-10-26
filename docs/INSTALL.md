# 安装

推荐 Python 3.11、Windows 和支持 CUDA 的 NVIDIA 显卡。以下命令在仓库根目录执行。源码包不包含本机路径或权重。

## 1. Python环境

```powershell
conda env create -f environment.yml
conda activate WormStudio
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements-cuda.txt
python -m pip install -r requirements.txt
```

也可自行创建 Python 3.11 虚拟环境。运行包版本保留本机回归所用版本；它不是全平台、带哈希的完整依赖锁。PyTorch 2.7.0 / torchvision 0.22.0 / CUDA 12.8 的组合来源于[官方历史版本表](https://pytorch.org/get-started/previous-versions/)。不同驱动环境需使用相互匹配的官方包并重新验证。

仅运行CPU测试时，用 `requirements-cpu.txt` 替代 CUDA 文件。CPU测试不会证明GPU模型推理可用。

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

下载器从记录的官方地址下载 SAM2 Large 和 TAPNext++，并核对 SHA-256。已有文件哈希不符时拒绝覆盖。无需安装另一个单虫算法仓库，源码已放在 `packages/single-worm-methods`。

模型来源和可选 Tiny / WormTracer 配置见 [模型说明](MODELS.md)。

## 4. 检查和启动

```powershell
python -m pip check
python tools/check_environment.py
python studio.py
```

工作台打开本地网页；从界面导入视频。`python worm_pipeline.py --help` 查看批处理参数。仅检查代码依赖使用 `python tools/check_environment.py --code-only`。

## 本机配置

默认使用当前 `sys.executable`。如需双击启动固定环境，可设置 `WORM_STUDIO_PYTHON`，或创建忽略提交的 `config/python_path.txt`，文件中仅写解释器绝对路径。优先级：环境变量、非空本机文件、当前Python。旧工作区配置继续兼容。

资源优先使用项目 `models/` 和 `resources/`。高级覆盖变量：`WORM_STUDIO_RESOURCES`、`WORMSTUDIO_SAM2_CHECKPOINT`、`WORMSTUDIO_SINGLE_ROOT`、`WORMSTUDIO_TAPNEXT_CHECKPOINT`。参考 [模型说明](MODELS.md)。

本轮做了已有环境的干净源码迁移回归；没有声称在全新电脑完成依赖下载、GPU安装和全流程推理。远程CI结果以上传后的实际运行状态为准。

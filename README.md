# Worm Studio

本地运行的线虫视频分析工作台：人工点选虫体，自动识别与跟踪、分析姿态、计算表型指标，并逐帧复核身份和头尾。

**Windows x64：** 在仓库根目录用 Python 3.11 建立环境并安装 CPU 或 CUDA 依赖，随后双击 `start.bat`。项目内的 `.venv` 可直接启动。详见[Windows 安装与启动](docs/INSTALL.md)。

**在 Mac 上继续开发：** 按 [Mac 开发与打包指南](docs/MACOS.md) 安装环境。在仓库根目录运行 `python tools/macos/run_dev.py` 即可从当前源码打开 Electron 桌面窗口。修改界面或算法后重启即可，不需要每次重新制作应用包。目标为 Apple 芯片、macOS 14+、Python 3.11。

## 当前源码版本

桌面应用版本为 **1.0.1**。本次更新修复 Mac 启动架构判断、应用图标尺寸和通道计数标签显示，算法保持不变。实测范围与限制见 [v1.0.1 发布说明](docs/releases/v1.0.1.md)。

新建单虫分析默认使用 **SAM2 Large + 多帧身体连续跟踪**（`body_sequence`）。程序提取 101 点中线，利用 13 个身体点的跨帧运动和绕柱方向选择连续路径。人工头点和自动推断分别记录。详见[质量与测量规则](docs/QUALITY_SCHEMA.md)。

工作台支持多培养槽标注、标注帧测量和通道计数。结果页按视频和培养槽汇总形态与运动指标，支持全屏播放、逐帧定位、复核关键节点和导出 CSV。近期改动见[更新记录](CHANGELOG.md)。

运行环境采用 Python 3.11。程序自动选择可用的 NVIDIA CUDA、Apple Metal（MPS）或 CPU。通道计数是独立入口，支持逐帧复核越线记录并导出 Excel，操作见[使用指南](docs/USER_GUIDE.md)。

工作台采用清爽的白色界面：首页聚焦文件导入，选中素材后显示画面与处理面板。左侧以文件树管理素材，支持文件夹层级、拖动排序、重命名与移除，重启后恢复；移除保留原文件和分析记录。文件信息、历史分析和流程说明按需展开，文件夹导入与新建位于文件栏顶部，路径导入、布局重置、评测和帮助收在顶部菜单。支持自动标注草稿，以及按视频查看历史结果。

## 桌面版下载

- **Mac v1.0.1**：[下载离线 ZIP](https://github.com/WRtidE/WormStudio/releases/download/v1.0.1/WormStudio-Mac-arm64.zip)，适用于 Apple 芯片、macOS 14+。解压后将应用拖入“应用程序”。首次运行按包内“先读我”完成准备。
- **Windows x64 v1.0.2**：解压基础包，打开 `WormStudio.exe` 即可分析。在“设置 → 通用 → 检测 GPU”查看本机能否加速。[下载基础包](https://github.com/WRtidE/WormStudio/releases/download/v1.0.2/WormStudio-Windows-x64.zip) · [可选 GPU 分包与说明](https://github.com/WRtidE/WormStudio/releases/tag/v1.0.2)
- [发布页、校验文件与历史版本](https://github.com/WRtidE/WormStudio/releases)。离线安装包包含运行环境和模型；源码归档用于开发。

## 开始使用

先按 [安装说明](docs/INSTALL.md) 配置环境、安装依赖并下载权重，然后在仓库根目录运行：

```powershell
python tools/check_environment.py
python studio.py
```

该命令在 Electron 中打开工作台。Windows 激活环境后也可双击 `start.bat`。导入自己的视频，选择“像素单位”或“标尺标定”，点选虫体并在底部逐条确认掩膜，再开始分析。详见 [使用指南](docs/USER_GUIDE.md)。

源码包**不包含模型权重、实验视频、分析结果或真实人工标注**。缺少模型时仍能启动工作台，但相关推理功能不可用。

## 功能与边界

- 原始视频、掩膜、中线与逐帧指标联动查看；支持虫体聚焦和缩放。
- 个体身份自动重新关联，歧义片段保留人工复核入口。
- 首个清晰帧人工确定头端优先；可按端部渐细程度初始化候选，沿可靠骨架保持方向。
- 当前姿态结果用 H/T 标记有方向的端点，A/B 标记方向待定；侧栏与逐帧记录区分人工确认和自动候选。
- 正式测量骨架与待复核显示骨架分开；头尾未确认不阻止显示虫体。
- 无物理标定时只输出像素结果；自接触、长遮挡后的身份和头尾仍可能不确定。

当前算法属于研究软件，四段开发视频的连续显示结果不等于独立准确率验证。复核候选和自动端点方向应结合逐帧质量记录解读。规则见 [质量与测量](docs/QUALITY_SCHEMA.md)。

## 目录

```text
WormStudio/
├── studio.py                 # 工作台启动入口
├── worm_pipeline.py          # 流程与命令行入口
├── evaluation.py             # 评测任务工具
├── review_saved_result.py    # 已有结果只读查看
├── src/                      # 分割适配、姿态、身份、头尾、测量、HTTP服务
├── web/                      # 无前端构建依赖的HTML/CSS/JavaScript
├── config/                   # 公共参数与本机配置示例
├── tests/                    # 合成数据与接口回归
├── tools/                    # 环境检查、模型下载、源码导出检查
├── docs/                     # 安装、用法、架构、质量、开发、发布
└── .github/workflows/        # Linux、Windows 和 macOS 检查配置
```

运行时产生的 `models/`、`resources/`、`input_videos/`、`outputs/` 和 `evaluation_data/` 都在 Git 忽略规则内。

## 开发与发布

界面在 `web/`，识别、测量和后端在 `src/`，Electron 窗口在 `tools/electron/`。在已安装依赖的 PowerShell 中运行回归测试。

```powershell
python run_tests.py
$tests = (Get-ChildItem tests/*.js, tests/*.cjs -File).FullName
node --test @tests
```

上传前检查待推送的提交。生成与提交一致的 ZIP、验证源码清单和推送到现有仓库的步骤见[发布指南](docs/RELEASE.md)。

[本轮验证](docs/VALIDATION.md) · [代码架构](docs/ARCHITECTURE.md) · [开发与验证](docs/DEVELOPMENT.md) · [发布方法](docs/RELEASE.md) · [贡献说明](CONTRIBUTING.md)

## 第三方与许可

第三方来源及固定提交见 [THIRD_PARTY.md](THIRD_PARTY.md)。项目自有代码尚未指定统一开源许可证；公开上传不等于已授予开源再分发许可。模型和上游源码遵循各自条款。

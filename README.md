# Worm Studio

本地运行的线虫视频分析工作台：人工点选虫体，自动识别与跟踪、分析姿态、计算表型指标，并逐帧复核身份和头尾。

**在 Mac 上继续开发：** 按 [Mac 开发与打包指南](docs/MACOS.md) 安装环境。在仓库根目录运行 `python tools/macos/run_dev.py` 即可从当前源码打开桌面窗口。修改界面或算法后重启即可，不需要每次重新制作应用包。目标为 Apple 芯片、macOS 14+、Python 3.11。首次实机验收仍待完成。

新分析统一使用当前最新版，一次处理一条线虫；头尾自动识别，人工标记优先。界面不再提供算法选择。完整推理支持 CPU 和 NVIDIA CUDA；无可用 CUDA 时自动使用 CPU，速度较慢。主要验证平台为 Windows、Python 3.11。

工作台采用清爽的白色界面：首页聚焦文件导入，选中素材后显示画面与处理面板。左侧以文件树管理素材，支持文件夹层级、拖动排序、重命名与移除，重启后恢复；移除保留原文件和分析记录。文件信息、历史分析和流程说明按需展开，文件夹导入与新建位于文件栏顶部，路径导入、布局重置、评测和帮助收在顶部菜单。支持自动标注草稿，以及按视频查看历史结果。

## 开始使用

先按 [安装说明](docs/INSTALL.md) 配置环境、安装依赖并下载权重，然后在仓库根目录运行：

```powershell
python tools/check_environment.py
python studio.py
```

Windows 激活环境后也可双击 `start.bat`。导入自己的视频，选择“像素单位”或“标尺标定”，点选虫体并在底部逐条确认掩膜，再开始分析。详见 [使用指南](docs/USER_GUIDE.md)。

源码包**不包含模型权重、实验视频、分析结果或真实人工标注**。缺少模型时仍能启动工作台，但相关推理功能不可用。

## 功能与边界

- 原始视频、掩膜、中线与逐帧指标联动查看；支持虫体聚焦和缩放。
- 个体身份自动重新关联，歧义片段保留人工复核入口。
- 首个清晰帧人工确定头端优先；可按端部渐细程度初始化候选，沿可靠骨架保持方向。
- 画面统一蓝色 H、橙色 T；是否确认由侧栏状态和记录决定，标签不代表解剖真值。
- 正式测量骨架与待复核显示骨架分开；头尾未确认不阻止显示虫体。
- 无物理标定时只输出像素结果；自接触、长遮挡后的身份和头尾仍可能不确定。

当前算法属于研究软件，四段开发视频的连续显示结果不等于独立准确率验证。不能将虚线、自动候选或固定方向直接作为人工真值。规则见 [质量与测量](docs/QUALITY_SCHEMA.md)。

## 目录

```text
WormStudio/
├── studio.py                 # 工作台启动入口
├── worm_pipeline.py          # 流程与命令行入口
├── evaluation.py             # 评测任务工具
├── review_saved_result.py    # 已有结果只读查看
├── src/                      # 分割适配、姿态、身份、头尾、测量、HTTP服务
├── web/                      # 无前端构建依赖的HTML/CSS/JavaScript
├── packages/single-worm-methods/ # 单虫算法包与注明来源的TAPNet推理子集
├── config/                   # 公共参数与本机配置示例
├── tests/                    # 合成数据与接口回归
├── tools/                    # 环境检查、模型下载、源码导出检查
├── docs/                     # 安装、用法、架构、质量、开发、发布
└── .github/workflows/        # CPU/JavaScript检查配置
```

运行时产生的 `models/`、`resources/`、`input_videos/`、`outputs/` 和 `evaluation_data/` 都在 Git 忽略规则内。

## 开发与发布

```powershell
python run_tests.py
node --test tests/annotation-coordinates.test.js tests/test_annotation_readiness.cjs
python tools/check_source.py
```

源码检查面向干净导出目录；开发目录含私人资源时，请先用 `tools/export_source.py` 导出再检查。

[本轮验证](docs/VALIDATION.md) · [代码架构](docs/ARCHITECTURE.md) · [开发与验证](docs/DEVELOPMENT.md) · [发布方法](docs/RELEASE.md) · [贡献说明](CONTRIBUTING.md)

## 第三方与许可

第三方来源、固定提交及 TAPNet Apache-2.0 许可证见 [THIRD_PARTY.md](THIRD_PARTY.md)。项目自有代码尚未指定统一开源许可证；公开上传不等于已授予开源再分发许可。模型和上游源码遵循各自条款。

<p align="center">
  <img src="web/wormstudio-icon.png" width="240" height="240" alt="WormStudio 项目图标">
</p>

<h1 align="center">WormStudio</h1>

<p align="center"><em>🔬 在本机跟踪线虫，逐帧测量体态与运动</em></p>

<p align="center">
  <a href="#下载与启动"><img src="https://img.shields.io/badge/desktop-Windows%20%7C%20macOS-475569?style=flat-square" alt="桌面平台：Windows 和 macOS"></a>
  <a href="docs/QUALITY_SCHEMA.md"><img src="https://img.shields.io/badge/SAM2-Large-007F73?style=flat-square" alt="跟踪模型：SAM2 Large"></a>
  <a href="https://github.com/WRtidE/WormStudio/releases/latest"><img src="https://img.shields.io/github/v/release/WRtidE/WormStudio?style=flat-square&amp;color=2864A0" alt="GitHub 最新发布版本"></a>
  <a href="https://github.com/WRtidE/WormStudio/releases"><img src="https://img.shields.io/github/downloads/WRtidE/WormStudio/total?style=flat-square&amp;color=784BA0" alt="GitHub 发布附件累计下载量"></a>
</p>

<p align="center">
  <strong><a href="https://github.com/WRtidE/WormStudio/releases/download/v1.0.2/WormStudio-Windows-x64.zip">下载 Windows 版</a></strong> ·
  <strong><a href="https://github.com/WRtidE/WormStudio/releases/download/v1.0.1/WormStudio-Mac-arm64.zip">下载 Mac 版</a></strong> ·
  <a href="#用练习视频完成第一次分析">第一次分析</a> ·
  <a href="#windows-可选-gpu-加速">GPU 加速</a>
</p>

在视频的一帧里点选虫体，WormStudio 会跟踪它在整段视频中的位置与姿态。你可以回看叠加了轮廓和中线的标注视频，也可以导出逐帧数据，在 Excel 中比较体长、体宽和运动速度。

## 下载与启动

桌面包已经包含运行环境、识别模型和练习视频。第一次使用，下载与你的电脑对应的**桌面包**即可。

| 电脑 | 下载 | 如何启动 |
| --- | --- | --- |
| Windows 64 位（x64） | [Windows v1.0.2 基础包，约 1.40 GiB](https://github.com/WRtidE/WormStudio/releases/download/v1.0.2/WormStudio-Windows-x64.zip) | 右键 ZIP，选择“全部解压”。打开解压后的 `WormStudio` 文件夹，双击 `WormStudio.exe`。基础包可直接用 CPU 分析。 |
| Apple 芯片 Mac（macOS 14 及以上） | [Mac v1.0.1 桌面包](https://github.com/WRtidE/WormStudio/releases/download/v1.0.1/WormStudio-Mac-arm64.zip) | 解压后将 `WormStudio.app` 拖入“应用程序”。首次启动按包内“先读我.txt”操作，选择“准备并打开”。 |

## 用练习视频完成第一次分析

下面以 Windows v1.0.2 为例。随包的 `practice.avi` 只有 5 帧，适合第一次熟悉标注和导出。

1. **选择视频。** 在左侧文件列表中选择 `practice.avi`，点击 **开始标注**。分析自己的视频时，点击 **导入文件** 或将视频拖入工作台，等导入完成后再选中它。
2. **选择区域和单位。** 练习视频选择 **完整画面** 和 **像素单位**，点击 **下一步：点选线虫**。分析实验视频时，如果需要以微米报告体长，选择 **标尺标定**，填入画面中标尺的实际长度（μm），再用 **手动选两端** 点出标尺的两个端点。
3. **点选并确认虫体。** 在深色虫体内部点一下，等待轮廓出现。如果漏掉一段，在那一段内部补点。如果圈入了背景，选 **排除提示** 并点击误选的位置。轮廓贴合后，点击底部 **确认虫体**。练习视频可以跳过头端标记。
4. **开始分析。** 点击 **下一步：确认并开始**，核对后点 **保存并开始处理**。在 **进度** 页查看跟踪、姿态分析和导出的进展。
5. **回看结果。** 处理完成后打开 **结果 → 查看分析视频**，点击播放或逐帧查看。核对虫体轮廓和中线后，可在同一页导出需要的文件。

## 导出数据、找回历史结果

在 **结果 → 其他文件** 中选择要保存的内容。

| 想做什么 | 选择哪一项 |
| --- | --- |
| 分享或复核识别过程 | **标注视频**，导出带轮廓和中线的 MP4 |
| 在 Excel 中比较每一帧的测量值 | **逐帧指标 CSV** |
| 查看每条虫的汇总指标 | **线虫汇总 CSV** |
| 保存统计图 | 展开 **更多数据与质量记录 → 指标与质心轨迹 PNG**。Windows v1.0.2 可直接显示中文样本名。 |

指标显示 **“—”** 时，点击指标名称可查看原因和有效数据范围。选择像素单位的任务用 px 表示长度。遇到虫体交叉、遮挡或头尾难以分辨的片段，可逐帧复核。如需测量头部角度，请在清晰帧人工确认头端。

关闭软件后，下次启动可在左侧 **历史任务** 中打开之前的结果。Windows 桌面版可通过 **设置 → 通用 → 工作目录 → 在文件夹中打开** 找到本机文件。分析结果在 `outputs` 中。备份时请保留整个工作目录。如果曾使用 **通过视频路径打开**，还需保留对应的原视频。

## Windows 可选 GPU 加速

打开 **设置 → 通用 → 检测 GPU**。先看检测结果，再决定是否下载组件。

| 检测结果 | 接下来怎么做 |
| --- | --- |
| 显示显卡型号，并提示工作台已能使用它计算 | 直接开始分析，现有环境已经可以加速。 |
| 检测到 NVIDIA 显卡，提示工作台当前使用 CPU | 可以继续用 CPU。需要加速时，按下方步骤启用。 |
| 没有可用的 NVIDIA 显卡或驱动 | 使用基础包进行 CPU 分析即可。 |

1. 将 [GPU 分包 1](https://github.com/WRtidE/WormStudio/releases/download/v1.0.2/WormStudio-GPU-cu128-part1.zip) 和 [GPU 分包 2](https://github.com/WRtidE/WormStudio/releases/download/v1.0.2/WormStudio-GPU-cu128-part2.zip) 下载到**同一个文件夹**，保留原文件名和 ZIP 格式。两份共约 3.12 GiB，软件所在磁盘另预留 10 GiB。
2. 等当前分析完成，点击设置里的 **启用离线加速包**。加速窗口打开后，**退出工作台，保留加速窗口**。
3. 在加速窗口点击 **选择已有离线包…**，选择存放两个 ZIP 的文件夹。看到“GPU 加速已启用”后，重新打开工作台，点击 **检测 GPU** 核对显卡型号。

## 分析更多视频

- 要**依次分析多个视频**，一次导入多个文件，按顺序为每个视频确认区域、单位和虫体，最后点击 **确认虫体并开始顺序处理**。
- 同一画面包含多个培养槽时，先勾选 **按培养槽标注** 再开始标注。分别框选各槽并确认虫体，完成后可按槽查看结果。
- 要**统计线虫通过通道的次数**，在左侧切换至 **通道计数**，设置通道数、检测线和两侧边界后点击 **开始计数**。结果支持逐帧回看和导出 Excel。

更详细的标注、批处理和复核操作见[完整使用指南](docs/USER_GUIDE.md)。指标的计算条件见[质量与测量规则](docs/QUALITY_SCHEMA.md)。

## 版本与开发资料

[桌面版发布页与校验清单](https://github.com/WRtidE/WormStudio/releases) · [更新记录](CHANGELOG.md) · [验证记录](docs/VALIDATION.md)

需要从源码运行或参与开发，可参阅 [Windows 安装与启动](docs/INSTALL.md)、[Mac 开发指南](docs/MACOS.md)、[代码架构](docs/ARCHITECTURE.md)、[开发与测试](docs/DEVELOPMENT.md)和[发布指南](docs/RELEASE.md)。项目自有代码尚未指定统一开源许可证。第三方依赖及模型的来源和许可见[第三方说明](THIRD_PARTY.md)。

# 第三方来源与许可

- **SAM2**：来源[官方仓库](https://github.com/facebookresearch/sam2)，使用提交 `2b90b9f5ceec907a1c18123530e92e794ad901a4`。不在本源码包复制源码或权重；按[安装说明](docs/INSTALL.md)从上游安装，并遵守其许可。
- **WormTracer**：来源[上游仓库](https://github.com/yuichiiino1/WormTracer)，原开发环境提交 `8fc27f5a083e7543ab3f2708a7b0867873673e2f`。仅保留接口适配，不分发其源码；使用时核对上游许可。
- **NIU OSW25示例**：参考[有序骨架相关示例](https://github.com/jyoti-bhogal/neuroinformatics_osw/tree/main/python_code_skeletonisation_and_node_selection)。项目内实现包含有序路径、几何筛查与时序适配，不把随机骨架点作为有序中线。
- 其余依赖由pip/Conda安装，许可随各依赖分发。

本发行包不含原实验视频、模型权重或人工标注。自有代码尚未指定统一开源许可证。

- 通道计数采用 WRtidE/WormTracker 的 MOG2、网格、追踪与越线计数核心及默认参数，来源提交 `f2a2a09a6864f77fb3e206f594e40686a7191a37`。https://github.com/WRtidE/WormTracker

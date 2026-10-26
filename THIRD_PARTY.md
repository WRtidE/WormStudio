# 第三方来源与许可

- **TAPNet / TAPNext++**：来源[Google DeepMind TAPNet](https://github.com/google-deepmind/tapnet)，固定提交 `730cda1c730877cfedbe01bf87fb1cadb78a565d`。内置未修改的推理源码子集，保留各文件版权头、[Apache-2.0许可证](packages/single-worm-methods/wormstudio_single/_vendor/tapnet/LICENSE)与[来源清单](packages/single-worm-methods/wormstudio_single/_vendor/tapnet/SOURCE.json)。
- **SAM2**：来源[官方仓库](https://github.com/facebookresearch/sam2)，使用提交 `2b90b9f5ceec907a1c18123530e92e794ad901a4`。不在本源码包复制源码或权重；按[安装说明](docs/INSTALL.md)从上游安装，并遵守其许可。
- **WormTracer**：来源[上游仓库](https://github.com/yuichiiino1/WormTracer)，原开发环境提交 `8fc27f5a083e7543ab3f2708a7b0867873673e2f`。仅保留接口适配，不分发其源码；使用时核对上游许可。
- **NIU OSW25示例**：参考[有序骨架相关示例](https://github.com/jyoti-bhogal/neuroinformatics_osw/tree/main/python_code_skeletonisation_and_node_selection)。项目内实现包含有序路径、几何筛查与时序适配，不把随机骨架点作为有序中线。
- 其余依赖由pip/Conda安装，许可随各依赖分发。

本发行包不含原实验视频、模型权重或人工标注。自有代码尚未指定统一开源许可证；TAPNet的Apache-2.0不自动覆盖其他代码、模型和数据。

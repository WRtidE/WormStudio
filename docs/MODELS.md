# 模型与可选方法

|组件|源码与位置|权重位置|
|---|---|---|
|SAM2 Large|官方源码固定提交，安装到当前Python；默认 `resources/sam2`|`models/sam2_hiera_large.pt`|
|TAPNext++|内置 `packages/single-worm-methods` 与未修改的TAPNet推理子集|`models/tapnextpp_ckpt.pt`|
|SAM2 Tiny|同一SAM2源码，界面显式选择|`models/sam2_hiera_tiny.pt`，单独取得；默认下载器不下载Tiny|
|WormTracer（可选）|外部 `resources/WormTracer/WormTracer19_5`，配置来自19.1|不随本仓库分发|
|NIU、保守拓扑|本项目 `src/`|无独立模型权重|

默认单虫组合只接受SAM2 Large；Tiny可用于其他支持的方法。

[模型清单](models.json)记录官方URL及SHA-256。TAPNext++使用匹配的256输入权重，不是VOT专用512模型。下载命令见 [安装](INSTALL.md)。

SAM2查找顺序：显式 `WORMSTUDIO_SAM2_CHECKPOINT`、项目 `models/`、资源根 `sam2/checkpoints/`、旧工作区的 `TWARDISv0.1/segment-anything-2/checkpoints/`。显式路径必须与所选 Large/Tiny 匹配。

TAPNext源码优先 `WORMSTUDIO_SINGLE_ROOT`，否则内置 `packages/single-worm-methods`；仅旧布局回退到相邻历史算法目录。权重优先显式 `WORMSTUDIO_TAPNEXT_CHECKPOINT`，否则算法包 `models/` 和项目 `models/`。启动可用性检查不将模型反序列化，实际使用前校验哈希。

可选WormTracer：从[上游仓库](https://github.com/yuichiiino1/WormTracer)取得源码，原工作区使用提交 `8fc27f5a083e7543ab3f2708a7b0867873673e2f`。自行核对许可，保持19.5代码及19.1配置目录结构。它不是默认单虫路线的必要依赖。

模型、第三方资源目录、输出与原视频均不进入Git。缓存签名会随算法或路径变化失效；整理后的源码首次重新处理可能重算。已有保存结果仍可查看，不以修改缓存签名绕过验证。

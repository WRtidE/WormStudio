# 模型与可选方法

|组件|源码与位置|权重位置|
|---|---|---|
|SAM2 Large|官方源码固定提交，安装到当前Python；默认 `resources/sam2`|`models/sam2_hiera_large.pt`|
|SAM2 Tiny|同一SAM2源码，界面显式选择|`models/sam2_hiera_tiny.pt`，单独取得；默认下载器不下载Tiny|
|NIU、保守拓扑|本项目 `src/`|无独立模型权重|

默认单虫组合只接受SAM2 Large；Tiny可用于其他支持的方法。

[模型清单](models.json)记录官方URL及SHA-256。多帧身体连续跟踪直接使用分割掩膜，无需额外的点跟踪模型。下载命令见 [安装](INSTALL.md)。

SAM2查找顺序：显式 `WORMSTUDIO_SAM2_CHECKPOINT`、项目 `models/`、资源根 `sam2/checkpoints/`、旧工作区的 `TWARDISv0.1/segment-anything-2/checkpoints/`。显式路径必须与所选 Large/Tiny 匹配。


模型、第三方资源目录、输出与原视频均不进入Git。缓存签名会随算法或路径变化失效；整理后的源码首次重新处理可能重算。已有保存结果仍可查看，不以修改缓存签名绕过验证。

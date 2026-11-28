# 模型与可选方法

|组件|源码与位置|权重位置|
|---|---|---|
|SAM 2.1 Large 线虫适配 v1|单虫点选、逐帧跟踪和扩框复核，使用 `configs/sam2.1/sam2.1_hiera_l.yaml`|本地适配权重 `models/sam2.1_hiera_large_worm_v1.pt`|
|SAM2 Large|官方源码固定提交，安装到当前Python；默认 `resources/sam2`|`models/sam2_hiera_large.pt`|
|SAM2 Tiny|同一SAM2源码，界面显式选择|`models/sam2_hiera_tiny.pt`，单独取得；默认下载器不下载Tiny|
|NIU、保守拓扑|本项目 `src/`|无独立模型权重|

单虫流程支持 SAM2 Large 和 SAM 2.1 Large 线虫适配 v1。把经过验证的 v1 完整权重放到上表位置，在 `config/single_worm_model.local.json` 写入 `{"sam2_model":"sam2.1-large-worm-v1"}`，新单虫任务和每槽一条虫的批处理就会使用它。加载时核对 SHA-256。也可以用 `WORMSTUDIO_SAM21_WORM_CHECKPOINT` 指定该适配权重的位置。任务配置和跟踪摘要会记录实际模型。

没有本地偏好文件时，新任务使用随发行包提供的 SAM2 Large。已保存任务继续使用记录的模型；旧配置中的 `large` 指 SAM 2.0 Large。原版 2.0 的下载器只提供官方权重。

[模型清单](models.json)记录官方URL及SHA-256。多帧身体连续跟踪直接使用分割掩膜，无需额外的点跟踪模型。下载命令见 [安装](INSTALL.md)。

SAM2查找顺序：显式 `WORMSTUDIO_SAM2_CHECKPOINT`、项目 `models/`、资源根 `sam2/checkpoints/`、旧工作区的 `TWARDISv0.1/segment-anything-2/checkpoints/`。显式路径必须与所选 Large/Tiny 匹配。


模型、第三方资源目录、输出与原视频均不进入Git。缓存签名会随算法或路径变化失效；整理后的源码首次重新处理可能重算。已有保存结果仍可查看，不以修改缓存签名绕过验证。

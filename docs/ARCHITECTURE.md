# 代码结构

项目按入口、应用模块、前端、算法包、配置与测试分层。`src/`模块保持现有名称，便于已有命令行和缓存流程兼容；本轮不把正在使用的算法改写为一套新框架。

|职责|主要文件|
|---|---|
|应用与批处理入口|`studio.py`、`worm_pipeline.py`|
|本地HTTP、交互标注、任务调度|`src/manual_worm_annotation.py`、`pipeline_progress.py`、`workspace_sessions.py`|
|分割与方法适配|`track_configured_video.py`、`extract_tapnext.py`、`extract_centerlines.py`、`extract_topology.py`|
|中线几何与时序|`centerline_topology.py`、`crossing_centerline.py`、`temporal_wormtracer.py`|
|头尾初始化和关联|`head_initialization.py`、`head_tail.py`、`head_appearance.py`、`head_temporal.py`|
|身份与质量|`identity_reassociation.py`、`multiworm_quality.py`、`quality_schema.py`、`quality_audit.py`|
|测量与显示|`analyze_phenotypes.py`、`frame_metrics.py`、`display_centerlines.py`、`result_overlay.py`|
|资源、缓存与输出|`resource_paths.py`、`tapnext_support.py`、`run_storage.py`、`cache_integrity.py`|
|独立算法包|`packages/single-worm-methods/wormstudio_single/`|
|网页|`web/studio*.js`、`result-player.js`、HTML/CSS与标注脚本|
|合成验证|`tests/`、算法包`tests/`|

数据流：视频与人工输入 → SAM2跟踪 → 几何/时序姿态 → 个体身份与头尾 → 按质量条件生成指标 → 显示骨架与视频。正式测量与仅显示恢复保持分离。

`head_point_tracks.py`和算法包中的反馈模块属于实验实现，保留源码与合成测试，不意味着默认启用或已证明优于现行方法。发布不包含历史模型缓存、示例研究视频或开发报告。

前端使用原生JavaScript和本地Python服务，没有Node生产运行依赖。Node只用于前端测试。GPU模型按任务加载，不随页面源码打包。

布局由 `web/workspace-links.js` 管理，主工作台与标注/进度 iframe 共用一个存储实例。`GET/POST /api/layout` 使用当前会话令牌校验写入，校验有限数值和组件键，原子合并到 `config/layout.local.json`。浏览器缓存只作为启动暂存，服务端已有记录优先，布局不依赖临时端口。窄视口把面板放进可切换的工具区，保留桌面几何。布局重置不触碰标注与分析数据。

文件树由 `web/library-tree.js` 呈现，`POST /api/library-tree` 校验会话后执行工作区操作。`workspace_library.py` 的 v2 索引记录 folders、parent、order、collapsed，以及文件 removed 标记；旧 relativePath 索引自动迁移。所有树操作只改变引用，历史目录扫描使用 auto 注册，避免恢复已移除条目。活动分析源文件受移除保护。

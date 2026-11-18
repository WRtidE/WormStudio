# 代码结构

项目按入口、应用模块、前端、算法包、配置与测试分层。`src/`模块保持现有名称，便于已有命令行和缓存流程兼容；本轮不把正在使用的算法改写为一套新框架。

|职责|主要文件|
|---|---|
|Electron 窗口与运行时|`tools/electron/main.cjs`、`src/electron_desktop.py`|
|应用与批处理入口|`studio.py`、`worm_pipeline.py`|
|本地HTTP、交互标注、任务调度|`src/manual_worm_annotation.py`、`pipeline_progress.py`、`workspace_sessions.py`|
|分割与方法适配|`track_configured_video.py`、`extract_body_sequence.py`|
|中线几何与时序|`centerline_topology.py`、`body_geometry.py`、`motion.py`、`body_sequence.py`|
|头尾初始化和关联|`head_initialization.py`、`head_tail.py`、`head_appearance.py`、`head_temporal.py`|
|身份与质量|`identity_reassociation.py`、`multiworm_quality.py`、`quality_schema.py`、`quality_audit.py`|
|测量与显示|`analyze_phenotypes.py`、`frame_metrics.py`、`display_centerlines.py`、`result_overlay.py`|
|资源、缓存与输出|`resource_paths.py`、`run_storage.py`、`cache_integrity.py`|
|网页|`web/studio*.js`、`result-player.js`、HTML/CSS与标注脚本|
|平台边界|`platforms/`（设备选择、桌面外壳、内存压力与文件发布）；其它模块不得判断宿主系统|
|合成验证|`tests/`、算法包`tests/`|

数据流：视频与人工输入 → SAM2跟踪 → 几何/时序姿态 → 个体身份与头尾 → 按质量条件生成指标 → 显示骨架与视频。正式测量与仅显示恢复保持分离。

身体序列的灰度帧与掩膜由 `body_frame_cache.py` 统一解码。两者合计不超过 256 MiB 时使用内存数组，更大时使用可由系统回收页面的磁盘映射；不降采样、不跳帧，也不将全序列求解改为独立分段。映射文件采用匿名或关闭即删除的临时文件，工作进程被终止时也由系统释放。当前方法直接使用求解器保存的头尾证据；旧基线的外观特征仅在对应兼容分支中提取。

`body_sequence_solver.signature()` 覆盖图像外观、头端标注归一化、基础中线测量与输入缓存依赖。更新代码后，旧结果保留供查看；继续计算时按签名检查决定重算，不绕过已有的输出内容校验。

复核派生任务通过 `run_storage.copy_tracking_snapshot()` 共享 `sam2-tracking/masks/` 下的 PNG 数据块，其它文件独立复制。所有程序内掩膜改写都通过 `write_png()` 调用 `replace_bytes()` 替换已有文件，使写入只影响当前任务；新的输出文件走普通写入路径。不支持硬链接时退回完整复制。后续新增掩膜写入入口也必须使用该替换函数，不能原地修改共享文件；对原始 PNG 的外部手工编辑不受程序的写入隔离保护。

历史中线方法的实现已移除，旧结果只能以只读方式查看；`centerline_methods.py` 保留其名称映射用来定位历史结果目录。发布不包含历史模型缓存、示例研究视频或开发报告。

Electron 主进程启动本地 Python 服务，并在 Chromium 窗口中载入工作台。页面使用原生 JavaScript 和 Canvas，渲染进程启用沙箱并关闭 Node 集成。Node 运行时随 Electron 一起分发。GPU 模型按任务加载。

布局由 `web/workspace-links.js` 管理，主工作台与标注/进度 iframe 共用一个存储实例。`GET/POST /api/layout` 使用当前会话令牌校验写入，校验有限数值和组件键，原子合并到 `config/layout.local.json`。浏览器缓存只作为启动暂存，服务端已有记录优先，布局不依赖临时端口。窄视口把面板放进可切换的工具区，保留桌面几何。布局重置不触碰标注与分析数据。

文件树由 `web/library-tree.js` 呈现，`POST /api/library-tree` 校验会话后执行工作区操作。`workspace_library.py` 的 v2 索引记录 folders、parent、order、collapsed，以及文件 removed 标记；旧 relativePath 索引自动迁移。所有树操作只改变引用，历史目录扫描使用 auto 注册，避免恢复已移除条目。从文件树移除正在使用的视频或其上级文件夹时，当前任务继续通过原路径读写，可通过“返回当前分析”继续。

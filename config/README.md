# 配置

`phenotype_config.json`与`motion_constraints.json`为共享算法参数。修改阈值需要重新验证，不把它们当作准确率。

以下是本机文件，不提交Git：

- `python_path.txt`：可选，固定解释器路径；不提供时使用当前Python。
- `target_videos.json`：可选，常用视频列表。复制 `target_videos.example.json` 并填写自己的视频。
- `evaluation_dataset.json`：可选，评测数据目录。复制对应示例，先通过 `evaluation.py` 建立真实数据任务。

未配置常用视频不影响界面导入；未配置评测集时评测页会说明缺少任务。

`motion_constraints.json`默认空约束；真实实验的人工运动约束保存在忽略提交的`motion_constraints.local.json`，运行时优先读取本机文件。

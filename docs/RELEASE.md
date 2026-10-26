# 发布与上传GitHub

上传本仓库源码目录，不上传原始研究工作区。使用GitHub网页上传时，将本目录内的文件和子目录上传；GitHub Release附件则可使用源码ZIP。

发布前可执行：

```powershell
python tools/check_source.py
python run_tests.py
node --test tests/annotation-coordinates.test.js tests/test_annotation_readiness.cjs
```

从含本机数据的开发目录生成一个新的干净副本：

```powershell
python tools/export_source.py --output ../WormStudio-source
```

导出器采用显式清单，拒绝覆盖既有目录和ZIP；不遍历运行输出链接。它校验文件大小、常见私人路径/凭据模式、文档本地链接，生成逐文件SHA-256清单及ZIP并校验压缩内容。这是有限模式扫描，不能代替全面的秘密审计。

## 不进入仓库

- 模型、视频、实验结果、评测标注、验证日志、虚拟环境和缓存。
- `config/python_path.txt`、`config/target_videos.json`、`config/evaluation_dataset.json`；只提交示例。
- `.git`嵌套仓库、开发报告截图和本地归档。

已保留第三方许可与来源。项目自有代码的开源许可证由维护者决定，当前未擅自添加MIT或其他授权。上传到GitHub与选择开源许可证是两个独立动作。

本次整理只准备本地源码，未创建远程仓库、提交或推送。

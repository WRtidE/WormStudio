# 发布与上传GitHub

源码仓库为 [WRtidE/WormStudio](https://github.com/WRtidE/WormStudio)。开发工作区可同时存放自己的视频、模型和输出，上传源码时使用已复核的 Git 提交。需要通过 GitHub Release 分发源码快照时，可附上导出器生成的 ZIP。

## 生成干净的源码副本

在仓库根目录执行，输出目录须为新目录：

```sh
python tools/export_source.py --output ../WormStudio-source
```

## 验证导出结果

在导出目录中使用已安装依赖的 Python 3.11 环境运行以下命令。单虫算法包合成测试的命令见[开发与验证](DEVELOPMENT.md)。

```sh
python tools/check_source.py
python tools/check_environment.py --code-only
python run_tests.py
node --test tests/*.js tests/*.cjs
```

导出器采用显式清单，拒绝覆盖既有目录和ZIP；不遍历运行输出链接。它校验文件大小、常见私人路径/凭据模式、文档本地链接，生成逐文件SHA-256清单及ZIP并校验压缩内容。这是有限模式扫描，不能代替全面的秘密审计。

## 不进入仓库

- 模型、视频、实验结果、评测标注、验证日志、虚拟环境和缓存。
- `config/python_path.txt`、`config/target_videos.json`、`config/evaluation_dataset.json`；只提交示例。
- `.git`嵌套仓库、开发报告截图和本地归档。

已保留第三方许可与来源。项目自有代码的开源许可证由维护者决定，当前未擅自添加MIT或其他授权。上传到GitHub与选择开源许可证是两个独立动作。

本版本的验证范围和结果见[源码验证记录](VALIDATION.md)。

## 桌面软件更新

正式版本号取自 `tools/electron/package.json` 的 `version`。构建时先改成新的三段版本号，例如 `1.1.0`。默认源为 WRtidE/WormStudio 的 GitHub Releases，私有仓库无法供普通用户检查。使用公开 Releases 时，发布同版本的稳定标签（如 `v1.1.0`），正文填写面向用户的更新说明。上传完整桌面安装包，Mac 使用 `WormStudio-Mac-arm64.zip`，Windows x64 使用 `WormStudio-Windows-x64.exe` 或同名 ZIP。源码归档不会被识别成安装包。

默认读取 GitHub 发布列表第一页。GitHub Release 的单个附件须小于 2 GiB。当前含模型的 Mac 离线 ZIP 超过该上限，整包应使用独立的 HTTPS 下载地址。私有仓库或大包可以在打包前给该 package.json 添加 `updateFeed`，指向公开 HTTPS JSON 文件，安装包可放在单独的下载站。测试环境也可用 `WORM_STUDIO_UPDATE_FEED` 指定该地址。格式如下，示例大小和 URL 请替换为真实值。可在 asset 中增加 `digest`（`sha256:` 加文件哈希）以校验下载内容。

```json
[{
  "tag_name": "v1.1.0",
  "body": "• 第一项更新\n• 第二项更新",
  "assets": [{
    "name": "WormStudio-Mac-arm64.zip",
    "browser_download_url": "https://downloads.example.org/v1.1.0/WormStudio-Mac-arm64.zip",
    "size": 123456
  }]
}]
```

启动检查默认开启，每 24 小时最多尝试一次，手动检查可随时重试。安装包下载到系统下载目录；用户可在任务结束后自行安装。

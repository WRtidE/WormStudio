# 贡献说明

## 把交付目录放上 GitHub

交付目录中与 `studio.py` 同级的 `README.md` 就是仓库首页。完整保留 `.github/`、`.gitattributes` 和 `.gitignore`；不要将外层研究工作区、离线 App ZIP 或源码 ZIP 本身提交为仓库内容。

- **更新已有仓库：** 克隆已有仓库，把交付目录的内容复制到克隆目录，保留克隆自带的 `.git/`。在该克隆中执行 `git status --short`，审阅变化后提交并推送；不需要强制推送或重建历史。
- **新建空仓库：** 在交付目录执行 `git init -b main`、`git add .`、`git diff --cached --stat`。检查后执行 `git commit -m "Add WormStudio Mac desktop app"`；将 GitHub 空仓库地址设置为 origin，再执行 `git push -u origin main`。
- **换到 Mac：** 克隆刚上传的仓库，按 [Mac 开发指南](docs/MACOS.md) 从源码运行和修改应用。

源码导出自带的 `SOURCE_MANIFEST.json` 用于本次本地校验，已由 Git 忽略。

先阅读[架构](docs/ARCHITECTURE.md)和[开发检查](docs/DEVELOPMENT.md)。修复说明应包含触发条件、行为变化、验证和未解决问题。

保持原视频和历史结果可恢复；不要提交模型、个人路径或虚构人工标签。区分显示恢复、正式测量、自动候选与真实确认。使用合成测试覆盖故障，不以开发视频覆盖率代替精度。

项目许可证尚未由维护者决定；提交较大贡献前应先明确贡献的授权与来源。

# 开发与验证

编码规范见项目根目录的 [AGENTS.md](../AGENTS.md)。复用已有共享模块，减少重复遍历和无用复制；对外部输入、保存文件及实际资源故障保留校验和错误传播。任务配置、复核修订和线程快照需要独立可变状态时保留深拷贝。

按 [安装说明](INSTALL.md) 同步 CPU 或 CUDA 环境后，在仓库根目录运行：

```powershell
uv lock --check
uv pip check
uv run --no-sync run_tests.py
$tests = (Get-ChildItem tests/*.js, tests/*.cjs -File).FullName
node --test @tests
```

应用测试使用合成视频、临时目录和受控的模型替身，测试质量边界、标注接口和指标一致性；不需要原工作区视频或权重。

在线安装的 JavaScript 回归包含暂停续传、Range 被忽略、错误长度与校验和、原生运算失败后切换 CPU，以及不同应用版本的组件复用；同时覆盖缓存校验期间取消、验证后停止、失败重试清除旧结果、清理错误报告及窗口启动定时器。`test_online_package.py` 检查组件打包的确定性、裁剪边界、完整首包与组件的分离及发布校验和。真实组件的本机 HTTP 集成测试与准备界面验证使用 `tools/release/smoke_online.cjs`，命令见[发布说明](RELEASE.md)；不要把测试服务器地址写入发行包。

日常启动使用 `uv run --no-sync studio.py` 或 Windows 的 `start.bat`，保留已经同步的 CPU/CUDA 选择。修改依赖时编辑 `pyproject.toml`，运行 `uv lock`，再按原来的 `--extra cpu` 或 `--extra cuda` 同步并测试；同时提交配置和锁文件。不要手动修改 `uv.lock`，也不要用 pip 向该环境补装未记录的运行依赖。发布工具需要 pip / wheel 时，使用 `uv sync --locked --extra cpu --group release`；CUDA 构建环境相应改为 `--extra cuda`。

较小的 CUDA 12.6 构建使用 `--extra cuda126`，更新时继续使用同一选项；与 `cpu`、`cuda` 互斥。包体积及实机验证范围见 [安装说明](INSTALL.md)和[验证记录](VALIDATION.md)。发布安装器的编译材料裁剪只用于离线运行时，不对 uv 开发环境实施。

`tools/check_source.py` 检查纯源码，不允许运行数据或虚拟环境。CI 在安装 `.venv` 前执行该检查；本地开发安装环境后，按 [发布指南](RELEASE.md) 导出源码并检查导出目录。导出器包含 `pyproject.toml`、`uv.lock`、`.python-version`，排除 `.venv` 和本机配置。

`.github/workflows/tests.yml`在 Linux 和 Windows x64 上运行 CPU 与 JavaScript 回归。Windows 任务还从中文路径启动桌面版并检查工作台窗口、后端与退出。其配置已随源码提供；上传前不声称远程CI已通过。各次本地验证的平台、测试范围和结果见[验证记录](VALIDATION.md)。

改变几何、身份、头尾或资源路由时，增加能捕获真实错误的测试。不要为格式或文本修改添加镜像式测试。资源路由的新测试覆盖缺省Python、本机覆盖优先级、标准模型目录和内置算法包。

## 数据保护

不在源代码中加入个人路径、访问令牌、实验记录、权重或真实标注。不要为增加接受率放宽正式测量条件。修改算法签名会使缓存失效，不能为了复用旧缓存绕过校验。

如要提交复现实验材料，应单独记录数据权限、来源、标注状态、固定版本和失败结果；源码测试通过不等于算法精度验证。

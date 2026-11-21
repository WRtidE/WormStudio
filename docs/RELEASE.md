# 源码上传与桌面发布

源码仓库为 [WRtidE/WormStudio](https://github.com/WRtidE/WormStudio)。开发工作区可同时存放自己的视频、模型和输出，上传源码时使用已复核的 Git 提交。需要通过 GitHub Release 分发源码快照时，可附上导出器生成的 ZIP。

## 更新现有 GitHub 仓库

在含有 `studio.py`、`.git/` 和 `.github/` 的仓库根目录运行以下命令。先确认远程地址、工作区状态以及即将上传的提交。

```sh
git remote -v
git status --short --branch
git log --oneline origin/main..HEAD
git diff --stat origin/main..HEAD
git diff --check origin/main..HEAD
```

维护者确认后执行 `git push origin main`。推送后可在仓库的 Actions 页面查看该提交的 Windows 和 Linux 检查结果。若远端已有新提交，先用 `git fetch origin` 更新远端引用，再核对分支差异。

需要与提交完全一致的源码 ZIP 时，从干净提交导出。归档包含 CI 配置、公共参数、测试和第三方许可。

```sh
git archive --format=zip --prefix=WormStudio/ --output=../WormStudio-source.zip HEAD
```

## 导出带逐文件校验清单的源码

在仓库根目录执行，输出目录须为新目录：

```sh
uv run --no-sync tools/export_source.py --output ../WormStudio-source
```

## 验证导出结果

在导出目录先检查纯源码，再从 uv 锁文件安装 CPU 环境并测试。源码检查必须在创建 `.venv` 前执行。其它测试命令见[开发与验证](DEVELOPMENT.md)。

```sh
uv run --no-project --python 3.11.15 tools/check_source.py
uv sync --locked --extra cpu --managed-python
uv pip check
uv run --no-sync tools/check_environment.py --code-only
uv run --no-sync run_tests.py
node --test tests/*.js tests/*.cjs
```

导出器在 Git 工作区中从已跟踪路径读取当前磁盘内容，适合验证尚未提交的修改。在已解压的干净源码包中，它使用内置文件和目录清单。工具检查文件大小、运行数据、本机配置、常见凭据模式和文档本地链接，生成 `SOURCE_MANIFEST.json`，再逐文件核对 ZIP 中的 SHA-256。

## 不进入仓库

- 模型、视频、实验结果、评测标注、验证日志、虚拟环境和缓存。
- `config/python_path.txt`、`config/target_videos.json`、`config/evaluation_dataset.json`；只提交示例。
- `.git`嵌套仓库、开发报告截图和本地归档。

已保留第三方许可与来源。项目自有代码的开源许可证由维护者决定，当前未擅自添加MIT或其他授权。上传到GitHub与选择开源许可证是两个独立动作。

本版本的验证范围和结果见[源码验证记录](VALIDATION.md)。

## 桌面软件更新

源码开发环境已迁移至 uv；已发布离线包的 wheel 清单和历史 Conda 便携构建配方继续单独维护。构建时需要宿主 pip / wheel 的工具，在项目环境同步时加 `--group release`，并使用 `uv run --no-sync` 运行。不要直接复制开发 `.venv` 作为可搬移的桌面运行时。

### 在线安装版

`tools/release/build_online.py` 将已验证的便携桌面构建拆成桌面首包、公共运行环境、CPU / NVIDIA 运行组件及模型。首包只含 Electron、应用代码、界面和练习视频；首次启动自动检测硬件并下载所选后端。NVIDIA 自动选择要求驱动至少为 572.61、计算能力在 5.x–12.x 范围内，安装后还会实际执行 CUDA 矩阵运算和 torchvision NMS。检测不到兼容驱动时使用 CPU；加速验证失败时可选择“改用 CPU”。Apple Silicon 使用原生环境，应用按现有设备策略选择 Metal。

先按原构建流程产生可搬移的 CPU 桌面目录，再从干净源码导出构建在线版。输入不能是开发虚拟环境；CUDA wheel 必须匹配 `uv.lock` 中的 Windows CPython 3.11 CUDA 12.8 哈希。示例路径由维护者替换：

```powershell
uv run --no-sync tools/release/build_online.py --source source-export --desktop desktop-build/WormStudio --gpu-wheels wheels --output online-release --base-url https://downloads.example.org/wormstudio/components
```

输出包含 `WormStudio/`、`WormStudio-Windows-x64.zip`、`components/` 和 `SHA256SUMS.txt`。Mac 在原生构建机上以 `--target darwin-arm64 --desktop desktop-build/WormStudio.app` 生成，其签名、公证仍按 Mac 发布规范执行；Windows 本地测试不代表 Mac 已验证。

Windows GPU 组件直接使用 `uv.lock` 固定的官方 wheel，构建时校验本机 wheel 并生成包含文件大小、SHA-256 和保留文件解压大小的清单。当前下载顺序为上海交大 PyTorch 镜像、阿里 PyTorch 镜像、锁文件中的官方地址；失败切换仍使用相同内容哈希和下载断点。安装器在公共 Python 就绪后解压 wheel，省去编译头文件和链接归档，保留运行库与许可证，验证 GPU 运算后再启用。GPU wheel 不作为 GitHub 大附件重复上传。

依赖和模型未变化时，可用 `--reuse-components 旧版在线清单.json` 配合旧版在线桌面目录构建更新，保留公共环境、CPU 和模型的原地址与哈希。必须保留这些地址对应的旧附件；新发布的校验清单只列本次实际生成的文件。GPU 从旧版归档切换为原始 wheel 时，两种文件内容不同，归档的未完成断点不能直接用于 wheel；已有公共环境、CPU 和模型缓存继续复用。

**发布顺序：**先将新生成的 `components/` 内文件原名上传至 `--base-url` 对应的公开 HTTPS 目录，验证新地址和复用地址，再分发桌面 ZIP。使用 GitHub Release 时把新组件作为发布附件上传，base URL 对应该标签的 `/releases/download/标签`。公共和 CPU 归档按文件分组，每个组件的未压缩文件总量不超过 1400 MiB，模型也是单独附件；GPU wheel 使用上面的镜像地址。组件清单内置在首包，不执行远端安装命令，不安装系统驱动。生成本地文件不会使下载地址自动可用。

组件保存在 Electron 用户数据目录的 `components/` 下，与实验工作区、程序目录分离。下载使用 `.partial`、HTTP Range 和 SHA-256；不支持 Range 的服务器会重新下载当前文件，校验失败的文件不会启用。关闭、暂停和网络失败保留进度。临时环境解压并通过运行验证后才会启用，旧环境继续保留。模型和下载文件按内容哈希保存，运行目录标识只依赖平台与组件哈希，因此更新应用版本或下载地址不强制重装环境。切换 CPU 后，可在“设置 → 通用 → 检测 GPU”安排下次启动重新准备 GPU，当前任务不受影响。

发布前验证真实组件的续传、安装和离线复用：

```powershell
node tools/release/smoke_online.cjs online-release validation/online-profile
```

GPU wheel 和复用组件位于其它目录时，将目录依次附在命令末尾，例如 `node tools/release/smoke_online.cjs online-release validation/online-profile old-release/components wheels`。工具仅将清单中的文件映射到本机测试服务器。

测试工具将组件 HTTPS 地址映射到本机 HTTP 服务器，实际传输完整文件、主动中断并续传、执行原生运算验证，再禁止联网测试复用。用 Electron 运行同一脚本可查看真实准备界面；它不修改正式包的下载地址，生产端仍只接受 HTTPS。

### Windows 基础安装与可选 NVIDIA 加速

以下为兼容已有用户保留的离线发布方式；新在线版见前一节。

当前离线 GPU 安装器校验完整官方 wheel 后，只解压运行所需文件，省去 `torch/lib/*.lib` 与 `torch/include/` 下的编译材料，同时保留许可证。对固定 CUDA 12.8 wheel 可减少约 853 MiB 的安装占用；DLL、Python 模块、包元数据及原始 wheel 的 SHA-256 校验保留。此运行时用于已编译的算子，不用于构建新 C++/CUDA 扩展。uv 开发环境保持完整，不对其文件作手动裁剪。

这项裁剪减少安装体积，既有官方 wheel 和已发布的两份 ZIP 下载大小不变。源码开发可按 [安装说明](INSTALL.md) 选择较小的 `cuda126` extra；已有离线 GPU 分包仍按固定的 CUDA 12.8 哈希与版本安装，不能把 CUDA 12.6 wheel 替换进旧分包。

v1.0.2 的 Windows x64 基础包为 `WormStudio-Windows-x64.zip`。维护者用下列命令生成两个 GPU 发布附件，连同基础包和 SHA256SUMS.txt 一起上传。两份 GPU 附件各小于 2 GiB。使用者将它们放在同一文件夹，在工作台设置中选择“启用离线加速包”，退出工作台后在加速窗口选择该文件夹。软件会读取两个 ZIP、校验原始组件并运行显卡验证。软件所在磁盘另预留 10 GiB 供临时读取和安装。

```powershell
python tools/release/package_windows_kits.py --wheels wheels --split-gpu --output delivery
```

下列步骤用于历史 v1.0.0 安装程序和单个离线 ZIP。

v1.0.0 可以分别交付基础包和离线加速包。基础包内的“安装 WormStudio.cmd”启动已发布的 x64 安装程序，保留安装进度窗口并跳过 CUDA 下载。CPU 用户安装完即可分析。已有 v1.0.0 的用户可以直接领取加速包。

制作交付包时，从仓库的 [v1.0.0 发布页](https://github.com/WRtidE/WormStudio/releases/tag/v1.0.0) 获取 EXE。用下面的命令下载一次官方 Windows 加速组件，并在新的输出目录生成两份 ZIP。打包工具对安装程序和两个 wheel 分别校验 SHA-256。也可以只给 `--base` 或 `--wheels` 生成其中一个包。

```powershell
python -m pip download --only-binary=:all: --no-deps --platform win_amd64 --python-version 3.11 --implementation cp --abi cp311 -r requirements-cuda.txt -d wheels
python tools/release/package_windows_kits.py --base WormStudio-Windows-x64.exe --wheels wheels --output delivery
```

GPU 包约 3.12 GiB，适合使用共享盘、U 盘或独立下载地址分发，超过 GitHub Release 单附件上限。使用者完整解压，结束任务并退出工作台，双击“启用 NVIDIA 加速.cmd”。安装过程校验官方文件，解压到独立目录并执行显卡运算，通过后才启用。软件安装盘应预留 6 GiB 空间，下载与解压分发 ZIP 另计。重复启用会先检查已有加速。当前组件适配 Windows x64、Python 3.11、PyTorch 2.7.0 和 torchvision 0.22.0。

从当前源码构建 Windows 桌面版时，`tools/release/build_desktop.py` 会把检测代码装入应用。分发完整的 WormStudio 目录，用户解压并打开 WormStudio.exe，即可在“设置 → 通用 → 检测 GPU”查看结果。检测在工作台的运行环境中执行 GPU 运算。仅在当前基础版适用时显示可选的离线加速入口。

前述 `package_windows_kits.py --base` 封装的是历史 v1.0.0 安装程序。对于尚未升级的 v1.0.0 用户，`--detector` 仍可生成兼容该版本的外部检测工具。

### 发布版本与更新检查

正式版本号取自 `tools/electron/package.json` 的 `version`。构建时先改成新的三段版本号，例如 `1.1.0`。默认源为 WRtidE/WormStudio 的 GitHub Releases，私有仓库无法供普通用户检查。使用公开 Releases 时，发布同版本的稳定标签（如 `v1.1.0`），正文填写面向用户的更新说明。上传完整桌面安装包，Mac 使用 `WormStudio-Mac-arm64.zip`，Windows x64 使用 `WormStudio-Windows-x64.exe` 或同名 ZIP。源码归档不会被识别成安装包。

默认读取 GitHub 发布列表第一页。GitHub Release 的单个附件须小于 2 GiB。v1.0.1 的含模型 Mac 离线 ZIP 约 1.25 GB，可直接作为 Release 附件。后续若超出该上限，整包应使用独立的 HTTPS 下载地址。私有仓库或大包可以在打包前给该 package.json 添加 `updateFeed`，指向公开 HTTPS JSON 文件，安装包可放在单独的下载站。测试环境也可用 `WORM_STUDIO_UPDATE_FEED` 指定该地址。格式如下，示例大小和 URL 请替换为真实值。可在 asset 中增加 `digest`（`sha256:` 加文件哈希）以校验下载内容。

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

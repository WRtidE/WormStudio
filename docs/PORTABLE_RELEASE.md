# 实验人员便携交付（2026-11-03）

交付目录为工作区 delivery/WormStudio-Windows-CPU-GPU。完整复制目录给使用者；入口为启动软件.bat，说明为先读我.html。Windows x64，CPU 或 NVIDIA CUDA；其他 GPU 当前走 CPU。包含 Python 3.11、PyTorch 2.7 CUDA 12.8、SAM2 Large、TAPNext++。

默认 auto 自动选择；使用CPU启动.bat 可强制 CPU，使用GPU启动.bat 在 GPU 不可用时明确报错。CPU 使用 float32，CUDA 使用设备支持的混合精度。同一模型的结果可能有小幅浮点差异。GPU 内存不足时由用户重新以 CPU 启动，不静默切换已运行的任务。

运行环境来自已验证的环境，以 conda-pack no-archive 导出。原环境 setuptools/wheel 由 pip 替换，打包使用 ignore_missing_files=True；交付环境实测导入、模型推理和导出通过。移除了 SAM2 的外部 editable 路径，改为包内 resources/sam2。首次启动执行 conda-unpack，使用者应在首次运行前放到最终目录。

本机 5 帧合成练习视频的 CPU/GPU 全流程、隐藏 GPU 后自动 CPU、首帧点选、真实线虫短片验证见工作区 delivery-validation。合成练习无生物准确率含义。尚未在另一台物理电脑完成用户验收；不能据此保证所有 Windows 驱动/硬件组合。长视频内存和耗时未给出最低配置承诺。

交付运行包与源码导出不同：运行包包含模型和依赖，自有代码的统一许可证尚未指定；第三方说明和许可证保留。源码导出工具仍排除模型和实验数据。

## 首次启动修复（2026-11-03）

初版清理 SAM2 editable finder 和 direct_url.json 后，conda-unpack 的清单仍包含这两个已删除文件，导致首次启动失败。已通过 tools/repair_windows_runtime.py 精确移除两条缺失记录；其他缺失文件仍报错，不能整体跳过初始化。修复后的实际首次 unpack、CPU 环境检查、启动软件.bat 和工作台 HTTP 200 已通过。打包后必须验证首次启动，不能只验证运行环境内的直接推理。

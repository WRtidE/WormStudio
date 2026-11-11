#!/bin/bash
set -u
res="$(cd "$(dirname "$0")/../Resources" && pwd)" || exit 1
notify_error() {
  /usr/bin/osascript - "$1" <<'OSA'
on run argv
 display alert "WormStudio" message (item 1 of argv) as critical
end run
OSA
}
major="$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d . -f 1)"
if [[ "$(/usr/bin/uname -m)" != "arm64" || "$major" -lt 14 ]]; then
  notify_error "此版本需要 Apple 芯片和 macOS 14 或更新版本。"
  exit 1
fi
base="$HOME/Library/Application Support/WormStudio"
mkdir -p "$base/logs" || { notify_error "无法创建用户数据目录：$base"; exit 1; }
log="$base/logs/$(date +%Y%m%d-%H%M%S)-$$.log"
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 MPLBACKEND=Agg
"$res/python/bin/python3.11" -I -B "$res/desktop.py" "$res" "$base" "$log" >"$log" 2>&1 || {
  notify_error "工作台未能启动。详细原因已保存在：$log"
  exit 1
}

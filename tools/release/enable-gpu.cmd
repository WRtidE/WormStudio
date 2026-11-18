@echo off
setlocal DisableDelayedExpansion
set "WS_GPU_SCRIPT=%~dp0windows_gpu.py"
powershell.exe -NoProfile -STA -Command ^
  "Add-Type -AssemblyName System.Windows.Forms;" ^
  "$root=[IO.Path]::GetDirectoryName($env:WS_GPU_SCRIPT);" ^
  "if (-not [IO.File]::Exists([IO.Path]::Combine($root,'WormStudio.exe'))) { try { $root=Get-ItemPropertyValue -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\org.wormstudio.desktop_is1' -Name InstallLocation -ErrorAction Stop } catch { $root=[IO.Path]::Combine($env:LOCALAPPDATA,'Programs','WormStudio') } };" ^
  "if (-not [IO.File]::Exists([IO.Path]::Combine($root,'WormStudio.exe'))) { $d=New-Object System.Windows.Forms.OpenFileDialog; $d.Filter='WormStudio.exe|WormStudio.exe'; if ($d.ShowDialog() -ne 'OK') { exit 1 }; $root=[IO.Path]::GetDirectoryName($d.FileName) };" ^
  "$python=[IO.Path]::Combine($root,'resources\analysis\python\pythonw.exe');" ^
  "if (-not [IO.File]::Exists($python)) { [Windows.Forms.MessageBox]::Show('Please install WormStudio for Windows x64 first.'); exit 1 };" ^
  "$rt=[IO.Path]::GetDirectoryName($python); $env:Path=$rt+';'+[IO.Path]::Combine($rt,'Library','bin')+';'+$env:Path; & $python -I -B $env:WS_GPU_SCRIPT"

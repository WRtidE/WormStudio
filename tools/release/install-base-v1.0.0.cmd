@echo off
setlocal DisableDelayedExpansion
set "WS_BASE=%~dp0installer\WormStudio-Windows-x64.exe"
if not exist "%WS_BASE%" set "WS_BASE=%~dp0WormStudio-Windows-x64.exe"
if not exist "%WS_BASE%" (
  echo Please extract the complete WormStudio base package first.
  pause
  exit /b 1
)
start "" /wait "%WS_BASE%" /SILENT /SP- /NORESTART /NOCLOSEAPPLICATIONS /MERGETASKS="!cuda"
if errorlevel 1 exit /b %ERRORLEVEL%
if exist "%~dp0GPU.cmd" call "%~dp0GPU.cmd"
exit /b 0

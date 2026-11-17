@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist "config\runtime.local.cmd" call "config\runtime.local.cmd"
set "STUDIO_PYTHON="
if defined WORM_STUDIO_PYTHON set "STUDIO_PYTHON=%WORM_STUDIO_PYTHON%"
if not defined STUDIO_PYTHON if exist "config\python_path.txt" for /f "usebackq delims=" %%P in (`powershell.exe -NoProfile -Command "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; [IO.File]::ReadAllText((Join-Path (Get-Location) 'config\python_path.txt')).Trim()"`) do set "STUDIO_PYTHON=%%P"
if not defined STUDIO_PYTHON if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set "STUDIO_PYTHON=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined STUDIO_PYTHON if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "STUDIO_PYTHON=%CONDA_PREFIX%\python.exe"
if not defined STUDIO_PYTHON if exist ".venv\Scripts\python.exe" set "STUDIO_PYTHON=%CD%\.venv\Scripts\python.exe"
if not defined STUDIO_PYTHON for /f "delims=" %%P in ('where python 2^>nul') do if not defined STUDIO_PYTHON set "STUDIO_PYTHON=%%P"
if not exist "%STUDIO_PYTHON%" (
  echo Python not found. Activate your environment or set WORM_STUDIO_PYTHON.
  if not defined CI pause
  exit /b 1
)
for %%I in ("%STUDIO_PYTHON%") do set "STUDIO_ENV=%%~dpI"
set "WORM_STUDIO_PYTHON=%STUDIO_PYTHON%"
set "PATH=%STUDIO_ENV%;%STUDIO_ENV%Library\bin;%STUDIO_ENV%Scripts;%PATH%"
if "%~1"=="" (
  "%STUDIO_PYTHON%" "%~dp0studio.py"
) else (
  "%STUDIO_PYTHON%" "%~dp0worm_pipeline.py" %*
)
set "STUDIO_EXIT=%ERRORLEVEL%"
if not "%STUDIO_EXIT%"=="0" if not defined CI pause
popd
exit /b %STUDIO_EXIT%

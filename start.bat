@echo off
setlocal
cd /d "%~dp0"
set "STUDIO_PYTHON="
if exist "%~dp0config\python_path.txt" set /p "STUDIO_PYTHON="<"%~dp0config\python_path.txt"
if defined WORM_STUDIO_PYTHON set "STUDIO_PYTHON=%WORM_STUDIO_PYTHON%"
if not defined STUDIO_PYTHON for /f "delims=" %%P in ('where python 2^>nul') do if not defined STUDIO_PYTHON set "STUDIO_PYTHON=%%P"
if not exist "%STUDIO_PYTHON%" (
  echo Python not found. Activate your environment or set WORM_STUDIO_PYTHON.
  pause
  exit /b 1
)
for %%I in ("%STUDIO_PYTHON%") do set "STUDIO_ENV=%%~dpI"
set "PATH=%STUDIO_ENV%;%STUDIO_ENV%Library\bin;%STUDIO_ENV%Scripts;%PATH%"
if "%~1"=="" (
  "%STUDIO_PYTHON%" "%~dp0studio.py"
) else (
  "%STUDIO_PYTHON%" "%~dp0worm_pipeline.py" %*
)
set "STUDIO_EXIT=%ERRORLEVEL%"
if not "%STUDIO_EXIT%"=="0" pause
exit /b %STUDIO_EXIT%

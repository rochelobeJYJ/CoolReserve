@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 처음 사용하는 PC에서는 INSTALL.cmd를 먼저 실행해 주세요.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1"
set "COOL_START_EXIT=%ERRORLEVEL%"
if not "%COOL_START_EXIT%"=="0" (
  echo 시작하지 못했습니다. 위 오류와 docs\FIRST_RUN.md를 확인해 주세요.
  pause
)
exit /b %COOL_START_EXIT%

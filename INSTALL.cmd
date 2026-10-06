@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install.ps1"
set "COOL_INSTALL_EXIT=%ERRORLEVEL%"
if not "%COOL_INSTALL_EXIT%"=="0" (
  echo 설치하지 못했습니다. 위 안내와 docs\FIRST_RUN.md를 확인해 주세요.
) else (
  echo 설치가 끝났습니다. START.cmd를 실행하세요.
)
pause
exit /b %COOL_INSTALL_EXIT%

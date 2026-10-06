@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 전용 실행 환경을 찾지 못했습니다. README의 설치 방법을 확인해 주세요.
  pause
  exit /b 1
)
echo 쿨메신저와 마우스·키보드를 조작하지 않는 자동 검사를 실행합니다.
".venv\Scripts\python.exe" -X utf8 -m unittest discover -s tests -q
set "TEST_EXIT=%ERRORLEVEL%"
pause
exit /b %TEST_EXIT%

@echo off
rem One-click dorm electric credential updater.
rem Tip: run  setx ASTRBOT_PASS "your-password"  once to skip the password prompt.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [X] python not found. Install Python and enable "Add to PATH".
  pause
  exit /b 1
)
python extract_cookie.py --host pay2.hjnu.edu.cn --auto --push
echo.
pause

@echo off
rem One-click dorm electric credential updater.
rem
rem Required environment variables (set per-user once, NOT stored in this repo):
rem   ASTRBOT_URL  - AstrBot base address, e.g. http://your-server:6185
rem   ASTRBOT_USER - AstrBot username
rem   ASTRBOT_PASS - AstrBot password (optional; script prompts if missing)
rem
rem Set them for the current session like this, then run this bat:
rem   set ASTRBOT_URL=http://your-server:6185
rem   set ASTRBOT_USER=your-account
rem   set ASTRBOT_PASS=your-password
rem
rem Prefer persistent config? Run these ONCE in your own terminal (they write
rem YOUR registry, never commit them anywhere):
rem   setx ASTRBOT_URL "http://your-server:6185"
rem   setx ASTRBOT_USER "your-account"
rem   setx ASTRBOT_PASS "your-password"
rem (setx only affects NEW processes; reopen the terminal afterwards.)
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [X] python not found. Install Python and enable "Add to PATH".
  pause
  exit /b 1
)
if "%ASTRBOT_URL%"=="" (
  echo [X] ASTRBOT_URL is not set. See the comments at the top of this file.
  pause
  exit /b 1
)
if "%ASTRBOT_USER%"=="" (
  echo [X] ASTRBOT_USER is not set. See the comments at the top of this file.
  pause
  exit /b 1
)
python extract_cookie.py --host pay2.hjnu.edu.cn --auto --push
echo.
pause

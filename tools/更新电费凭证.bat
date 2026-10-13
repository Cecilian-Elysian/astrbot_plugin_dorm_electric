@echo off
rem One-click dorm electric credential updater.
rem
rem 必填环境变量（设一次即可，脚本缺什么会在控制台里问你，不再报错退出）：
rem   ASTRBOT_URL  - AstrBot 基地址，如 http://your-server:6185
rem   ASTRBOT_USER - AstrBot 用户名
rem   ASTRBOT_PASS - 省略密码时会安全地提示输入（不显示字符，不落盘）
rem
rem 查看/设置（持久化到你的注册表，不进本仓库）：
rem   setx ASTRBOT_URL  "http://your-server:6185"
rem   setx ASTRBOT_USER "your-account"
rem 密码默认不存，省事可加：
rem   setx ASTRBOT_PASS "your-password"
rem （setx 只影响新进程：新设后请重开终端 / 重新双击本文件。）
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

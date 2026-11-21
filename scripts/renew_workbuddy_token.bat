@echo off
chcp 65001 >nul
REM ============================================================
REM WorkBuddy 服务器签到 token 续期（在本机 Windows 运行）
REM 作用：从本机客户端导出最新 accessToken，推送到腾讯云服务器
REM 建议：每月运行一次，或在服务器签到日志出现“认证失败”时运行
REM ============================================================
setlocal

REM ===== 运行前先设置下面 4 项（或设同名环境变量）=====
if not defined CHECKIN_PEM set "PEM=C:\Users\YOU\Desktop\agent.pem"
if defined CHECKIN_PEM set "PEM=%CHECKIN_PEM%"
if not defined CHECKIN_SERVER set "SERVER=ubuntu@YOUR.SERVER.IP"
if defined CHECKIN_SERVER set "SERVER=%CHECKIN_SERVER%"
set "REMOTE=~/checkin/workbuddy/token.txt"
if not defined EXTRACT_DIR set "EXTRACT_DIR=C:\Users\YOU\path\to\ai-token-checkin-workers\workers\workbuddy\scripts"
set "TMP=%TEMP%\wb_access_token.txt"
if "%PEM%"=="C:\Users\YOU\Desktop\agent.pem" (
    echo 请先设置 CHECKIN_PEM / CHECKIN_SERVER 环境变量，或编辑本脚本顶部配置区。
    exit /b 1
)

echo [1/3] 从本机 WorkBuddy 客户端导出 accessToken ...
cd /d "%EXTRACT_DIR%"
python extract_token.py --field accessToken --raw > "%TMP%" 2>nul
if errorlevel 1 (
    echo 导出失败，请确认本机 WorkBuddy 已登录。
    exit /b 1
)

echo [2/3] 推送到服务器 ...
scp -i "%PEM%" -o StrictHostKeyChecking=no -q "%TMP%" "%SERVER%:%REMOTE%"
if errorlevel 1 (
    echo 推送失败，请检查密钥或网络。
    exit /b 1
)
ssh -i "%PEM%" -o StrictHostKeyChecking=no "%SERVER%" "chmod 600 %REMOTE% && echo 服务器端 token 已更新"

echo [3/3] 在服务器试跑一次签到验证 ...
ssh -i "%PEM%" -o StrictHostKeyChecking=no "%SERVER%" "cd ~/checkin/workbuddy && python3 signin_server.py"

echo.
echo 完成。下次到期约为 30 天后。
endlocal

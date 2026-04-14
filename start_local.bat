@echo off
title Gemini VSCode Copilot - Local Admin Boot
color 0A

:: 1. 检查并申请管理员权限
>nul 2>&1 "%SYSTEMROOT%\system32\cacls.exe" "%SYSTEMROOT%\system32\config\system"
if '%errorlevel%' NEQ '0' (
    echo 🛡️ Requesting Administrative Privileges...
    goto UACPrompt
) else ( goto gotAdmin )

:UACPrompt
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~s0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    "%temp%\getadmin.vbs"
    exit /B

:gotAdmin
    if exist "%temp%\getadmin.vbs" ( del "%temp%\getadmin.vbs" )
    pushd "%CD%"
    CD /D "%~dp0"

:: 2. 启动服务
echo ===================================================
echo 🚀 Gemini Antigravity - Local Server Boot Sequence
echo ===================================================
echo.
echo [INFO] Environment: Windows (Admin)
echo [INFO] Starting FastAPI local server on 127.0.0.1:8000...
echo.

:: 如果你使用了虚拟环境 (如 venv)，请取消下面这行的注释并修改路径
:: call venv\Scripts\activate

python main.py

echo.
echo [❌] Server unexpectedly stopped.
pause
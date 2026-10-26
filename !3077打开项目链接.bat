@echo off
chcp 65001 >nul
setlocal
set "TASK3077_URL=https://www.upwork.com/jobs/span-class-highlight-Python-span-based-tool-automatically-extract-manufacturing-operation-steps-from-existing-span-class-highlight-pdf-span_~022108933001015251006/"
if exist "%ProgramFiles%\Mozilla Firefox\firefox.exe" (
    start "" "%ProgramFiles%\Mozilla Firefox\firefox.exe" "%TASK3077_URL%"
    exit /b 0
)
if exist "%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe" (
    start "" "%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe" "%TASK3077_URL%"
    exit /b 0
)
if exist "%LOCALAPPDATA%\Mozilla Firefox\firefox.exe" (
    start "" "%LOCALAPPDATA%\Mozilla Firefox\firefox.exe" "%TASK3077_URL%"
    exit /b 0
)
where firefox.exe >nul 2>nul
if errorlevel 1 (
    echo 未找到已安装的 Firefox，请安装后重试。本脚本不会自动下载。
    exit /b 1
)
start "" firefox.exe "%TASK3077_URL%"
exit /b 0

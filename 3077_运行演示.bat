@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist "%~dp03077_新演示结果" (
    echo 输出位置3077_新演示结果已存在，拒绝覆盖。请保留原结果并选择新的输出位置。
    exit /b 1
)
set "TASK3077_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%TASK3077_PYTHON%" goto TASK3077_TRY_PY
"%TASK3077_PYTHON%" -X utf8 -B -c "import sys,importlib.metadata as m; assert sys.version_info[:2]==(3,12); assert m.version('pypdf')=='6.10.0'; assert m.version('reportlab')=='4.4.9'" >nul 2>nul
if errorlevel 1 goto TASK3077_TRY_PY
"%TASK3077_PYTHON%" -X utf8 -B "%~dp03077_运行演示.py" --output "%~dp03077_新演示结果"
set "TASK3077_EXIT=%ERRORLEVEL%"
goto TASK3077_DONE

:TASK3077_TRY_PY
where py.exe >nul 2>nul
if errorlevel 1 goto TASK3077_MISSING
py -3.12 -X utf8 -B -c "import sys,importlib.metadata as m; assert sys.version_info[:2]==(3,12); assert m.version('pypdf')=='6.10.0'; assert m.version('reportlab')=='4.4.9'" >nul 2>nul
if errorlevel 1 goto TASK3077_MISSING
py -3.12 -X utf8 -B "%~dp03077_运行演示.py" --output "%~dp03077_新演示结果"
set "TASK3077_EXIT=%ERRORLEVEL%"
goto TASK3077_DONE

:TASK3077_MISSING
echo 未找到具有固定依赖的已安装Python 3.12。请按3077_依赖.txt准备运行环境。
echo 本脚本不会下载、安装软件或依赖。
exit /b 1

:TASK3077_DONE
if not "%TASK3077_EXIT%"=="0" echo 演示未完成，请查看上方错误；本脚本没有连接真实ION或MES。
exit /b %TASK3077_EXIT%

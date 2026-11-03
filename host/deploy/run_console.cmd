@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0..\.."

echo === AI 解码器 · 端侧音频控制台 ===
echo 工作目录: %CD%

rem 1) 先确认 python 里有 numpy —— 这是最常见的失败原因
python -c "import numpy, sys; print('python', sys.version.split()[0], 'numpy', numpy.__version__)" 2>nul
if errorlevel 1 (
    echo.
    echo [错误] 当前 python 里没有 numpy。
    echo        请先执行:  pip install -r host\deploy\requirements.txt
    echo        或换成装有 numpy 的解释器再运行本脚本。
    pause
    exit /b 1
)

rem 2) 起服务并打开窗口（--port 可改端口）
python host\app\run_web.py %*
if errorlevel 1 (
    echo.
    echo [错误] 控制台启动失败，看上面的报错。
    pause
    exit /b 1
)
endlocal

@echo off
chcp 65001 >nul
title 学术文献原版对照翻译流水线
echo =========================================================================
echo               学术文献原版对照翻译流水线 (MinerU + UniMERNet)
echo =========================================================================
echo.

set PYTHON_EXE=%~dp0.venv\Scripts\python.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python
)

set INPUT_PDF=%~1
if "%INPUT_PDF%"=="" (
    echo [提示] 您可以直接把 PDF 文件拖拽到本脚本图标上运行！
    echo.
    set /p INPUT_PDF="请输入待翻译的 PDF 文件路径 (或直接把文件拖入窗口按回车): "
)

if "%INPUT_PDF%"=="" (
    echo [错误] 未提供输入文件，程序退出。
    pause
    exit /b 1
)

echo.
echo [*] 开始处理文献: %INPUT_PDF%
echo.
"%PYTHON_EXE%" "%~dp0main.py" %INPUT_PDF%

echo.
echo =========================================================================
echo [完成] 处理结束，翻译产物已存入 output 文件夹。
echo =========================================================================
pause

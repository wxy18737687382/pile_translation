@echo off
chcp 65001 >nul
echo =========================================================================
echo               UniMERNet 本地公式识别微服务启动器
echo =========================================================================
echo.

set PYTHON_EXE=D:\meachine_learning\minicoda\envs\unimernet_env\python.exe
if exist "%PYTHON_EXE%" (
    echo [*] 找到专属环境 Python: %PYTHON_EXE%
    echo [*] 服务监听地址: http://127.0.0.1:8000
    echo [*] API 预测接口: http://127.0.0.1:8000/predict
    echo.
    "%PYTHON_EXE%" "%~dp0server.py" --port 8000
    pause
    exit /b
)

set CONDA_BAT=D:\meachine_learning\minicoda\condabin\conda.bat
if not exist "%CONDA_BAT%" (
    set CONDA_BAT=conda
)

echo [*] 正在使用 Conda 激活专属环境 [unimernet_env] 并启动 FastAPI 微服务...
echo [*] 服务监听地址: http://127.0.0.1:8000
echo [*] API 预测接口: http://127.0.0.1:8000/predict
echo.
call "%CONDA_BAT%" run -n unimernet_env python "%~dp0server.py" --port 8000
pause

@echo off
chcp 65001 >nul
echo =========================================================================
echo       UniMERNet 本地公式大模型微服务 - 专属 Conda 隔离环境一键配置
echo =========================================================================
echo.

set CONDA_BAT=D:\meachine_learning\minicoda\condabin\conda.bat
if not exist "%CONDA_BAT%" (
    set CONDA_BAT=conda
)

echo [1/4] 正在创建 Python 3.10 隔离环境 [unimernet_env]...
call "%CONDA_BAT%" create -n unimernet_env python=3.10 -y
if %ERRORLEVEL% neq 0 (
    echo [错误] Conda 环境创建失败，请检查网络或权限。
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [2/4] 正在安装 PyTorch (带 CUDA 支持) 与核心依赖...
call "%CONDA_BAT%" run -n unimernet_env pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118
call "%CONDA_BAT%" run -n unimernet_env pip install fastapi uvicorn pillow pydantic python-multipart modelscope

echo.
echo [3/4] 正在安装 UniMERNet 官方包...
call "%CONDA_BAT%" run -n unimernet_env pip install "unimernet[full]"

echo.
echo [4/4] 正在检查/下载 UniMERNet 模型权重 (ModelScope 极速下载)...
set WEIGHTS_FILE=%~dp0weights\unimernet_base\unimernet_base.pth
if not exist "%WEIGHTS_FILE%" (
    echo [*] 未检测到权重文件，正在通过 ModelScope 自动下载 unimernet_base 模型...
    call "%CONDA_BAT%" run -n unimernet_env python -c "from modelscope import snapshot_download; snapshot_download('wanderkid/unimernet_base', local_dir=r'%~dp0weights\unimernet_base')"
) else (
    echo [+] 已就绪: %WEIGHTS_FILE%
)

echo.
echo =========================================================================
echo [成功] UniMERNet 独立环境与模型权重配置完成！
echo 您可以双击运行 start_service.bat 启动本地微服务。
echo =========================================================================
pause

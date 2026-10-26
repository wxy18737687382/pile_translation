@echo off
chcp 65001 >nul
echo =========================================================================
echo           MinerU (magic-pdf) 本地解析引擎环境一键配置
echo =========================================================================
echo.

set CONDA_BAT=D:\meachine_learning\minicoda\condabin\conda.bat
if not exist "%CONDA_BAT%" (
    set CONDA_BAT=conda
)

echo [1/3] 正在创建 Python 3.10 隔离环境 [magic_pdf]...
call "%CONDA_BAT%" create -n magic_pdf python=3.10 -y

echo.
echo [2/3] 正在安装 magic-pdf (含 GPU 加速及依赖)...
call "%CONDA_BAT%" run -n magic_pdf pip install -U "magic-pdf[full]" --extra-index-url https://wheels.myhloli.com
call "%CONDA_BAT%" run -n magic_pdf pip install huggingface_hub modelscope

echo.
echo [3/3] 验证 magic-pdf 安装版本...
call "%CONDA_BAT%" run -n magic_pdf magic-pdf --version

echo.
echo =========================================================================
echo [提示] 安装完成！请确保已在 ~/magic-pdf.json 中正确配置 models-dir 与 device-mode。
echo 随后可运行 test_parse.bat 进行测试验证。
echo =========================================================================
pause

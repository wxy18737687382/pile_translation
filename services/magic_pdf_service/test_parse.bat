@echo off
chcp 65001 >nul
echo =========================================================================
echo              magic-pdf 本地解析引擎环境验证测试
echo =========================================================================
echo.
set MAGIC_PDF_EXE=D:\meachine_learning\minicoda\envs\magic_pdf\Scripts\magic-pdf.exe

if not exist "%MAGIC_PDF_EXE%" (
    echo [错误] 未找到 magic-pdf 可执行程序: %MAGIC_PDF_EXE%
    pause
    exit /b 1
)

echo [1/2] 检查版本与 CUDA 环境...
"%MAGIC_PDF_EXE%" --version
echo.

echo [2/2] 执行快速解析测试 (sample_paper.pdf)...
"%MAGIC_PDF_EXE%" -p "..\..\sample_paper.pdf" -o "..\..\test_out" -m auto

if %ERRORLEVEL% equ 0 (
    echo.
    echo =========================================================================
    echo [成功] magic-pdf 本地 GPU 解析全部测试通过！
    echo 产物已生成至 test_out 目录。
    echo =========================================================================
) else (
    echo.
    echo [错误] 解析测试失败，错误码: %ERRORLEVEL%
)
pause

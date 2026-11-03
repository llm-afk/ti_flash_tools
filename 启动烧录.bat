@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title XDS110 - F28035

set "TI_FLASH_PYTHON=python"
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run

set "TI_FLASH_PYTHON=py -3"
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run

echo [错误] 未找到 Python 3.10 或更新版本，请先安装 Python 并添加到 PATH。
set "TI_FLASH_EXIT_CODE=1"
goto finish

:run
%TI_FLASH_PYTHON% -u -B "%~dp0scripts\auto_flash.py" %*
set "TI_FLASH_EXIT_CODE=%errorlevel%"

:finish
echo(
pause
exit /b %TI_FLASH_EXIT_CODE%

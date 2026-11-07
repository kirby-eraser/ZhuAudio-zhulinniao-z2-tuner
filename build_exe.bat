@echo off
REM 一键打包竹林鸟 Z2 PC 调音台（App 风格版）
REM 默认打包 gui_app.py（App 风格界面，推荐）。如需打包稳定版 gui.py，
REM 把下面的 spec 换成 竹林鸟Z2调音台.spec 即可。
REM 注意：打包用的 Python 必须自带 tkinter（标准 python.org 安装即带；
REM       用精简/沙箱 Python 会导致运行报 "No module named 'tkinter'"）。
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

python -m PyInstaller --noconfirm --clean "竹林鸟Z2调音台_App风格版.spec"
if errorlevel 1 goto fail

echo.
echo 构建完成：dist\竹林鸟Z2调音台_App风格版.exe
goto end

:fail
echo.
echo 构建失败，请查看上面的报错。
exit /b 1

:end
pause

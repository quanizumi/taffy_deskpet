@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title 永雏塔菲 — 桌面宠物

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"
set "APP=%~dp0src\desk_pet.py"
set "PY_EXE="
set "FIRST_SETUP="

echo.
echo  ========================================
echo    永雏塔菲 — 桌面宠物
echo  ========================================
echo.

if not exist "%APP%" (
  echo [错误] 找不到程序: %APP%
  pause
  exit /b 1
)

if exist "%VENV_PY%" goto :ensure_deps

echo [1/3] 检查 Python ...
call :resolve_python
if not defined PY_EXE (
  echo     未检测到 Python 3.9+，尝试自动安装 ...
  call :install_python
  call :refresh_path
  call :resolve_python
)
if not defined PY_EXE (
  echo.
  echo [错误] 未找到 Python 3.9 或更高版本。
  echo 请安装 Python，并勾选 "Add python.exe to PATH"，
  echo 完成后重新双击本脚本。
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
echo     使用: %PY_EXE%
set "FIRST_SETUP=1"

echo [2/3] 创建虚拟环境 ...
"%PY_EXE%" -m venv "%VENV_DIR%"
if errorlevel 1 (
  echo [错误] 创建虚拟环境失败。请确认安装的是完整版 Python（非 Windows 商店占位程序）。
  pause
  exit /b 1
)

:ensure_deps
if not exist "%VENV_PY%" (
  echo [错误] 虚拟环境不完整，请删除 .venv 文件夹后重新运行本脚本。
  pause
  exit /b 1
)

if defined FIRST_SETUP (
  echo [3/3] 检查依赖 ...
) else (
  echo 检查运行环境 ...
)
"%VENV_PY%" -c "from PySide6.QtWidgets import QApplication; from PySide6.QtMultimedia import QMediaPlayer" >nul 2>&1
if errorlevel 1 (
  echo     正在安装运行依赖（首次可能需要几分钟）...
  call :pip_install "PySide6_Essentials>=6.6.0"
  "%VENV_PY%" -c "from PySide6.QtWidgets import QApplication; from PySide6.QtMultimedia import QMediaPlayer" >nul 2>&1
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

if not exist "%VENV_PYW%" set "VENV_PYW=%VENV_PY%"

echo.
echo 启动中 ...
start "" "%VENV_PYW%" "%APP%"
exit /b 0

rem ---------- helpers ----------

:resolve_python
set "PY_EXE="
where py >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY_EXE=%%i"
  if defined PY_EXE (
    call :py_ok
    if not errorlevel 1 exit /b 0
  )
  set "PY_EXE="
)
where python >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY_EXE=%%i"
  if defined PY_EXE (
    call :py_ok
    if not errorlevel 1 exit /b 0
  )
  set "PY_EXE="
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%D\python.exe" (
    set "PY_EXE=%%D\python.exe"
    call :py_ok
    if not errorlevel 1 exit /b 0
  )
)
for /d %%D in ("%ProgramFiles%\Python3*") do (
  if exist "%%D\python.exe" (
    set "PY_EXE=%%D\python.exe"
    call :py_ok
    if not errorlevel 1 exit /b 0
  )
)
set "PY_EXE="
exit /b 1

:py_ok
"%PY_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
exit /b %ERRORLEVEL%

:install_python
where winget >nul 2>&1
if errorlevel 1 (
  echo     未找到 winget，跳过自动安装。
  exit /b 1
)
echo     正在通过 winget 安装 Python 3.12 ...
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
exit /b %ERRORLEVEL%

:refresh_path
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%LOCALAPPDATA%\Programs\Python\Launcher;%PATH%"
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "PATH=%%B;%PATH%"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "PATH=%%B;%PATH%"
exit /b 0

:pip_install
set "PKG=%~1"
"%VENV_PY%" -m pip install "%PKG%" --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 exit /b 0
echo     清华源失败，改用阿里云 ...
"%VENV_PY%" -m pip install "%PKG%" --disable-pip-version-check -i https://mirrors.aliyun.com/pypi/simple
if not errorlevel 1 exit /b 0
echo     镜像失败，改用官方源 ...
"%VENV_PY%" -m pip install "%PKG%" --disable-pip-version-check
exit /b %ERRORLEVEL%

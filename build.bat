@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] Building DeskPet.exe ...
python -m PyInstaller --noconfirm --clean ^
  --onefile ^
  --windowed ^
  --name DeskPet ^
  --add-data "assets\character.png;assets" ^
  --hidden-import PySide6.QtCore ^
  --hidden-import PySide6.QtGui ^
  --hidden-import PySide6.QtWidgets ^
  src\desk_pet.py

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

echo.
echo [2/2] Done.
echo EXE: %~dp0dist\DeskPet.exe
echo Double-click dist\DeskPet.exe to run.
pause

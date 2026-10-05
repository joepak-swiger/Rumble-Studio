@echo off
cd /d "%~dp0"
if "%~1"=="" (
  echo.
  echo RUMBLE STUDIO v0.13 - MUGEN IMPORT
  echo.
  echo Drag a MUGEN .def, .zip, .rar, or .7z file onto IMPORT_MUGEN.bat
  echo or use the easier IMPORT MUGEN CHARACTER button inside Rumble Studio.
  echo.
  pause
  exit /b 1
)
python -m pip install -r requirements.txt
python mugen_import.py "%~1" --fighters "assets\fighters"
pause

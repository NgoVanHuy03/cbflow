@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  MO GIAO DIEN TAO ANH 2K (ChichBong Imagen4)
rem  Double-click la chay. Tu doc license tu may.
rem ============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo [LOI] Chua cai Python.
  echo       Tai tai: https://www.python.org/downloads/
  echo       Nho TICK "Add Python to PATH" khi cai dat.
  pause
  exit /b 1
)

echo Dang chuan bi (cai thu vien lan dau co the hoi lau)...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo [LOI] Cai thu vien that bai. Kiem tra ket noi mang.
  pause
  exit /b 1
)

rem Mo giao dien (pythonw = khong hien cua so den)
start "" pythonw tao_anh_2k_gui.py
exit /b 0

@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  TAO ANH 2K - ChichBong Imagen4 (chay tren Windows)
rem  Double-click de chay. Tu doc license tu Registry cua may.
rem ============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo [LOI] Chua cai Python. Tai tai: https://www.python.org/downloads/
  echo       Nho tick "Add Python to PATH" khi cai.
  pause
  exit /b 1
)

echo [1/2] Cai thu vien can thiet...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo [LOI] Cai thu vien that bai. Kiem tra ket noi mang.
  pause
  exit /b 1
)

echo.
echo [2/2] Nhap mo ta anh muon tao (tieng Anh cho ket qua tot nhat):
set /p PROMPT=^>

if "%PROMPT%"=="" (
  echo Chua nhap mo ta. Thoat.
  pause
  exit /b 1
)

python tao_anh_2k.py -p "%PROMPT%" -o ".\anh_2k"
echo.
echo Anh da luu trong thu muc: anh_2k
pause

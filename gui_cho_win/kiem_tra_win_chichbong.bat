@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  CHAN DOAN ChichBong (Imagen4) tren Windows
rem  - Khong can Python. Double-click de chay.
rem  - Chi DOC, khong sua gi trong Registry.
rem  - Ket qua luu vao: ket_qua_chichbong.txt  (gui lai file nay)
rem ============================================================

set "OUT=%~dp0ket_qua_chichbong.txt"

echo ====== CHAN DOAN CHICHBONG / IMAGEN4 ======> "%OUT%"
echo Thoi gian: %date% %time%>> "%OUT%"
echo.>> "%OUT%"

echo ========== SYSTEM UUID (mainboard_uuid) ==========>> "%OUT%"
wmic csproduct get uuid >> "%OUT%" 2>nul
if errorlevel 1 (
  powershell -NoProfile -Command "(Get-CimInstance Win32_ComputerSystemProduct).UUID" >> "%OUT%" 2>nul
)
echo.>> "%OUT%"

echo ========== REGISTRY: cac nhanh ChichBong/Imagen4/ElevenLabs ==========>> "%OUT%"

for %%K in (
  "HKCU\Software\Imagen4"
  "HKCU\Software\ElevenLabs"
  "HKCU\Software\chichbong"
  "HKLM\Software\Imagen4"
  "HKLM\Software\ElevenLabs"
) do (
  echo.>> "%OUT%"
  echo --- %%~K --->> "%OUT%"
  reg query %%K /s >> "%OUT%" 2>nul
  if errorlevel 1 echo   ^(khong ton tai^)>> "%OUT%"
)

echo.>> "%OUT%"
echo ========== TIM THEM: moi key chua tu "license" trong Imagen4 ==========>> "%OUT%"
reg query "HKCU\Software\Imagen4" /s /f "license" >> "%OUT%" 2>nul

echo.>> "%OUT%"
echo ====== XONG ======>> "%OUT%"

echo.
echo Da xong! Ket qua luu tai:
echo   %OUT%
echo Hay gui file ket_qua_chichbong.txt nay lai.
echo.
start "" notepad "%OUT%"
pause

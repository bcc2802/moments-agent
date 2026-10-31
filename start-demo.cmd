@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 -B scripts\app.py --workspace "%USERPROFILE%\MomentsAgentDemo" --demo
) else (
  python -B scripts\app.py --workspace "%USERPROFILE%\MomentsAgentDemo" --demo
)
if errorlevel 1 (
  echo.
  echo Could not start the demo. See docs\INSTALL.md. No private material is required.
  pause
)
endlocal

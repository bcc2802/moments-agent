@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 -B scripts\app.py
) else (
  python -B scripts\app.py
)
if errorlevel 1 (
  echo.
  echo Could not start Moments Agent. See docs\INSTALL.md for Python and Codex setup.
  pause
)
endlocal

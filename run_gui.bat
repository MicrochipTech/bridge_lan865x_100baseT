@echo off
rem ===========================================================================
rem  run_gui.bat - launches scripts\bridge_gui.py (Bridge Status & Configuration
rem                GUI) against the board's EDBG virtual COM port.
rem
rem  Ported from the sister project t1s_100baset_bridge's run_gui.bat. Uses
rem  this project's own .venv (see setup.bat) - same pattern as cli.bat/
rem  flash.bat - falls back to the bare "python" from PATH if it's missing.
rem ===========================================================================
setlocal

set "SCRIPT_DIR=%~dp0"
set "PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    pause
    exit /b 1
)

echo Starting Bridge GUI...
cd /d "%SCRIPT_DIR%"
"%PY%" scripts\bridge_gui.py

if errorlevel 1 (
    echo.
    echo ERROR: GUI failed to start
    pause
    exit /b 1
)

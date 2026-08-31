@echo off
rem ===========================================================================
rem  run_gui_telnet.bat - launches scripts\bridge_gui_telnet.py (Bridge Status &
rem                Configuration GUI, Telnet variant) against the board's
rem                Telnet server (TCP/23) instead of the EDBG virtual COM port.
rem
rem  Parallel to run_gui.bat/bridge_gui.py - same tool, connection layer swapped.
rem  Uses this project's own .venv (see setup.bat) - same pattern as
rem  run_gui.bat - falls back to the bare "python" from PATH if it's missing.
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

echo Starting Bridge GUI (Telnet)...
cd /d "%SCRIPT_DIR%"
"%PY%" scripts\bridge_gui_telnet.py

if errorlevel 1 (
    echo.
    echo ERROR: GUI failed to start
    pause
    exit /b 1
)

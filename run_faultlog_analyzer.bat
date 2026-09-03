@echo off
rem ===========================================================================
rem  run_faultlog_analyzer.bat - launches scripts\faultlog_analyze.py against
rem                the clipboard content (a copied "faultlog"/boot-time crash
rem                report) and explains it: resolves PC/LR against the current
rem                build's ELF, shows the source lines, decodes every set
rem                CFSR/HFSR bit in plain language.
rem
rem  Same .venv-first pattern as run_gui.bat/cli.bat/flash.bat - falls back to
rem  the bare "python" from PATH if the venv is missing. Meant to be launched
rem  by double-click right after copying a faultlog report to the clipboard,
rem  so the window stays open until a key is pressed either way (success or
rem  error), not just on error like run_gui.bat.
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

cd /d "%SCRIPT_DIR%"
"%PY%" scripts\faultlog_analyze.py %*

echo.
pause

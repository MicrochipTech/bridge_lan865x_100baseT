@echo off
rem ===========================================================================
rem  run_web_telnet.bat - launches scripts\bridge_web_telnet.py, the browser
rem                front end for the same board run_gui_telnet.bat operates.
rem
rem  Starts a local web server and opens a browser tab against it. Python does
rem  NOT disappear here: a browser cannot open a raw TCP socket to the board's
rem  Telnet port, so this process keeps the TLS/Telnet connection and the page
rem  is only the display.
rem
rem  Binds to 127.0.0.1 - this machine only. Whoever can open the page can log
rem  into the board and rewrite its EEPROM, and there is no login in front of
rem  it; see the docstring in scripts\bridge_web_telnet.py before adding --host.
rem
rem  Uses this project's own .venv (see setup.bat) - same pattern as
rem  run_gui_telnet.bat - falls back to the bare "python" from PATH if missing.
rem ===========================================================================
setlocal

set "SCRIPT_DIR=%~dp0"
set "PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo WARNING: no .venv in this checkout - setup.bat has not been run here.
    echo          Falling back to "python" from PATH. nicegui, listed in
    echo          scripts\requirements.txt, fails there as a plain
    echo          ModuleNotFoundError if it is missing.
    set "PY=python"
)

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    pause
    exit /b 1
)

echo Starting Bridge Web UI (Telnet) on http://127.0.0.1:8088 ...
cd /d "%SCRIPT_DIR%"
"%PY%" scripts\bridge_web_telnet.py %*

if errorlevel 1 (
    echo.
    echo ERROR: web UI failed to start
    pause
    exit /b 1
)

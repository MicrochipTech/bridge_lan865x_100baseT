@echo off
rem ===========================================================================
rem  cli.bat - send CLI commands to the board's EDBG virtual COM port.
rem
rem  Usage:   cli.bat "netinfo" "stats"
rem           cli.bat --port COM8 --read 3 "ping 192.168.0.54"
rem           cli.bat --listen 8
rem
rem  Thin wrapper around scripts\cli.py, using this project's own .venv (see
rem  setup.bat) - see flash.bat for the same pattern. All arguments are
rem  passed through as-is.
rem ===========================================================================
setlocal

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo WARNING: no .venv in this checkout - setup.bat has not been run here.
    echo          Falling back to "python" from PATH. Anything listed in
    echo          scripts\requirements.txt that is missing there fails later as a
    echo          plain ModuleNotFoundError, which looks like a broken tool but is not.
    set "PY=python"
)

"%PY%" "%~dp0scripts\cli.py" %*
exit /b %errorlevel%

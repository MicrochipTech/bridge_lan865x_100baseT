@echo off
rem ===========================================================================
rem  probes.bat - lists the debug probes connected to this PC.
rem
rem  Usage:   probes.bat          ... serial, COM port, bench.json selection, board
rem           probes.bat --chip   ... plus device and chip serial behind each
rem           probes.bat -help    ... all options
rem
rem  The plain list needs USB only and does not touch the boards. --chip attaches
rem  to each target without halting it - the firmware keeps running.
rem  All arguments go to scripts\probes.py.
rem ===========================================================================
setlocal

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo WARNING: no .venv in this checkout - setup.bat has not been run here.
    echo          Falling back to "python" from PATH.
    set "PY=python"
)

"%PY%" "%~dp0scripts\probes.py" %*
exit /b %errorlevel%

@echo off
rem ===========================================================================
rem  flash.bat - flashes tcpip_iperf_lan865x onto the SAM E54 board via pyOCD
rem              (EDBG probe, no MPLAB X needed at flash time)
rem
rem  Usage:   flash.bat                  ... flash the default build output
rem           flash.bat --dry-run        ... only show what would run
rem           flash.bat <file.hex> ...   ... flash a different image
rem           flash.bat --list           ... list connected probes
rem           flash.bat --probe <serial> ... pick a probe for a single run
rem
rem  Ported from the sister project t1s_100baset_bridge's flash.bat/
rem  flash_same54.py. Uses this project's own .venv (see setup.bat) -
rem  falls back to the bare "python" from PATH if it's missing.
rem
rem  All further arguments are passed through to scripts\flash_same54.py.
rem ===========================================================================
setlocal

set "PROBE="

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "TOOL=%~dp0scripts\flash_same54.py"
set "HEX=%~dp0firmware\tcpip_iperf_lan865x.X\dist\default\production\tcpip_iperf_lan865x.X.production.hex"

if not exist "%TOOL%" (
    echo ERROR: %TOOL% missing.
    exit /b 1
)

rem --- first argument: image, unless it's an option -------------------
set "ARGS=%*"
set "FIRST=%~1"
if not "%FIRST%"=="" (
    echo %FIRST% | findstr /b /c:"-" >nul
    if errorlevel 1 (
        set "HEX=%~f1"
        shift
        set "ARGS=%1 %2 %3 %4 %5 %6 %7 %8 %9"
    )
)

rem --- special case --list: no image needed -------------------------------
echo %ARGS% | findstr /c:"--list" >nul
if not errorlevel 1 (
    "%PY%" "%TOOL%" --list
    exit /b %errorlevel%
)

if not exist "%HEX%" (
    echo ERROR: %HEX% does not exist.
    echo         Run build.bat first ^(or open+build the project once in MPLAB X^).
    exit /b 1
)

echo [flash] Image : %HEX%
echo.

"%PY%" "%TOOL%" "%HEX%" %ARGS%
if errorlevel 1 (
    echo.
    echo ERROR: flashing failed.
    exit /b 1
)

echo.
echo %ARGS% | findstr /c:"--dry-run" >nul
if not errorlevel 1 (
    echo [ok   ] Dry run - the board was not touched.
    exit /b 0
)
echo [ok   ] flashed and reset.
echo         Console: EDBG COM port, 115200 8N1.
exit /b 0

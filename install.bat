@echo off
rem ===========================================================================
rem  install.bat - check and set up the prerequisites for flash.bat
rem
rem  Usage:   install.bat            ... check prerequisites, then pick the probe
rem           install.bat --install  ... same, plus install missing pyOCD via pip
rem           install.bat --select   ... only pick the probe, skip the checks
rem
rem  Checked: Python >= 3.9, pyOCD (verified with 0.43.0), visible EDBG probes
rem  on USB, and a locally present Microchip.SAME54_DFP pack. The pack itself
rem  is deliberately NOT loaded automatically - there is no official direct
rem  URL for it; it comes from an MPLAB X / MCC installation.
rem
rem  Probe selection: EVERY run lists the connected probes with a number and asks
rem  which one flash.bat should program - on a bench with several boards, which
rem  one is about to be overwritten must not be assumed silently. Enter keeps the
rem  current choice, so re-running costs one keystroke. The answer goes into
rem  bench.json (keyed on the probe's serial), which flash.bat reads. A single
rem  probe takes the same path, so the mechanism is identical no matter how many
rem  boards are connected.
rem
rem  Exit code 0 = everything present, 1 = something is missing (details in
rem  the output).
rem ===========================================================================
setlocal

rem Runs install_prereqs.py through .venv (created by setup.bat) so the pyocd
rem check/install below targets the same interpreter flash.bat itself uses.
rem Falls back to bare "python" if .venv is missing (e.g. this is the very
rem first run, before batch\setup_venv.bat has created it).
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "TOOL=%~dp0scripts\install_prereqs.py"

if not exist "%TOOL%" (
    echo ERROR: %TOOL% missing.
    exit /b 1
)

"%PY%" "%TOOL%" %*
set "RC=%errorlevel%"

rem --select only records the probe choice; the prerequisite summary below would be
rem a lie there (nothing was checked), so return straight away.
echo %* | findstr /c:"--select" >nul
if not errorlevel 1 exit /b %RC%

echo.
if "%RC%"=="0" (
    echo [ok   ] Prerequisites satisfied - build.bat and flash.bat are usable.
) else (
    echo [missing] See the messages above. "install.bat --install" fetches missing pyOCD.
)
exit /b %RC%

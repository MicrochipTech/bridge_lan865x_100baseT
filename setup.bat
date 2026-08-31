@echo off
setlocal
:: ===========================================================================
:: setup.bat - the ONE script to run once, per machine, after cloning.
::
:: Adapts the project to the local machine: Python venv + deps, pyOCD (for
:: flashing), and the SAME54_DFP debug fix. It drives batch\setup_venv.bat /
:: install.bat / batch\genmk.bat for you - those exist as separate files
:: because other scripts also call them, but you should never need to run
:: them directly yourself. After this:
::
::   build.bat
::   flash.bat
::
:: The only other script you run directly, and only when it applies: switching
:: which board flash.bat programs, via "install.bat --select" (see 2/4 below).
::
:: Connect the board via its USB debugger port BEFORE running this so the
:: probe check can detect it. Steps are independent: a failure in one is
:: reported but does not abort the rest.
::
:: Ported from the sister project t1s_100baset_bridge's setup.bat - one step
:: dropped: that project's XC32 compiler selection (setup_compiler.py) feeds
:: build_summary.py, which this project does not have, so there is nothing
:: here for it to configure.
:: ===========================================================================
set "SCRIPT_DIR=%~dp0"
set "RC=0"

echo ============================================================
echo   tcpip_iperf_lan865x (T1S Bridge) - one-time machine setup
echo ============================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python 3.9+ and re-run.
    exit /b 1
)

echo.
echo [1/4] Python virtual environment ^(.venv^) + dependencies ...
call "%SCRIPT_DIR%batch\setup_venv.bat"
if errorlevel 1 ( echo [WARN] .venv setup failed - check your network/pip. & set "RC=1" )

rem Resolved AFTER step 1, which is what creates .venv on a fresh clone - every
rem other .bat in this repo can assume .venv already exists and resolve PY near
rem the top, but this script cannot. Falls back to the bare "python" from PATH
rem if .venv is still missing (step 1 failed), so step 3 still gets attempted.
set "PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo [2/4] Flasher prerequisites (pyOCD, EDBG probe, SAME54_DFP pack) ...
call "%SCRIPT_DIR%install.bat" --install
if errorlevel 1 ( echo [WARN] install.bat reported missing prerequisites - see above. & set "RC=1" )

echo.
echo [3/4] VS Code debug fix (SAME54_DFP tool pack) ...
"%PY%" "%SCRIPT_DIR%scripts\setup_debug.py"
if errorlevel 1 ( echo [WARN] setup_debug.py failed - only needed for VS Code debugging. & set "RC=1" )

rem The nbproject Makefile fragments are gitignored - they carry absolute paths
rem of the machine that generated them, so a fresh clone has none. Generating
rem them here means the first build.bat has nothing left to discover; build.bat
rem does it too, so this step is a convenience, not a prerequisite.
echo.
echo [4/4] MPLAB X project Makefiles (no IDE session needed) ...
call "%SCRIPT_DIR%batch\genmk.bat" "%SCRIPT_DIR%firmware\tcpip_iperf_lan865x.X"
if errorlevel 1 ( echo [WARN] genmk.bat failed - build.bat will try again. & set "RC=1" )

echo.
echo ============================================================
if "%RC%"=="0" (
    echo   Setup complete. Now build and flash:
) else (
    echo   Setup finished with warnings ^(see above^). You can still:
)
echo     build.bat
echo     flash.bat
echo ============================================================
endlocal & exit /b %RC%

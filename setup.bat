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
:: which board flash.bat programs, via "install.bat --select" (see 3/5 below).
::
:: Connect the board via its USB debugger port BEFORE running this so the
:: probe check can detect it. Steps are independent: a failure in one is
:: reported but does not abort the rest.
::
:: Ported from a related in-house project's setup.bat. Includes
:: its XC32 compiler-selection step (scripts\setup_compiler.py) even though,
:: same as there, it's advisory only here - build.bat does
:: not read setup_compiler.config for the actual build (see that script's
:: own docstring for why). Useful anyway when more than one XC32 version is
:: installed, to record and see which one you intend to use.
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
echo [1/5] Python virtual environment ^(.venv^) + dependencies ...
call "%SCRIPT_DIR%batch\setup_venv.bat"
if errorlevel 1 ( echo [WARN] .venv setup failed - check your network/pip. & set "RC=1" )

rem Resolved AFTER step 1, which is what creates .venv on a fresh clone - every
rem other .bat in this repo can assume .venv already exists and resolve PY near
rem the top, but this script cannot. Falls back to the bare "python" from PATH
rem if .venv is still missing (step 1 failed), so later steps still get attempted.
set "PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo [2/5] Compiler selection (XC32, advisory - see scripts\setup_compiler.py) ...
"%PY%" "%SCRIPT_DIR%scripts\setup_compiler.py"
if errorlevel 1 ( echo [WARN] setup_compiler.py failed - run it manually. & set "RC=1" )

echo.
echo [3/5] Flasher prerequisites (pyOCD, EDBG probe, SAME54_DFP pack) ...
call "%SCRIPT_DIR%install.bat" --install
if errorlevel 1 ( echo [WARN] install.bat reported missing prerequisites - see above. & set "RC=1" )

echo.
echo [4/5] VS Code debug fix (SAME54_DFP tool pack) ...
"%PY%" "%SCRIPT_DIR%scripts\setup_debug.py"
if errorlevel 1 ( echo [WARN] setup_debug.py failed - only needed for VS Code debugging. & set "RC=1" )

rem The nbproject Makefile fragments are gitignored - they carry absolute paths
rem of the machine that generated them, so a fresh clone has none. Generating
rem them here means the first build.bat has nothing left to discover; build.bat
rem does it too, so this step is a convenience, not a prerequisite.
echo.
echo [5/5] MPLAB X project Makefiles (no IDE session needed) ...
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

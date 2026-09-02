@echo off
setlocal EnableDelayedExpansion

:: ===========================================================================
:: build.bat - Shell build for tcpip_iperf_lan865x (T1S<->100BASE-T bridge
:: work-in-progress, based on the Microchip net_10base_t1s content repo).
::
:: Ported from a related in-house project's build.bat. Drives
:: MPLAB X's own generated NetBeans-style Makefile (nbproject\Makefile-impl.mk
:: etc.) via MPLAB X's bundled 'make', the same mechanism the IDE itself uses.
::
:: Usage:
::     build.bat [incremental|clean|rebuild|help]
::     flash.bat
:: ===========================================================================

set "SCRIPT_DIR=%~dp0"
rem Runs build_summary.py through this project's own .venv (created by
rem setup.bat / batch\setup_venv.bat), falling back to the bare "python" from
rem PATH if .venv doesn't exist yet - same pattern as flash.bat/cli.bat.
set "PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "MPLAB_DIR=%SCRIPT_DIR%firmware\tcpip_iperf_lan865x.X"
set "PROJ_NAME=tcpip_iperf_lan865x"
set "CONF=default"
set "TYPE_IMAGE=PRODUCTION"
set "DIST_DIR=%MPLAB_DIR%\dist\%CONF%\production"
set "ELF_PATH=%DIST_DIR%\%PROJ_NAME%.X.production.elf"
set "HEX_PATH=%DIST_DIR%\%PROJ_NAME%.X.production.hex"
set "COMPILER_CONFIG=%SCRIPT_DIR%setup_compiler.config"
set "MPLABX_MAKE="
for /f "delims=" %%D in ('dir /b /ad /o-n "C:\Program Files\Microchip\MPLABX\v*" 2^>nul') do (
    if not defined MPLABX_MAKE if exist "C:\Program Files\Microchip\MPLABX\%%D\gnuBins\GnuWin32\bin\make.exe" (
        set "MPLABX_MAKE=C:\Program Files\Microchip\MPLABX\%%D\gnuBins\GnuWin32\bin\make.exe"
    )
)

if not defined MPLABX_MAKE (
    echo ERROR: Could not find MPLAB X's bundled make.exe under
    echo        C:\Program Files\Microchip\MPLABX\v*\gnuBins\GnuWin32\bin\
    echo        Install MPLAB X IDE.
    exit /b 1
)
echo Make      : %MPLABX_MAKE%

rem Parallel compile, same rationale as that project's build.bat: the
rem per-configuration makefile inherits the jobserver via ${MAKE}, so compile
rem rules run concurrently. -Otarget keeps each compiler's output together.
if not defined BUILD_JOBS set "BUILD_JOBS=%NUMBER_OF_PROCESSORS%"
if not defined BUILD_JOBS set "BUILD_JOBS=1"
set "MAKE_PARALLEL=-j%BUILD_JOBS% -Otarget"
echo Jobs      : %BUILD_JOBS% parallel ^(override with BUILD_JOBS=n^)

rem NOTE: deliberately NOT passing MP_CC_DIR/MP_CC_TYPE_IMAGE on the command
rem line - nbproject\Makefile-local-default.mk (written by MPLAB X itself)
rem already has the correct absolute compiler path. A command-line override -
rem even blank - takes precedence and silently breaks xc32-bin2hex (link
rem succeeds, then "file not found").
rem setup_compiler.config is only used here for build_summary.py's xc32-nm.
if exist "%COMPILER_CONFIG%" (
    for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "(Get-Content '%COMPILER_CONFIG%' | ConvertFrom-Json).bin_dir"`) do set "XC32_BIN_DIR=%%D"
    if defined XC32_BIN_DIR echo Compiler  : %XC32_BIN_DIR%
) else (
    echo WARNING: No compiler configured ^(run "python scripts\setup_compiler.py"^).
    echo          build_summary.py's interrupt-handler listing will be empty.
)

set "MODE=incremental"
if not "%~1"=="" set "MODE=%~1"
if /i "%MODE%"=="help"        goto :help
if /i "%MODE%"=="clean"       goto :clean
if /i "%MODE%"=="rebuild"     goto :rebuild
if /i "%MODE%"=="incremental" goto :incremental
echo ERROR: Unknown parameter "%~1"
goto :help

:help
echo Usage: build.bat [incremental^|clean^|rebuild^|help]
echo   (no argument)  Incremental build (default)
echo   clean          Delete all build artifacts for this configuration
echo   rebuild        Clean then perform a full build
echo.
echo Environment:
echo   BUILD_JOBS=n   Parallel compile jobs (default: NUMBER_OF_PROCESSORS = %NUMBER_OF_PROCESSORS%).
echo                  Use BUILD_JOBS=1 to reproduce a build failure serially.
exit /b 0

:clean
echo Cleaning (make clean, CONF=%CONF%)...
pushd "%MPLAB_DIR%"
"%MPLABX_MAKE%" -f Makefile CONF=%CONF% TYPE_IMAGE=%TYPE_IMAGE% clean
popd
exit /b 0

:rebuild
call :clean
goto :build

:incremental
:build
echo [1/1] Building (make, CONF=%CONF%, TYPE_IMAGE=%TYPE_IMAGE%, %BUILD_JOBS% jobs)...
pushd "%MPLAB_DIR%"
"%MPLABX_MAKE%" -f Makefile CONF=%CONF% TYPE_IMAGE=%TYPE_IMAGE% %MAKE_PARALLEL% build
set "BUILD_RC=%errorlevel%"
popd
if not "%BUILD_RC%"=="0" ( echo ERROR: Build failed. & exit /b 1 )

echo.
echo BUILD SUCCESSFUL.
if exist "%HEX_PATH%" (
    echo HEX: %HEX_PATH%
    rem Copy the HEX into release\ so a fresh clone can flash without building.
    rem NOTE: only this script does that copy - a build from inside the MPLAB X
    rem IDE leaves release\ stale. flash.bat programs the dist\ HEX by default,
    rem not this one, so release\ is not a record of what is on the target. The
    rem guard below is "if exist", not a freshness check: a HEX that survived
    rem from an earlier build gets copied as-is.
    if not exist "%SCRIPT_DIR%release" mkdir "%SCRIPT_DIR%release"
    copy /Y "%HEX_PATH%" "%SCRIPT_DIR%release\bridge_lan865x_100baseT.hex" >nul
    echo Released: %SCRIPT_DIR%release\bridge_lan865x_100baseT.hex
) else (
    echo WARNING: expected HEX not found at %HEX_PATH%
)

rem Post-build memory / interrupt summary (flash/RAM, heap, IRQ handlers).
if exist "%ELF_PATH%" "%PY%" "%SCRIPT_DIR%scripts\build_summary.py" "%DIST_DIR%" "%ELF_PATH%" "%XC32_BIN_DIR%"
endlocal

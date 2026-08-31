@echo off
rem ===========================================================================
rem genmk.bat - generate the nbproject Makefiles WITHOUT opening MPLAB X IDE.
rem
rem   genmk.bat <project-folder.X>
rem
rem WHY THIS EXISTS
rem MPLAB X derives `nbproject\Makefile-*.mk` from `configurations.xml`, and those
rem fragments are gitignored - rightly so, because they are full of absolute paths
rem of the machine that generated them:
rem
rem   C:/Users/<user>/.mchp_packs/Microchip/SAME54_DFP/3.8.234
rem   C:\Program Files\Microchip\xc32\v4.60\bin
rem
rem Committed, they would be wrong on every other machine - and wrong in the
rem expensive way: the link succeeds, then `xc32-bin2hex` fails with "file not
rem found". So they do not belong in the repo. They still have to exist, though,
rem or a fresh clone cannot build.
rem
rem `prjMakefilesGenerator.bat` is the supported tool MPLAB X ships for exactly
rem this. With it, a new machine needs the IDE INSTALLED, not opened - which
rem retires the old instruction "open and build the project in the IDE once".
rem
rem MEASURED 2026-08-18 in a fresh clone: fragments deleted, generator run
rem (rc=0, five files), then `build.bat` reported BUILD SUCCESSFUL with a HEX of
rem 580 034 bytes - bit-identical to the one from the set-up folder.
rem ===========================================================================
setlocal

if "%~1"=="" (
    echo usage: genmk.bat ^<project-folder.X^>
    exit /b 2
)
if not exist "%~1\nbproject\configurations.xml" (
    echo ERROR: %~1 does not look like an MPLAB X project
    echo        ^(nbproject\configurations.xml is missing^) - that file is the
    echo        source the Makefiles are generated from.
    exit /b 2
)

rem Newest installed MPLAB X first - scanned dynamically (dir /o-n = newest
rem version-string first), same mechanism build.bat uses to find make.exe.
rem A hardcoded version list (this file's original form, ported from the
rem sister project) silently picks a STALE version once a newer one is
rem installed that isn't in the list - confirmed 2026-08-31 on this machine
rem (v6.25 and v6.35 both installed; a "v6.25 v6.20 ..." list picked v6.25,
rem producing a Makefile-local-default.mk with a different DFP_DIR/Java path
rem than the IDE's own v6.35-driven build - wrong in the same expensive way
rem this file's own header warns about for xc32-bin2hex, just one directory
rem up). Fails loud (below) rather than silently picking an old version if
rem the scan itself turns up nothing.
set "GEN="
for /f "delims=" %%D in ('dir /b /ad /o-n "C:\Program Files\Microchip\MPLABX\v*" 2^>nul') do (
    if not defined GEN if exist "C:\Program Files\Microchip\MPLABX\%%D\mplab_platform\bin\prjMakefilesGenerator.bat" (
        set "GEN=C:\Program Files\Microchip\MPLABX\%%D\mplab_platform\bin\prjMakefilesGenerator.bat"
    )
)
if not defined GEN (
    echo ERROR: prjMakefilesGenerator.bat not found.
    echo        Looked under C:\Program Files\Microchip\MPLABX\v*\mplab_platform\bin\
    echo        Install MPLAB X IDE ^(opening it is not necessary^).
    exit /b 1
)

echo Generator : %GEN%
echo Project   : %~1
call "%GEN%" "%~1"
set "RC=%ERRORLEVEL%"

if not exist "%~1\nbproject\Makefile-impl.mk" (
    echo ERROR: the generator exited with rc=%RC%, but Makefile-impl.mk is still
    echo        missing. Without that file make has nothing to work with - so this
    echo        stops here instead of running into a follow-up error.
    exit /b 1
)
echo OK: nbproject Makefiles generated.
endlocal & exit /b 0

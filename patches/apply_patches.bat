@echo off
rem ===========================================================================
rem  apply_patches.bat - re-applies this project's hand-patches to MCC-generated
rem                       code after a Generate Code / Force Update on All run.
rem
rem  Usage:   apply_patches.bat            ... apply whatever is missing
rem           apply_patches.bat --check    ... dry run, report only
rem
rem  Thin wrapper around apply_patches.py, using this project's own .venv (see
rem  ..\setup.bat) - same pattern as ..\cli.bat/..\flash.bat - falls back to
rem  the bare "python" from PATH if it's missing. All arguments are passed
rem  through as-is. See README.md for what this does and doesn't cover.
rem ===========================================================================
setlocal

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0apply_patches.py" %*
exit /b %errorlevel%

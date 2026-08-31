@echo off
:: ===========================================================================
:: run_term.bat - double-click: three serial consoles in ONE window, ready to go.
::
:: Ported from the sister project t1s_100baset_bridge's run_term.bat. Which
:: board is on which COM port comes from json\term_ports.json (per-machine,
:: gitignored) - use "Setup > Configure Ports" inside the tool to set it up,
:: not by hand. This bench's three boards: COM8 (this bridge), COM10 (A),
:: COM23 (B) - see CLAUDE.md section 2.
::
:: The window comes up DISCONNECTED - a port only opens once you click
:: "Connect All" (or the button in a single pane's header). Force it to
:: connect on startup with:
::     run_term.bat --connect
::
:: Everything else is passed through, e.g.:
::     run_term.bat --columns           ... side by side instead of stacked
::     run_term.bat --font-size 9
::
:: Started with pythonw so no second window (the console) sits behind the
:: GUI. If something goes wrong there is nothing to see there; run it by
:: hand instead:
::     python scripts\gui_term.py --selftest
::     python scripts\gui_term.py
::
:: Addressed via %~dp0 so this also works from Git Bash and from Explorer.
:: ===========================================================================
setlocal

rem Uses this project's own .venv (see setup.bat) - same pattern as cli.bat/
rem flash.bat/run_gui.bat. Falls back to the bare pythonw/python from PATH if
rem that venv (or its pythonw.exe) is missing.
set "VENV_DIR=%~dp0.venv\Scripts"
set "PYW=%VENV_DIR%\pythonw.exe"
set "PY=%VENV_DIR%\python.exe"
if exist "%PYW%" goto :havepyw

set "PYW=pythonw"
where pythonw >nul 2>&1
if errorlevel 1 (
    if not exist "%PY%" set "PY=python"
    "%PY%" "%~dp0scripts\gui_term.py" %*
    set "RC=%ERRORLEVEL%"
    if not "%RC%"=="0" (
        echo.
        pause
    )
    exit /b %RC%
)

:havepyw
start "" "%PYW%" "%~dp0scripts\gui_term.py" %*
exit /b 0

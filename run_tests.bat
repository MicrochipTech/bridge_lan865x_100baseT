@echo off
rem ===========================================================================
rem  run_tests.bat - the host-side tests that need no hardware.
rem
rem  1) scripts\test_bridge_core_sim.py - bridge_core and BOTH front ends against
rem     a simulated board. Stdlib only. This is the one to run after touching
rem     anything in bridge_core: it reproduces the two timing cases that were
rem     measured against the real board and cost a session each to diagnose.
rem
rem  2) scripts\test_fuses_same54.py - the fuse decoding behind fuses.bat, fed
rem     a real board read and this project's own HEX fuses. Stdlib only.
rem
rem  3) scripts\test_fuse_programmer.py - the fuse write sequence against a
rem     simulated NVMCTRL, and the guards around it. Stdlib only.
rem
rem  4) scripts\test_probes.py - what probes.bat makes of given probes,
rem     bench.json and board files; no USB access. Stdlib only.
rem
rem  5) scripts\test_web_ui.py - the web front end with a simulated browser
rem     client (NiceGUI's own User fixture). Covers what only exists once a
rem     client is connected, above all the lazily built register groups.
rem     Needs pytest + pytest-asyncio; skipped with a note if they are missing.
rem
rem  None of them talks to a board, a probe or the network.
rem ===========================================================================
setlocal

set "SCRIPT_DIR=%~dp0"
set "PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%SCRIPT_DIR%"

echo === bridge_core + both front ends, simulated board ===
"%PY%" scripts\test_bridge_core_sim.py
if errorlevel 1 goto :failed

echo.
echo === fuse decoding, no board ===
"%PY%" scripts\test_fuses_same54.py
if errorlevel 1 goto :failed

echo.
echo === fuse programming, simulated NVMCTRL ===
"%PY%" scripts\test_fuse_programmer.py
if errorlevel 1 goto :failed

echo.
echo === probe list, no probe ===
"%PY%" scripts\test_probes.py
if errorlevel 1 goto :failed

echo.
echo === web front end, simulated browser client ===
"%PY%" -c "import pytest, pytest_asyncio" 2>nul
if errorlevel 1 (
    echo SKIPPED - pytest/pytest-asyncio not installed.
    echo           pip install pytest pytest-asyncio
    goto :done
)
rem See the docstring in scripts\test_web_ui.py for why all three -o/-p switches
rem are needed and why they are not in a pytest.ini.
"%PY%" -m pytest scripts\test_web_ui.py -q -p nicegui.testing.user_plugin -o asyncio_mode=auto -o main_file=
if errorlevel 1 goto :failed

:done
echo.
echo All tests passed.
exit /b 0

:failed
echo.
echo TESTS FAILED
exit /b 1

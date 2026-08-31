@echo off
:: ============================================================
::  setup_venv.bat
::  Creates the project's .venv (if missing) and installs every package
::  listed in scripts\requirements.txt into it - never into the machine's
::  global Python. Every other .bat in this repo runs Python tools via
::  .venv\Scripts\python(w).exe, so nothing here needs manual activation:
::  once this script has run, build.bat/flash.bat/run_gui.bat/run_term.bat/
::  setup.bat just work.
::
::  Safe to re-run: an existing .venv is reused, only the pip install
::  step repeats (cheap, and picks up a bumped/pinned version such as the
::  known-good pyocd from requirements.txt).
::
::  Usually called automatically - by setup.bat (once per machine, after
::  cloning) or by bridge_gui.py/gui_term.py's "Install now" dialog if a
::  hard dependency (sv-ttk) is missing. Safe to run directly too, e.g. to
::  pick up a requirements.txt change without redoing the rest of setup.bat.
::
::  Compatible with Windows 10 / Windows 11
:: ============================================================

setlocal enabledelayedexpansion

echo.
echo ============================================================
echo   Python Virtual Environment Setup
echo ============================================================
echo.

:: ------------------------------------------------------------
:: 1. Check if Python is installed
:: ------------------------------------------------------------
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not found in PATH.
    echo.
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    goto :error_exit
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYTHON_VERSION=%%v
echo [OK]    Found: %PYTHON_VERSION%

:: ------------------------------------------------------------
:: 2. Create .venv if it doesn't exist yet
:: ------------------------------------------------------------
rem %~dp0 is this script's OWN directory (batch\), so every repo-root path
rem below goes through REPO_ROOT instead - this script must work no matter
rem where it's called from (setup.bat, dep_check.py's "Install now" dialog).
set "REPO_ROOT=%~dp0.."
set "VENV_DIR=%REPO_ROOT%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if exist "%VENV_PY%" (
    echo [OK]    Found existing virtual environment: %VENV_DIR%
) else (
    echo Creating virtual environment at %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
    if %errorlevel% neq 0 (
        echo [ERROR] Could not create the virtual environment.
        goto :error_exit
    )
    echo [OK]    Created: %VENV_DIR%
)
echo.

:: ------------------------------------------------------------
:: 3. Check if requirements.txt exists
:: ------------------------------------------------------------
set REQUIREMENTS=%REPO_ROOT%\scripts\requirements.txt

if not exist "%REQUIREMENTS%" (
    echo.
    echo [ERROR] requirements.txt not found at:
    echo         %REQUIREMENTS%
    echo.
    goto :error_exit
)

echo [OK]    Found: %REQUIREMENTS%
echo.

:: ------------------------------------------------------------
:: 4. Upgrade pip inside the venv
:: ------------------------------------------------------------
echo Upgrading pip (in .venv) ...
"%VENV_PY%" -m pip install --upgrade pip
if %errorlevel% neq 0 (
    echo [WARN]  Could not upgrade pip. Continuing with current version.
)
echo.

:: ------------------------------------------------------------
:: 5. Install packages from requirements.txt into the venv
:: ------------------------------------------------------------
echo Installing packages from requirements.txt (into .venv) ...
echo.
"%VENV_PY%" -m pip install -r "%REQUIREMENTS%"

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] One or more packages failed to install.
    echo.
    echo Possible causes:
    echo   - No internet connection or network timeout
    echo   - A package name in requirements.txt is incorrect
    echo   - A package requires a compiler that is not installed
    echo.
    echo Tip: Try running the following command manually for more details:
    echo         "%VENV_PY%" -m pip install -r "%REQUIREMENTS%" -v
    echo.
    goto :error_exit
)

:: ------------------------------------------------------------
:: 6. Success
:: ------------------------------------------------------------
echo.
echo ============================================================
echo   Virtual environment ready, all packages installed!
echo ============================================================
echo.
goto :end

:error_exit
echo ============================================================
echo   Installation failed. See error messages above.
echo ============================================================
echo.
exit /b 1

:end
exit /b 0

@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

rem Find a Python interpreter even when PATH is not inherited by double-click.
set "PYTHON="
set "PYTHON_ARGS="
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
if not defined PYTHON (
    where python >nul 2>&1 && set "PYTHON=python"
)
if not defined PYTHON (
    where py >nul 2>&1 && set "PYTHON=py" && set "PYTHON_ARGS=-3"
)
if not defined PYTHON if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PYTHON if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYTHON=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYTHON if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYTHON if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PYTHON=%LocalAppData%\Programs\Python\Python310\python.exe"
if not defined PYTHON if exist "%ProgramFiles%\Python313\python.exe" set "PYTHON=%ProgramFiles%\Python313\python.exe"
if not defined PYTHON if exist "%ProgramFiles%\Python312\python.exe" set "PYTHON=%ProgramFiles%\Python312\python.exe"

if not defined PYTHON (
    echo Python not found.
    echo Install Python 3.10+ or add it to PATH, then run this again.
    pause
    exit /b 1
)

rem Double-click to launch the TUI. Run "start.bat plain" for the plain REPL.
if /i "%~1"=="plain" (
    "%PYTHON%" %PYTHON_ARGS% -m alex.cli
) else (
    "%PYTHON%" %PYTHON_ARGS% -m alex.tui
)

if errorlevel 1 (
    echo.
    echo Launch failed. Check API_KEY in .env and installed dependencies.
    pause
)

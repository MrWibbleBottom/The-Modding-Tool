@echo off
setlocal
cd /d "%~dp0"

echo ==================================================
echo   The Modding Tool - Local Launcher
echo ==================================================

set "NO_BROWSER=0"
if /i "%~1"=="--no-browser" set "NO_BROWSER=1"

set "PY_EXE="
set "PYW_EXE="
set "PY_ARGS="

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
    if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW_EXE=%~dp0.venv\Scripts\pythonw.exe"
) else (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY_EXE set "PY_EXE=%%i"
    )
    if not defined PY_EXE (
        for /f "delims=" %%i in ('where py 2^>nul') do (
            if not defined PY_EXE (
                set "PY_EXE=%%i"
                set "PY_ARGS=-3"
            )
        )
    )
)

if not defined PY_EXE (
    echo [ERROR] Python is not installed or not on your PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo and check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] The Modding Tool is already running on http://127.0.0.1:8765
    if "%NO_BROWSER%"=="0" start "" "http://127.0.0.1:8765"
    exit /b 0
)

if "%NO_BROWSER%"=="1" (
    if defined PYW_EXE (
        start "TheModdingToolServer" "%PYW_EXE%" "%~dp0run_server.py"
    ) else (
        start "TheModdingToolServer" /min "%PY_EXE%" %PY_ARGS% "%~dp0run_server.py"
    )
    exit /b 0
)

"%PY_EXE%" %PY_ARGS% "%~dp0run_server.py"
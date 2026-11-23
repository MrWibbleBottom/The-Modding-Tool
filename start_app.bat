@echo off
setlocal
cd /d "%~dp0"

echo ==================================================
echo   The Modding Tool - Local Launcher
echo ==================================================

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on your PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo and check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating Python virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [INFO] Installing dependencies from poc-ui\requirements.txt ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r "poc-ui\requirements.txt"
    if exist "bepinex-mcp\mcp\requirements.txt" (
        ".venv\Scripts\python.exe" -m pip install -r "bepinex-mcp\mcp\requirements.txt"
    )
)

echo [INFO] Opening http://127.0.0.1:8765 in your default browser...
start "" "http://127.0.0.1:8765"

echo [INFO] Starting supervised server on http://127.0.0.1:8765 ...
".venv\Scripts\python.exe" run_server.py

pause

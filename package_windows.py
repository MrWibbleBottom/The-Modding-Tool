"""
Packages The Modding Tool into a portable Windows distribution:
  dist/TheModdingTool/
  dist/TheModdingTool.zip
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DIST_DIR = PROJECT_ROOT / "dist"
PKG_NAME = "TheModdingTool"
PKG_DIR = DIST_DIR / PKG_NAME
ZIP_PATH = DIST_DIR / f"{PKG_NAME}.zip"


LAUNCH_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch_TheModdingTool.ps1"
if errorlevel 1 (
    echo.
    echo The Modding Tool exited with an error. Press any key to close...
    pause >nul
)
"""


LAUNCH_PS1 = r"""param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  The Modding Tool - Portable Windows Launcher" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Find-SystemPython {
    foreach ($cmd in @("py -3", "python", "python3")) {
        try {
            if ($cmd -eq "py -3") {
                $ver = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); assert sys.version_info >= (3, 10)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
                    return $exe.Trim()
                }
            } else {
                $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); assert sys.version_info >= (3, 10)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $exe = & $cmd -c "import sys; print(sys.executable)" 2>$null
                    return $exe.Trim()
                }
            }
        } catch {}
    }
    return $null
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "[Setup] First-time setup: locating Python 3.10+..." -ForegroundColor Yellow
    $SysPy = Find-SystemPython
    if (-not $SysPy) {
        Write-Host "[Setup] Python 3.10+ not found on this PC." -ForegroundColor Yellow
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            Write-Host "[Setup] Attempting to install Python 3.12 via winget..." -ForegroundColor Yellow
            & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            $SysPy = Find-SystemPython
        }
    }
    if (-not $SysPy) {
        Write-Host ""
        Write-Host "ERROR: Python 3.10 or newer is required to run The Modding Tool." -ForegroundColor Red
        Write-Host "Install Python from https://www.python.org/downloads/ (check 'Add Python to PATH') and re-run Launch_TheModdingTool.bat." -ForegroundColor Red
        exit 1
    }

    Write-Host "[Setup] Creating virtual environment at $VenvDir ..." -ForegroundColor Yellow
    & $SysPy -m venv $VenvDir
    Write-Host "[Setup] Installing Python dependencies..." -ForegroundColor Yellow
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")
    Write-Host "[Setup] Environment ready." -ForegroundColor Green
}

$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) {
    Write-Host "[Note] .NET SDK ('dotnet') not detected. The UI and live modding work normally, but compiling standalone Unity IL2CPP C# mods in 'Build a Mod' requires .NET 8 SDK (https://dotnet.microsoft.com/download)." -ForegroundColor DarkYellow
}

$Url = "http://127.0.0.1:$Port"
Write-Host "[Start] Launching The Modding Tool server at $Url ..." -ForegroundColor Green
Write-Host "[Start] Keep this window open while using the app. Press Ctrl+C to stop." -ForegroundColor Gray

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($TargetUrl)
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            try {
                $resp = Invoke-WebRequest -Uri $TargetUrl -UseBasicParsing -TimeoutSec 2
                if ($resp.StatusCode -eq 200) {
                    Start-Process $TargetUrl
                    break
                }
            } catch {}
        }
    } -ArgumentList $Url | Out-Null
}

$env:MODTOOL_SUPERVISED = "1"
while ($true) {
    & $VenvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -eq 42) {
        Write-Host "[Restart] Server requested restart (exit 42). Relaunching..." -ForegroundColor Yellow
        Start-Sleep -Milliseconds 400
        continue
    }
    break
}
"""


README_WINDOWS = """The Modding Tool - Portable Windows Package
===========================================

How to Run on Another Windows 10 / 11 PC
----------------------------------------
1. Extract this entire folder (for example, to C:\\TheModdingTool).
2. Double-click `Launch_TheModdingTool.bat`.
   - On first launch, it automatically creates a local `.venv` and installs
     the Python packages from `requirements.txt`.
   - If Python 3.10+ is not installed, it attempts to install Python 3.12
     via `winget` (or prompts you to install Python from python.org).
3. Your default web browser will open automatically to:
   http://127.0.0.1:8765

Optional Requirements
---------------------
- .NET 8 SDK (`dotnet`): Only required if you use the "Build a Mod" tab to
  compile standalone C# BepInEx 6 IL2CPP `.dll` plugins.
  Download: https://dotnet.microsoft.com/download

Included Contents
-----------------
- app\\              : FastAPI backend, single-page web UI, Steam reference DB
- bridges\\bepinex-mcp\\mcp\\  : BepInEx MCP Python bridge server
- bridges\\bepinex-mcp\\plugins\\BepInExMCP.IL2CPP\\ : Pre-built IL2CPP bridge plugin + source
- assets\\           : Bundled BepInEx 6 IL2CPP archive (if present at build time)
- Launch_TheModdingTool.bat / .ps1 : Self-bootstrapping Windows launcher
"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree_clean(src: Path, dst: Path, ignore_names: set[str] | None = None) -> int:
    ignore = ignore_names or set()
    copied = 0
    for item in src.rglob("*"):
        if any(part in ignore for part in item.parts):
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
    return copied


def build_package() -> dict[str, object]:
    if PKG_DIR.exists():
        shutil.rmtree(PKG_DIR)
    PKG_DIR.mkdir(parents=True, exist_ok=True)

    file_count = 0
    # 1. app/ package (including static, templates, & data), root requirements.txt, & branding
    file_count += copy_tree_clean(
        PROJECT_ROOT / "app",
        PKG_DIR / "app",
        ignore_names={"__pycache__", ".pytest_cache"},
    )
    if (PROJECT_ROOT / "requirements.txt").is_file():
        shutil.copy2(PROJECT_ROOT / "requirements.txt", PKG_DIR / "requirements.txt")
        file_count += 1
    if (PROJECT_ROOT / "branding").is_dir():
        file_count += copy_tree_clean(
            PROJECT_ROOT / "branding",
            PKG_DIR / "branding",
        )

    # 2. bridges/bepinex-mcp/mcp and skills
    bridge_root = PROJECT_ROOT / "bridges" / "bepinex-mcp"
    file_count += copy_tree_clean(
        bridge_root / "mcp",
        PKG_DIR / "bridges" / "bepinex-mcp" / "mcp",
        ignore_names={"__pycache__", ".pytest_cache"},
    )
    if (bridge_root / "skills").is_dir():
        file_count += copy_tree_clean(
            bridge_root / "skills",
            PKG_DIR / "bridges" / "bepinex-mcp" / "skills",
        )

    # 3. Pre-built BepInExMCP.IL2CPP Release binaries + source
    plugin_src = bridge_root / "plugins" / "BepInExMCP.IL2CPP"
    if plugin_src.is_dir():
        file_count += copy_tree_clean(
            plugin_src,
            PKG_DIR / "bridges" / "bepinex-mcp" / "plugins" / "BepInExMCP.IL2CPP",
            ignore_names={"obj", "Debug"},
        )

    # 4. Bundled BepInEx zip if available
    bep_zip_name = "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.793+dbc82f0.zip"
    for candidate in [
        PROJECT_ROOT / "assets" / bep_zip_name,
        Path(r"D:\Downloads") / bep_zip_name,
        Path.home() / "Downloads" / bep_zip_name,
    ]:
        if candidate.is_file():
            assets_dir = PKG_DIR / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, assets_dir / bep_zip_name)
            file_count += 1
            break

    # 5. Launchers, version files & README
    (PKG_DIR / "Launch_TheModdingTool.bat").write_text(LAUNCH_BAT, encoding="utf-8")
    (PKG_DIR / "Launch_TheModdingTool.ps1").write_text(LAUNCH_PS1, encoding="utf-8")
    (PKG_DIR / "README_WINDOWS.txt").write_text(README_WINDOWS, encoding="utf-8")
    file_count += 3
    for vf in ("version.json", "version_manager.py"):
        src_vf = PROJECT_ROOT / vf
        if src_vf.is_file():
            shutil.copy2(src_vf, PKG_DIR / vf)
            file_count += 1

    app_ver = "0.1.0"
    app_channel = "pre-launch"
    ver_file = PROJECT_ROOT / "version.json"
    if ver_file.is_file():
        try:
            vdata = json.loads(ver_file.read_text(encoding="utf-8"))
            app_ver = vdata.get("version", app_ver)
            app_channel = vdata.get("channel", app_channel)
        except Exception:
            pass

    manifest = {
        "package_name": PKG_NAME,
        "app_version": app_ver,
        "channel": app_channel,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "file_count": file_count,
        "entrypoint": "Launch_TheModdingTool.bat",
    }
    (PKG_DIR / "package_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # 6. Create distributable .zip
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(PKG_DIR.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(DIST_DIR))

    zip_sha = sha256_file(ZIP_PATH)
    result = {
        "package_dir": str(PKG_DIR),
        "zip_path": str(ZIP_PATH),
        "zip_size_mb": round(ZIP_PATH.stat().st_size / (1024 * 1024), 2),
        "zip_sha256": zip_sha,
        "file_count": file_count,
    }
    print(json.dumps(result, indent=2))
    return result


def build_pyinstaller_command(onefile: bool = False) -> list[str]:
    """Return a PyInstaller command bundling `app/static`, `app/templates`, `app/data`, `app/engines`, and `app/services`."""
    cmd = [
        "pyinstaller",
        "--noconfirm",
        "--clean",
        "--name",
        PKG_NAME,
        "--onefile" if onefile else "--onedir",
    ]
    ico_path = PROJECT_ROOT / "branding" / "favicons" / "app.ico"
    if ico_path.is_file():
        cmd.extend(["--icon", str(ico_path)])
    cmd.extend([
        "--add-data",
        f"{PROJECT_ROOT / 'app' / 'static'};app/static",
        "--add-data",
        f"{PROJECT_ROOT / 'app' / 'templates'};app/templates",
        "--add-data",
        f"{PROJECT_ROOT / 'app' / 'data' / 'steam_games_reference.csv'};app/data",
        "--add-data",
        f"{PROJECT_ROOT / 'app' / 'engines'};app/engines",
        "--add-data",
        f"{PROJECT_ROOT / 'app' / 'services'};app/services",
        "--collect-submodules",
        "app.engines",
        "--collect-submodules",
        "app.services",
        str(PROJECT_ROOT / "app" / "main.py"),
    ])
    return cmd


if __name__ == "__main__":
    build_package()

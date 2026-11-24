"""System health, defaults, audit logs, PC specs, tool installers, version, maintenance, watcher, and dev mode."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import zipfile
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.core.audit import append_audit_log, clear_audit_log_cache, log_action_error, read_audit_logs
import app.core.audit as _audit
from app.core.config import (
    ANTI_CHEAT_BINARIES,
    APP_DATA_ROOT,
    APP_SNAPSHOTS_DIR,
    APP_UI_CONFIG_PATH,
    ASSET_AUDITS_DIR,
    AUDIT_APP_LOG_PATH,
    AUDIT_GAME_LOG_PATH,
    AUDIT_LOG_PATH,
    BACKUP_ROOT,
    BRANDING_BANNERS_DIR,
    BRANDING_DIR,
    BRANDING_FAVICONS_DIR,
    BUILT_MODS_DIR,
    DEFAULT_BEPINEX_SHA256,
    DEFAULT_BEPINEX_ZIP,
    DEFAULT_GAME_DIR,
    DEFAULT_MCP_APP,
    DEFAULT_PLUGIN_BUILD_DIR,
    DEFAULT_VENV_PYTHON,
    DEV_MODE_PATH,
    GAME_ICONS_DIR,
    GAMES_RESPONSE_CACHE,
    GAMES_STORE_PATH,
    ICON_EXTRACT_SEMAPHORE,
    LIBRARY_CACHE_PATH,
    MANIFEST_DIR,
    PC_SPECS_CACHE_PATH,
    PLUGIN_DEPLOY_FILES,
    REPO_CONFIG_PATH,
    RESOURCE_ROOT,
    SCAN_EXCLUSIONS_PATH,
    SCAN_PROGRESS,
    STEAM_ASSETS_CACHE,
    STEAM_ASSETS_CACHE_PATH,
    TOOL_VERSIONS_PATH,
    TOOLS_ROOT,
    UI_SETTINGS_PATH,
    UNREAL_INI_BACKUP_DIR,
    WORKSPACE_DIR,
    _load_workspace_module,
    backup_dir_for_game,
    clear_pc_specs_cache,
    default_managed_games,
    get_app_data_root,
    get_bepinex_bridge_roots,
    invalidate_games_cache,
    load_managed_games,
    manifest_path_for_game,
    save_managed_games,
    sha256_file,
    sha256_text,
)


import app.core.config as _cfg

router = APIRouter()

class InstallToolRequest(BaseModel):
    tool_id: str
    game_id: str = Field(default="battleground")
    game_dir: str = Field(default=DEFAULT_GAME_DIR)


class OpenToolFolderRequest(BaseModel):
    path: str


@router.get("/api/defaults")
async def get_defaults() -> dict[str, Any]:
    return {
        "game_dir": DEFAULT_GAME_DIR,
        "mcp_app_path": DEFAULT_MCP_APP,
        "venv_python_path": DEFAULT_VENV_PYTHON,
        "plugin_build_dir": DEFAULT_PLUGIN_BUILD_DIR,
        "bepinex_zip_path": DEFAULT_BEPINEX_ZIP,
        "expected_sha256": DEFAULT_BEPINEX_SHA256,
        "audit_log_path": str(AUDIT_LOG_PATH),
        "stage4": {
            "game_object_name": "Piece_DragonbornPaladin(Clone)",
            "game_object_id": "-269924",
            "scene": "27653627778046a48b230901ca6f384c",
            "path": "/LevelSceneRoot[0]/Piece_DragonbornPaladin%28Clone%29[10]",
            "component_name": "Jade.GameView.CameraTargetCharacter",
            "alt_component_name": "Jade.GameView.PieceView",
            "member_name": "PreferredDragLayerMask",
            "member_value": "1007361",
            "ui_health_object_id": "-64174",
            "ui_health_path": "/ScreenspaceHudViewNonVR%28Clone%29[0]/UICanvas[0]/LeftStretchAnchor[0]/LeftStretchOffset[0]/TopLeftAnchor[0]/PartyViewNonVR[0]/RootStack[0]/TurnViews[0]/PartyPieceView1[1]/Offset[0]/Portrait[1]/PieceHealthBar2D[0]/HealthText[3]",
            "ui_health_component": "TMPro.TextMeshProUGUI",
            "ui_health_member": "text",
            "ui_health_value": "23 / 23 ",
            "selector": {
                "scene": "DontDestroyOnLoad",
                "path": "/ScreenspaceHudViewNonVR%28Clone%29[0]/UICanvas[0]/LeftStretchAnchor[0]/LeftStretchOffset[0]/TopLeftAnchor[0]/PartyViewNonVR[0]/RootStack[0]/TurnViews[0]/PartyPieceView1[1]/Offset[0]/Portrait[1]/PieceHealthBar2D[0]/HealthText[3]",
                "name": "HealthText",
                "component": "TMPro.TextMeshProUGUI",
            },
            "hero_selector": {
                "scene": "27653627778046a48b230901ca6f384c",
                "path": "/LevelSceneRoot[0]/Piece_DragonbornPaladin%28Clone%29[10]",
                "name": "Piece_DragonbornPaladin(Clone)",
                "component": "Jade.GameView.PieceView",
            },
            "profile_name": "paladin_hp_23",
        },
    }


@router.get("/api/logs")
async def get_logs(request: Request, limit: int = 50, category: str = "all"):
    limit = max(1, min(limit, 200))
    cat = (category or "all").strip().lower()
    entries = read_audit_logs(limit=limit, category=cat)
    all_entries = _audit.AUDIT_LOG_CACHE or []
    game_count = sum(1 for e in all_entries if e.get("category") == "game")
    app_count = sum(1 for e in all_entries if e.get("category") == "app")
    active_path = (
        AUDIT_GAME_LOG_PATH if cat == "game" else (AUDIT_APP_LOG_PATH if cat == "app" else AUDIT_LOG_PATH)
    )
    payload = {
        "audit_log_path": str(active_path),
        "category": cat,
        "game_count": game_count,
        "app_count": app_count,
        "count": len(entries),
        "entries": entries,
    }
    etag = f'W/"{sha256_text(json.dumps(payload, sort_keys=True, default=str))[:16]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=payload, headers={"ETag": etag})


class ClientErrorRequest(BaseModel):
    source: str = Field(default="client")
    action: str = Field(default="client.error")
    endpoint: str = Field(default="")
    status_code: int | None = None
    message: str = Field(default="")
    error: str = Field(default="")
    detail: Any = None
    stack: str = Field(default="")
    url: str = Field(default="")
    line: int | None = None
    column: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/audit/client-error")
async def log_client_error(req: ClientErrorRequest) -> dict[str, Any]:
    """Record a sanitized browser/client error as an ACTION_FAILED audit entry (session-token protected)."""
    err_msg = (req.message or req.error or "Unknown client error").strip()
    extra_details: dict[str, Any] = dict(req.details or {})
    if req.detail is not None:
        if isinstance(req.detail, dict):
            extra_details.update(req.detail)
        else:
            extra_details["detail"] = req.detail
    if req.stack:
        extra_details["stack"] = req.stack
    if req.url:
        extra_details["url"] = req.url
    if req.line is not None:
        extra_details["line"] = req.line
    if req.column is not None:
        extra_details["column"] = req.column
    entry = log_action_error(
        source=req.source or "client",
        action=req.action or "client.error",
        endpoint=req.endpoint or req.url or "",
        status_code=req.status_code,
        message=err_msg,
        detail=extra_details or None,
    )
    return {"ok": True, "status": "ok", "entry": entry}


async def _download_and_extract_zip(url: str, target_dir: Path, archive_name: str) -> tuple[Path, str, int]:
    """Downloads a zip archive over HTTPS, computes its SHA-256, and extracts it into target_dir."""
    import httpx

    target_dir.mkdir(parents=True, exist_ok=True)
    archive_path = target_dir / archive_name
    async with httpx.AsyncClient(follow_redirects=True, timeout=90.0) as client:
        resp = await client.get(url, headers={"User-Agent": "TheModdingTool/1.0"})
        resp.raise_for_status()
        archive_path.write_bytes(resp.content)

    file_hash = sha256_file(archive_path)
    extracted_count = 0
    if archive_name.lower().endswith(".zip") and zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(target_dir)
            extracted_count = len(zf.namelist())
    return (archive_path, file_hash, extracted_count)


async def _install_github_release_tool(
    repo: str,
    target_dir: Path,
    asset_keyword: str = ".zip",
) -> tuple[str, str, int]:
    """Queries GitHub latest release API for repo, downloads matching asset (or saves release bundle), and extracts if zip."""
    import httpx

    target_dir.mkdir(parents=True, exist_ok=True)
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        resp = await client.get(api_url, headers={"User-Agent": "TheModdingTool/1.0"})
        resp.raise_for_status()
        rel = resp.json()

    tag_name = rel.get("tag_name", "latest")
    assets = rel.get("assets", []) or []
    chosen_asset = None
    for a in assets:
        aname = str(a.get("name", "")).lower()
        if asset_keyword.lower() in aname and aname.endswith(".zip"):
            chosen_asset = a
            break
    if chosen_asset is None:
        for a in assets:
            aname = str(a.get("name", "")).lower()
            if aname.endswith(".zip"):
                chosen_asset = a
                break
    if chosen_asset is None and assets:
        chosen_asset = assets[0]

    if chosen_asset and chosen_asset.get("browser_download_url"):
        dl_url = chosen_asset["browser_download_url"]
        aname = chosen_asset.get("name", f"{repo.replace('/', '_')}_{tag_name}.zip")
        _, file_hash, count = await _download_and_extract_zip(dl_url, target_dir, aname)
        (target_dir / "release_info.json").write_text(
            json.dumps({"repo": repo, "tag": tag_name, "asset": aname, "download_url": dl_url}, indent=2),
            encoding="utf-8",
        )
        return (f"{aname} ({tag_name})", file_hash, count)

    info_file = target_dir / "release_info.json"
    info_text = json.dumps(
        {"repo": repo, "tag": tag_name, "html_url": rel.get("html_url", f"https://github.com/{repo}/releases")},
        indent=2,
    )
    info_file.write_text(info_text, encoding="utf-8")
    return (f"{repo} ({tag_name})", sha256_text(info_text), 1)


@router.post("/api/tools/install")
async def install_modding_tool(req: InstallToolRequest) -> dict[str, Any]:
    """Installs or deploys a modding tool/app for the active game or workstation tools directory."""
    tool_id = req.tool_id.strip().lower()
    game_dir = Path(req.game_dir.strip()) if req.game_dir.strip() else Path(DEFAULT_GAME_DIR)
    dir_exists = game_dir.is_dir()

    try:
        if tool_id == "bepinex_il2cpp":
            zip_path = Path(DEFAULT_BEPINEX_ZIP)
            if not zip_path.is_file():
                raise HTTPException(status_code=400, detail=f"Local BepInEx 6 IL2CPP zip not found at {zip_path}")
            dest_dir = game_dir if dir_exists else (TOOLS_ROOT / "BepInEx6_IL2CPP")
            dest_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(dest_dir)
                count = len(zf.namelist())
            h = sha256_file(zip_path)
            msg = f"Extracted BepInEx 6 IL2CPP ({count} files) into {dest_dir}"

        elif tool_id == "bepinex_mcp_il2cpp":
            build_dir = Path(DEFAULT_PLUGIN_BUILD_DIR)
            if not build_dir.is_dir():
                raise HTTPException(status_code=400, detail=f"Plugin build directory not found: {build_dir}")
            dest_dir = (
                (game_dir / "BepInEx" / "plugins" / "BepInExMCP.IL2CPP")
                if dir_exists
                else (TOOLS_ROOT / "BepInExMCP.IL2CPP")
            )
            dest_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            for fname in PLUGIN_DEPLOY_FILES:
                src = build_dir / fname
                if src.is_file():
                    shutil.copy2(src, dest_dir / fname)
                    copied += 1
            main_dll = dest_dir / "BepInExMCP.IL2CPP.dll"
            h = sha256_file(main_dll) if main_dll.is_file() else sha256_text(str(dest_dir))
            msg = f"Deployed BepInExMCP.IL2CPP + Roslyn ({copied} files) to {dest_dir}"

        elif tool_id == "il2cpp_dumper":
            dest_dir = TOOLS_ROOT / "Il2CppDumper"
            url = "https://github.com/Perfare/Il2CppDumper/releases/download/v6.7.46/Il2CppDumper-net6-v6.7.46.zip"
            _, h, count = await _download_and_extract_zip(url, dest_dir, "Il2CppDumper-net6-v6.7.46.zip")
            msg = f"Downloaded & extracted Il2CppDumper v6.7.46 ({count} files) to {dest_dir}"

        elif tool_id == "unity_explorer_il2cpp":
            dest_dir = (
                (game_dir / "BepInEx")
                if (dir_exists and (game_dir / "GameAssembly.dll").is_file())
                else (TOOLS_ROOT / "UnityExplorer_IL2CPP")
            )
            url = "https://github.com/sinai-dev/UnityExplorer/releases/download/4.9.0/UnityExplorer.BepInEx6.IL2CPP.zip"
            _, h, count = await _download_and_extract_zip(url, dest_dir, "UnityExplorer.BepInEx6.IL2CPP.zip")
            msg = f"Installed UnityExplorer (BepInEx 6 IL2CPP, {count} files) to {dest_dir}"

        elif tool_id == "bepinex_mono":
            is_mono_game = dir_exists and not (game_dir / "GameAssembly.dll").is_file() and bool(list(game_dir.glob("*_Data")))
            dest_dir = game_dir if is_mono_game else (TOOLS_ROOT / "BepInEx5_Mono")
            url = "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.2/BepInEx_win_x64_5.4.23.2.zip"
            _, h, count = await _download_and_extract_zip(url, dest_dir, "BepInEx_win_x64_5.4.23.2.zip")
            msg = f"Installed BepInEx 5.4.23.2 (Unity Mono x64, {count} files) to {dest_dir}"

        elif tool_id == "dnspy_ex":
            dest_dir = TOOLS_ROOT / "dnSpyEx"
            url = "https://github.com/dnSpyEx/dnSpy/releases/download/v6.5.1/dnSpy-net-win64.zip"
            _, h, count = await _download_and_extract_zip(url, dest_dir, "dnSpy-net-win64.zip")
            msg = f"Installed dnSpyEx v6.5.1 Win64 ({count} files) to {dest_dir}"

        elif tool_id == "ue4ss":
            dest_dir = TOOLS_ROOT / "UE4SS"
            label, h, count = await _install_github_release_tool("UE4SS-RE/RE-UE4SS", dest_dir, "UE4SS")
            msg = f"Installed RE-UE4SS [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "fmodel":
            dest_dir = TOOLS_ROOT / "FModel"
            label, h, count = await _install_github_release_tool("4sval/FModel", dest_dir, "FModel")
            msg = f"Installed FModel [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "ghidra_mcp":
            dest_dir = TOOLS_ROOT / "GhidraMCP"
            label, h, count = await _install_github_release_tool("LaurieWired/GhidraMCP", dest_dir, "GhidraMCP")
            msg = f"Installed GhidraMCP [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "x64dbg":
            dest_dir = TOOLS_ROOT / "x64dbg"
            label, h, count = await _install_github_release_tool("x64dbg/x64dbg", dest_dir, "snapshot")
            msg = f"Installed x64dbg [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "gdre_tools":
            dest_dir = TOOLS_ROOT / "GDRETools"
            label, h, count = await _install_github_release_tool("GDRETools/gdsdecomp", dest_dir, "windows")
            msg = f"Installed GDRE Tools [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "mod_engine_2":
            dest_dir = TOOLS_ROOT / "ModEngine2"
            label, h, count = await _install_github_release_tool("soulsmods/ModEngine2", dest_dir, "ModEngine")
            msg = f"Installed Mod Engine 2 [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "reframework":
            dest_dir = TOOLS_ROOT / "REFramework"
            label, h, count = await _install_github_release_tool("praydog/REFramework", dest_dir, ".zip")
            msg = f"Installed REFramework [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "xedit_creation":
            dest_dir = TOOLS_ROOT / "xEdit"
            label, h, count = await _install_github_release_tool("TES5Edit/TES5Edit", dest_dir, "Edit")
            msg = f"Installed xEdit [{label}] ({count} files) to {dest_dir}"

        else:
            raise HTTPException(status_code=400, detail=f"Unknown modding tool_id: '{tool_id}'")

    except HTTPException:
        raise
    except Exception as exc:
        append_audit_log(
            action=f"tools.install.{tool_id}",
            status="error",
            summary=f"Failed to install modding tool '{tool_id}': {exc}",
            details={"tool_id": tool_id, "game_id": req.game_id, "game_dir": str(game_dir)},
        )
        raise HTTPException(status_code=500, detail=f"Failed to install '{tool_id}': {exc}") from exc

    append_audit_log(
        action=f"tools.install.{tool_id}",
        status="ok",
        summary=msg,
        sha256=h,
        details={"tool_id": tool_id, "install_dir": str(dest_dir), "game_id": req.game_id},
    )

    from app.routers.library import enrich_game_status, inspect_game_modding_info

    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == req.game_id), None)
    if target and dir_exists:
        scraped_info = inspect_game_modding_info(game_dir, str(target.get("name", "")))
        scraped_info["metadata_scraped"] = True
        target["cached_modding_info"] = scraped_info
        target["metadata_scraped"] = True
        save_managed_games(store)

    return {
        "status": "ok",
        "tool_id": tool_id,
        "install_dir": str(dest_dir),
        "sha256": h,
        "message": msg,
        "game": enrich_game_status(target) if target else None,
    }


@router.post("/api/tools/open-folder")
async def open_tool_folder(req: OpenToolFolderRequest) -> dict[str, Any]:
    """Opens the specified tool or game directory in Windows Explorer."""
    import subprocess

    target = Path(req.path.strip())
    if not target.exists():
        if target.parent.is_dir():
            target = target.parent
        else:
            raise HTTPException(status_code=404, detail=f"Directory does not exist yet: {target}")
    subprocess.Popen(["explorer.exe", str(target.resolve())])
    append_audit_log(
        action="tools.open_folder",
        status="ok",
        summary=f"Opened directory in Explorer: {target.resolve()}",
        sha256=sha256_text(str(target.resolve()).lower()),
        details={"path": str(target.resolve())},
    )
    return {"status": "ok", "path": str(target.resolve())}


_PC_SPECS_MEM_CACHE: dict[str, Any] | None = None


def scrape_pc_specs(force_refresh: bool = False) -> dict[str, Any]:
    """Scrapes comprehensive PC hardware & OS specifications (CPU, GPU, VRAM, RAM, Display, Storage, Tier)."""
    global _PC_SPECS_MEM_CACHE
    if not force_refresh and _PC_SPECS_MEM_CACHE is not None:
        return _PC_SPECS_MEM_CACHE
    if not force_refresh and PC_SPECS_CACHE_PATH.is_file():
        try:
            cached = json.loads(PC_SPECS_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("cpu") and cached.get("gpu"):
                _PC_SPECS_MEM_CACHE = cached
                return cached
        except Exception:
            pass

    import ctypes
    import platform
    import subprocess
    import winreg

    # 1. System Memory (RAM) via GlobalMemoryStatusEx
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    mem_stat = MEMORYSTATUSEX()
    mem_stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_stat))
    total_ram_gb = round(mem_stat.ullTotalPhys / (1024**3), 1)
    avail_ram_gb = round(mem_stat.ullAvailPhys / (1024**3), 1)
    used_ram_gb = round(total_ram_gb - avail_ram_gb, 1)
    ram_load_pct = int(mem_stat.dwMemoryLoad)

    # 2. CPU via Registry + os.cpu_count
    cpu_name = platform.processor() or "Unknown CPU"
    cpu_mhz = 0
    cpu_vendor = "Unknown"
    logical_threads = os.cpu_count() or 1
    physical_cores = max(1, logical_threads // 2)
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            cpu_name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
            cpu_mhz = int(winreg.QueryValueEx(key, "~MHz")[0])
            cpu_vendor = str(winreg.QueryValueEx(key, "VendorIdentifier")[0]).strip()
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor",
        ) as root_cpu:
            logical_threads = winreg.QueryInfoKey(root_cpu)[0] or logical_threads
            physical_cores = max(1, logical_threads // 2)
    except Exception:
        pass

    # 3. Motherboard & BIOS via Registry
    board_vendor = "Unknown"
    board_product = "Unknown"
    bios_version = "Unknown"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\BIOS",
        ) as bkey:
            for reg_val, target_name in [
                ("BaseBoardManufacturer", "vendor"),
                ("BaseBoardProduct", "product"),
                ("BIOSVersion", "bios"),
            ]:
                try:
                    v, _ = winreg.QueryValueEx(bkey, reg_val)
                    if isinstance(v, list):
                        v = " ".join(str(x) for x in v)
                    if target_name == "vendor" and v:
                        board_vendor = str(v).strip()
                    elif target_name == "product" and v:
                        board_product = str(v).strip()
                    elif target_name == "bios" and v:
                        bios_version = str(v).strip()
                except OSError:
                    pass
    except Exception:
        pass

    # 4. Windows OS Details via Registry
    os_product = f"{platform.system()} {platform.release()}"
    os_build = platform.version()
    os_display_ver = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as okey:
            prod, _ = winreg.QueryValueEx(okey, "ProductName")
            build, _ = winreg.QueryValueEx(okey, "CurrentBuildNumber")
            try:
                disp, _ = winreg.QueryValueEx(okey, "DisplayVersion")
                os_display_ver = str(disp)
            except OSError:
                pass
            if int(str(build)) >= 22000 and "Windows 10" in str(prod):
                prod = str(prod).replace("Windows 10", "Windows 11")
            os_product = str(prod)
            os_build = str(build)
    except Exception:
        pass

    # 5. Primary Display Resolution & Refresh Rate via Win32 user32
    screen_w = 1920
    screen_h = 1080
    refresh_hz = 60
    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        screen_w = int(user32.GetSystemMetrics(0)) or 1920
        screen_h = int(user32.GetSystemMetrics(1)) or 1080

        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", ctypes.c_wchar * 32),
                ("dmSpecVersion", ctypes.c_ushort),
                ("dmDriverVersion", ctypes.c_ushort),
                ("dmSize", ctypes.c_ushort),
                ("dmDriverExtra", ctypes.c_ushort),
                ("dmFields", ctypes.c_ulong),
                ("dmPositionX", ctypes.c_long),
                ("dmPositionY", ctypes.c_long),
                ("dmDisplayOrientation", ctypes.c_ulong),
                ("dmDisplayFixedOutput", ctypes.c_ulong),
                ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short),
                ("dmFormName", ctypes.c_wchar * 32),
                ("dmLogPixels", ctypes.c_ushort),
                ("dmBitsPerPel", ctypes.c_ulong),
                ("dmPelsWidth", ctypes.c_ulong),
                ("dmPelsHeight", ctypes.c_ulong),
                ("dmDisplayFlags", ctypes.c_ulong),
                ("dmDisplayFrequency", ctypes.c_ulong),
            ]

        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
            if dm.dmPelsWidth > 0 and dm.dmPelsHeight > 0:
                screen_w = int(dm.dmPelsWidth)
                screen_h = int(dm.dmPelsHeight)
            if dm.dmDisplayFrequency > 0:
                refresh_hz = int(dm.dmDisplayFrequency)
    except Exception:
        pass

    # 6. GPU & VRAM via nvidia-smi (with Registry fallback)
    gpu_name = "Unknown GPU"
    vram_total_mb = 0
    vram_free_mb = 0
    gpu_driver = "Unknown"
    gpu_temp_c: float | None = None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            first_gpu = proc.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_gpu.split(",")]
            if len(parts) >= 4:
                gpu_name = parts[0]
                vram_total_mb = int(float(parts[1]))
                vram_free_mb = int(float(parts[2]))
                gpu_driver = parts[3]
            if len(parts) >= 5:
                try:
                    gpu_temp_c = round(float(parts[4]), 1)
                except ValueError:
                    pass
    except Exception:
        pass

    if gpu_name == "Unknown GPU":
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000",
            ) as gkey:
                gpu_name = str(winreg.QueryValueEx(gkey, "DriverDesc")[0])
                gpu_driver = str(winreg.QueryValueEx(gkey, "DriverVersion")[0])
                try:
                    qw_mem, _ = winreg.QueryValueEx(gkey, "HardwareInformation.qwMemorySize")
                    if isinstance(qw_mem, int):
                        vram_total_mb = int(qw_mem // (1024 * 1024))
                except OSError:
                    pass
        except Exception:
            pass

    vram_total_gb = round(vram_total_mb / 1024.0, 1) if vram_total_mb else 0.0
    vram_free_gb = round(vram_free_mb / 1024.0, 1) if vram_free_mb else 0.0

    # 7. Storage Drives
    drives: list[dict[str, Any]] = []
    for drv_letter in ("C:", "D:", "E:", "F:", "G:"):
        drv_path = Path(drv_letter + "\\")
        try:
            if drv_path.is_dir():
                usage = shutil.disk_usage(drv_path)
                total_g = round(usage.total / (1024**3), 1)
                free_g = round(usage.free / (1024**3), 1)
                used_g = round(usage.used / (1024**3), 1)
                pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
                drives.append(
                    {
                        "drive": drv_letter,
                        "total_gb": total_g,
                        "free_gb": free_g,
                        "used_gb": used_g,
                        "used_percent": pct,
                    }
                )
        except Exception:
            pass

    # 8. Performance Tier Classification for Unreal Engine & Modding
    if vram_total_gb >= 16.0 and physical_cores >= 8 and total_ram_gb >= 24.0:
        tier_name = "Enthusiast / Flagship 4K Ultra Tier"
        tier_badge = "Ultra / Epic (4K 60-120+ FPS)"
        rec_ue_preset = 3  # Epic (3) / Cinematic (4)
        rec_ue_preset_label = "Epic (3) / Cinematic (4) with Hardware Lumen & Virtual Shadow Maps"
        rec_resolution = f"{screen_w}x{screen_h} (Native 4K / High-Refresh)"
    elif vram_total_gb >= 10.0 and physical_cores >= 6 and total_ram_gb >= 16.0:
        tier_name = "High-End 1440p / 4K Upscaled Tier"
        tier_badge = "High / Epic (1440p / DLSS 4K)"
        rec_ue_preset = 3
        rec_ue_preset_label = "High (2) to Epic (3) with TSR / DLSS Quality"
        rec_resolution = "2560x1440 (QHD) or 3840x2160 with DLSS/TSR"
    elif vram_total_gb >= 6.0 and total_ram_gb >= 12.0:
        tier_name = "Mainstream 1080p Gaming Tier"
        tier_badge = "Medium / High (1080p)"
        rec_ue_preset = 2
        rec_ue_preset_label = "Medium (1) to High (2)"
        rec_resolution = "1920x1080 (FHD)"
    else:
        tier_name = "Entry / Integrated Graphics Tier"
        tier_badge = "Low / Medium (720p-1080p)"
        rec_ue_preset = 1
        rec_ue_preset_label = "Low (0) to Medium (1) with Dynamic Resolution"
        rec_resolution = "1920x1080 (66% Resolution Scale)"

    specs = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "hostname": platform.node(),
        "tier": {
            "name": tier_name,
            "badge": tier_badge,
            "recommended_ue_scalability": rec_ue_preset,
            "recommended_ue_preset_label": rec_ue_preset_label,
            "recommended_resolution": rec_resolution,
        },
        "cpu": {
            "name": cpu_name,
            "vendor": cpu_vendor,
            "physical_cores": physical_cores,
            "logical_threads": logical_threads,
            "base_clock_mhz": cpu_mhz,
            "base_clock_ghz": round(cpu_mhz / 1000.0, 2) if cpu_mhz else None,
            "architecture": platform.machine() or "AMD64",
        },
        "gpu": {
            "name": gpu_name,
            "vram_total_mb": vram_total_mb,
            "vram_total_gb": vram_total_gb,
            "vram_free_mb": vram_free_mb,
            "vram_free_gb": vram_free_gb,
            "driver_version": gpu_driver,
            "temperature_c": gpu_temp_c,
            "directx_feature_level": "DirectX 12 Ultimate (SM 6.6 / Hardware Ray Tracing & DLSS)"
            if ("RTX" in gpu_name or "RX 7" in gpu_name or "RX 6" in gpu_name)
            else "DirectX 12 (SM 6.0+)",
        },
        "ram": {
            "total_gb": total_ram_gb,
            "available_gb": avail_ram_gb,
            "used_gb": used_ram_gb,
            "load_percent": ram_load_pct,
        },
        "display": {
            "width": screen_w,
            "height": screen_h,
            "resolution_label": f"{screen_w}x{screen_h}",
            "refresh_rate_hz": refresh_hz,
            "aspect_ratio": "16:9"
            if round(screen_w / max(1, screen_h), 2) == 1.78
            else f"{screen_w}:{screen_h}",
        },
        "os": {
            "product_name": os_product,
            "display_version": os_display_ver,
            "build_number": os_build,
            "motherboard": f"{board_vendor} {board_product}".strip(),
            "bios_version": bios_version,
        },
        "drives": drives,
    }
    _PC_SPECS_MEM_CACHE = specs
    try:
        PC_SPECS_CACHE_PATH.write_text(json.dumps(specs, indent=2), encoding="utf-8")
    except Exception:
        pass
    return specs


_UE_QUALITY_LABELS = {
    0: "Low (0)",
    1: "Medium (1)",
    2: "High (2)",
    3: "Epic (3)",
    4: "Cinematic (4)",
}

# Reference Array mapping PC Hardware Tiers (GPU Generation + VRAM + CPU + RAM) to Recommended Unreal Engine 4/5 Configuration Options
UNREAL_HARDWARE_REFERENCE_ARRAY: list[dict[str, Any]] = [
    {
        "tier_id": "tier_flagship_24gb_4090",
        "tier_name": "Flagship 24GB+ (RTX 4090 / 5090 / 3090 Ti / RX 7900 XTX)",
        "example_hardware": "NVIDIA RTX 4090 24GB | 8-16+ Core CPU (i9 / Ryzen 9) | 32-64GB RAM",
        "min_vram_gb": 20.0,
        "max_vram_gb": 48.0,
        "min_cpu_cores": 8,
        "min_ram_gb": 24.0,
        "target_resolution": "3840x2160",
        "target_res_x": 3840,
        "target_res_y": 2160,
        "max_safe_res_y": 2160,
        "recommended_res_scale": 100,
        "min_safe_res_scale": 75,
        "vram_budget_ceiling_gb": 22.5,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — TSR High / DLAA"},
            "sg.ShadowQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full VSM"},
            "sg.GlobalIlluminationQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Hardware Lumen GI"},
            "sg.ReflectionQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Hardware Lumen Reflections"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full Uncompressed Pool"},
            "sg.EffectsQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full Niagara GPU Particles"},
            "sg.FoliageQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Max Nanite Foliage"},
            "sg.ShadingQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
        },
        "notes": "24GB VRAM + Ada Lovelace SER easily sustains native 4K Epic (3) / Cinematic (4) with Hardware Lumen GI, Virtual Shadow Maps, and 12GB+ texture pools.",
    },
    {
        "tier_id": "tier_high_16gb_4080",
        "tier_name": "High-End 16GB (RTX 4080 / 4070 Ti Super / RX 7900 XT / 6950 XT)",
        "example_hardware": "NVIDIA RTX 4080 16GB | 8+ Core CPU (i7 / Ryzen 7) | 32GB RAM",
        "min_vram_gb": 15.0,
        "max_vram_gb": 19.9,
        "min_cpu_cores": 8,
        "min_ram_gb": 16.0,
        "target_resolution": "3840x2160 (TSR/DLSS 85%) or 2560x1440 Native",
        "target_res_x": 3840,
        "target_res_y": 2160,
        "max_safe_res_y": 2160,
        "recommended_res_scale": 85,
        "min_safe_res_scale": 67,
        "vram_budget_ceiling_gb": 15.0,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadowQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.GlobalIlluminationQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) — Lumen GI"},
            "sg.ReflectionQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) — 8-10GB Pool"},
            "sg.EffectsQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.FoliageQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
        },
        "notes": "16GB VRAM sustains Epic (3) across all scalability groups at 1440p Native or 4K with DLSS/TSR Quality (75-85%).",
    },
    {
        "tier_id": "tier_upper_mid_12gb_4070_3080",
        "tier_name": "Upper-Mid 12GB (RTX 4070 / RTX 3080 12GB / 3080 Ti / RX 7800 XT)",
        "example_hardware": "NVIDIA RTX 4070 / 3080 12GB | 6-8 Core CPU | 16-32GB RAM",
        "min_vram_gb": 11.0,
        "max_vram_gb": 14.9,
        "min_cpu_cores": 6,
        "min_ram_gb": 16.0,
        "target_resolution": "2560x1440",
        "target_res_x": 2560,
        "target_res_y": 1440,
        "max_safe_res_y": 1440,
        "recommended_res_scale": 85,
        "min_safe_res_scale": 67,
        "vram_budget_ceiling_gb": 11.2,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadowQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — Balanced VSM"},
            "sg.GlobalIlluminationQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — Software Lumen"},
            "sg.ReflectionQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) at 1440p / High (2) at 4K"},
            "sg.EffectsQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.FoliageQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.ShadingQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
        },
        "notes": "Optimal for 1440p High (2) / Epic Textures (3). Running native 4K Epic with Lumen + VSM will exceed the 12GB VRAM pool.",
    },
    {
        "tier_id": "tier_mid_8gb_3000_4060",
        "tier_name": "Mainstream 8GB RTX 3000 / 4060 Gen (RTX 3070 / 3060 Ti / 3060 8GB / 4060 / RX 6600 XT)",
        "example_hardware": "NVIDIA 8GB RTX 3000 Series (RTX 3070 / 3060 Ti) | 6+ Core CPU | 16GB RAM",
        "min_vram_gb": 7.5,
        "max_vram_gb": 10.9,
        "min_cpu_cores": 6,
        "min_ram_gb": 16.0,
        "target_resolution": "1920x1080 (or 2560x1440 with DLSS/TSR 67%)",
        "target_res_x": 1920,
        "target_res_y": 1080,
        "max_safe_res_y": 1440,
        "recommended_res_scale": 75,
        "min_safe_res_scale": 60,
        "vram_budget_ceiling_gb": 7.6,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.AntiAliasingQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — TSR / DLSS Quality"},
            "sg.ShadowQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — VSM Pool Saver"},
            "sg.GlobalIlluminationQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — Software Lumen"},
            "sg.ReflectionQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — SSR / Hybrid"},
            "sg.PostProcessQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.TextureQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — 8GB Safe Pool (Epic overflows)"},
            "sg.EffectsQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.FoliageQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.ShadingQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.LandscapeQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
        },
        "notes": "8GB VRAM ceiling requires High (2) Textures and Medium (1) / High (2) Shadows & Reflections; 4K or Epic (3) Textures + VSM causes severe 8GB VRAM exhaustion.",
    },
    {
        "tier_id": "tier_entry_6gb_2060_1660",
        "tier_name": "Entry 6GB GPU (RTX 2060 6GB / GTX 1660 Super / RTX 3050 6GB / RX 5600 XT)",
        "example_hardware": "NVIDIA RTX 2060 / GTX 1660S 6GB | 4-6 Core CPU | 16GB RAM",
        "min_vram_gb": 5.5,
        "max_vram_gb": 7.4,
        "min_cpu_cores": 4,
        "min_ram_gb": 12.0,
        "target_resolution": "1920x1080 (TSR/DLSS 67% Scale)",
        "target_res_x": 1920,
        "target_res_y": 1080,
        "max_safe_res_y": 1080,
        "recommended_res_scale": 67,
        "min_safe_res_scale": 50,
        "vram_budget_ceiling_gb": 5.6,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.AntiAliasingQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — TSR Upscaling"},
            "sg.ShadowQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — CSM Shadows"},
            "sg.GlobalIlluminationQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — Screen-Space GI"},
            "sg.ReflectionQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — SSR"},
            "sg.PostProcessQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.TextureQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — 6GB Pool Cap"},
            "sg.EffectsQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1)"},
            "sg.FoliageQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1)"},
            "sg.ShadingQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.LandscapeQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
        },
        "notes": "6GB VRAM requires 1080p with Medium (1) Scalability and TSR/DLSS 67% render scale to avoid PCIe VRAM spilling.",
    },
    {
        "tier_id": "tier_budget_4gb_1650_igpu",
        "tier_name": "Budget 4GB / Handheld / iGPU (GTX 1650 / RX 6400 / Steam Deck / ROG Ally)",
        "example_hardware": "4GB VRAM or Shared APU Memory | 4-8 Core CPU | 8-16GB RAM",
        "min_vram_gb": 0.0,
        "max_vram_gb": 5.4,
        "min_cpu_cores": 4,
        "min_ram_gb": 8.0,
        "target_resolution": "1280x720 or 1920x1080 (50% Scale)",
        "target_res_x": 1280,
        "target_res_y": 720,
        "max_safe_res_y": 1080,
        "recommended_res_scale": 50,
        "min_safe_res_scale": 50,
        "vram_budget_ceiling_gb": 3.8,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.AntiAliasingQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — TSR Performance"},
            "sg.ShadowQuality": {"rec": 0, "max_safe": 0, "label": "Low (0)"},
            "sg.GlobalIlluminationQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.ReflectionQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.PostProcessQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.TextureQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.EffectsQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.FoliageQuality": {"rec": 0, "max_safe": 0, "label": "Low (0)"},
            "sg.ShadingQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.LandscapeQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
        },
        "notes": "4GB VRAM / APU requires Low (0) Scalability, 720p-1080p at 50% 3D Resolution Scale, and Dynamic Resolution Enabled.",
    },
]


def match_pc_to_unreal_reference_tier(
    pc_specs: dict[str, Any],
    override_tier_id: str = "auto",
) -> dict[str, Any]:
    """Selects the matching hardware profile from UNREAL_HARDWARE_REFERENCE_ARRAY based on scraped PC specs or an explicit override_tier_id."""
    if override_tier_id and override_tier_id.strip().lower() != "auto":
        req_id = override_tier_id.strip().lower()
        for tier in UNREAL_HARDWARE_REFERENCE_ARRAY:
            if tier["tier_id"].lower() == req_id:
                return dict(tier, matched_mode="Simulated Reference Tier Override")

    vram_gb = float(pc_specs.get("gpu", {}).get("vram_total_gb", 8.0))
    for tier in UNREAL_HARDWARE_REFERENCE_ARRAY:
        if vram_gb >= float(tier["min_vram_gb"]):
            return dict(tier, matched_mode="Auto-Matched from Scraped PC Specs")
    return dict(UNREAL_HARDWARE_REFERENCE_ARRAY[-1], matched_mode="Auto-Matched from Scraped PC Specs")


def evaluate_scalability_traffic_light(
    setting_key: str,
    current_lvl: int | None,
    ref_tier: dict[str, Any],
) -> dict[str, Any]:
    """Evaluates a single Unreal scalability setting (0..4) against the reference tier using a Traffic-Light (Green / Amber / Red) system."""
    target_info = ref_tier.get("scalability_targets", {}).get(
        setting_key,
        {"rec": 2, "max_safe": 3, "label": "High (2)"},
    )
    rec_lvl = int(target_info["rec"])
    max_safe = int(target_info["max_safe"])
    rec_label = str(target_info["label"])
    tier_name = str(ref_tier.get("tier_name", "Reference Tier"))

    if current_lvl is None:
        return {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "UNSET / DEFAULT",
            "recommended_value": rec_label,
            "advice": f"Setting not saved in INI yet. Recommended for {tier_name}: {rec_label}.",
        }

    if current_lvl > max_safe:
        return {
            "traffic_light": "red",
            "light_icon": "",
            "light_badge": "OVER-BUDGET",
            "recommended_value": rec_label,
            "advice": (
                f"Current {_UE_QUALITY_LABELS.get(current_lvl, str(current_lvl))} exceeds safe ceiling "
                f"({_UE_QUALITY_LABELS.get(max_safe, str(max_safe))}) for {tier_name} — causes VRAM/frame-time bottleneck. Lower to {rec_label}."
            ),
        }

    if current_lvl == rec_lvl or (current_lvl > rec_lvl and current_lvl <= max_safe):
        return {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": rec_label,
            "advice": f"Matches optimal reference target ({rec_label}) for {tier_name}.",
        }

    diff_below = rec_lvl - current_lvl
    if diff_below == 1:
        return {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "SUB-OPTIMAL",
            "recommended_value": rec_label,
            "advice": f"Set 1 tier below hardware capability ({_UE_QUALITY_LABELS.get(current_lvl)} vs recommended {rec_label} for {tier_name}). Safe to raise.",
        }

    return {
        "traffic_light": "red" if rec_lvl >= 3 and current_lvl == 0 else "amber",
        "light_icon": "",
        "light_badge": "UNDER-UTILIZED",
        "recommended_value": rec_label,
        "advice": f"Set {diff_below} tiers below hardware capability ({_UE_QUALITY_LABELS.get(current_lvl)} vs recommended {rec_label} for {tier_name}).",
    }


def _format_ue_quality(val_str: str | None) -> tuple[str, int | None]:
    if val_str is None or str(val_str).strip() == "":
        return ("Not Set (Engine Default)", None)
    try:
        iv = int(float(str(val_str).strip()))
        return (_UE_QUALITY_LABELS.get(iv, f"Level {iv}"), iv)
    except ValueError:
        return (str(val_str).strip(), None)


@router.get("/api/pc-specs")
async def get_pc_specs() -> dict[str, Any]:
    """Returns scraped PC hardware specifications (from cache or fresh scrape) plus the Unreal Hardware Reference Array."""
    
    if _cfg.PC_SPECS_RESPONSE_CACHE is not None:
        return _cfg.PC_SPECS_RESPONSE_CACHE
    specs = await asyncio.to_thread(scrape_pc_specs, False)
    _cfg.PC_SPECS_RESPONSE_CACHE = {
        "status": "ok",
        "specs": specs,
        "matched_reference_tier": match_pc_to_unreal_reference_tier(specs, "auto"),
        "unreal_reference_array": UNREAL_HARDWARE_REFERENCE_ARRAY,
    }
    return _cfg.PC_SPECS_RESPONSE_CACHE


@router.post("/api/pc-specs/scan")
async def rescan_pc_specs() -> dict[str, Any]:
    """Forces a fresh scrape of PC hardware & OS specifications and logs the scan in the Audit Log."""
    
    _cfg.PC_SPECS_RESPONSE_CACHE = None
    specs = await asyncio.to_thread(scrape_pc_specs, True)
    spec_hash = sha256_text(json.dumps(specs, sort_keys=True, default=str))
    summary = (
        f"Scraped PC Specifications: {specs['cpu']['name']} ({specs['cpu']['physical_cores']}C/{specs['cpu']['logical_threads']}T) | "
        f"{specs['gpu']['name']} ({specs['gpu']['vram_total_gb']} GB VRAM) | "
        f"{specs['ram']['total_gb']} GB RAM | {specs['display']['resolution_label']} @ {specs['display']['refresh_rate_hz']}Hz"
    )
    append_audit_log(
        action="pc_specs.scan",
        status="ok",
        summary=summary,
        sha256=spec_hash,
        details={
            "cpu": specs["cpu"]["name"],
            "gpu": specs["gpu"]["name"],
            "vram_gb": specs["gpu"]["vram_total_gb"],
            "ram_gb": specs["ram"]["total_gb"],
            "display": f"{specs['display']['resolution_label']}@{specs['display']['refresh_rate_hz']}Hz",
            "tier": specs["tier"]["name"],
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "sha256": spec_hash,
        "specs": specs,
        "matched_reference_tier": match_pc_to_unreal_reference_tier(specs, "auto"),
        "unreal_reference_array": UNREAL_HARDWARE_REFERENCE_ARRAY,
    }


def _load_tool_versions() -> dict[str, Any]:
    if TOOL_VERSIONS_PATH.is_file():
        try:
            return json.loads(TOOL_VERSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_tool_versions(data: dict[str, Any]) -> None:
    TOOL_VERSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOOL_VERSIONS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


@router.get("/api/app/tools/status")
async def get_modding_tools_status() -> dict[str, Any]:
    """Returns local versions and SHA-256 checksums for deployed modding tools."""
    saved = _load_tool_versions()
    bep_zip = Path(DEFAULT_BEPINEX_ZIP)
    bep_sha = sha256_file(bep_zip) if bep_zip.is_file() else None

    plugin_dll = Path(DEFAULT_PLUGIN_BUILD_DIR) / "BepInExMCP.IL2CPP.dll"
    plugin_sha = sha256_file(plugin_dll) if plugin_dll.is_file() else None

    ue4ss_dir = get_app_data_root() / "tools" / "ue4ss"
    melon_dir = get_app_data_root() / "tools" / "melonloader"

    return {
        "status": "ok",
        "tools": [
            {
                "id": "bepinex6_il2cpp",
                "name": "BepInEx 6 Unity.IL2CPP (x64)",
                "present": bep_zip.is_file(),
                "version": saved.get("bepinex6_il2cpp", {}).get("version", "6.0.0-be.793" if bep_zip.is_file() else "Not Cached"),
                "path": str(bep_zip),
                "sha256": bep_sha,
                "expected_sha256": saved.get("bepinex6_il2cpp", {}).get("expected_sha256", DEFAULT_BEPINEX_SHA256),
                "checksum_match": (bep_sha == saved.get("bepinex6_il2cpp", {}).get("expected_sha256", DEFAULT_BEPINEX_SHA256)) if bep_sha else False,
            },
            {
                "id": "bepinex_mcp_plugin",
                "name": "BepInExMCP.IL2CPP Bridge Plugin",
                "present": plugin_dll.is_file(),
                "version": saved.get("bepinex_mcp_plugin", {}).get("version", "Local Release Build" if plugin_dll.is_file() else "Not Built"),
                "path": str(plugin_dll),
                "sha256": plugin_sha,
                "checksum_match": plugin_dll.is_file(),
            },
            {
                "id": "ue4ss",
                "name": "RE-UE4SS (Unreal Engine 4/5)",
                "present": ue4ss_dir.is_dir() and any(ue4ss_dir.iterdir()),
                "version": saved.get("ue4ss", {}).get("version", "Not Downloaded"),
                "path": str(ue4ss_dir),
                "sha256": saved.get("ue4ss", {}).get("sha256"),
                "checksum_match": bool(saved.get("ue4ss", {}).get("sha256")),
            },
            {
                "id": "melonloader",
                "name": "MelonLoader (Unity Mono/IL2CPP)",
                "present": melon_dir.is_dir() and any(melon_dir.iterdir()),
                "version": saved.get("melonloader", {}).get("version", "Not Downloaded"),
                "path": str(melon_dir),
                "sha256": saved.get("melonloader", {}).get("sha256"),
                "checksum_match": bool(saved.get("melonloader", {}).get("sha256")),
            },
        ],
    }


class UpgradeModdingToolRequest(BaseModel):
    tool_id: str
    require_https: bool = Field(default=True)
    require_checksum: bool = Field(default=True)
    expected_sha256: str = Field(default="")
    custom_url: str = Field(default="")
    keep_backup: bool = Field(default=True)


@router.post("/api/app/tools/upgrade")
async def upgrade_modding_tool(req: UpgradeModdingToolRequest) -> dict[str, Any]:
    """Upgrades a modding deployment tool with HTTPS, SHA-256 checksum, and backup security options."""
    import subprocess
    import urllib.request

    tool_id = req.tool_id.strip()
    saved = _load_tool_versions()
    expected = req.expected_sha256.strip().lower()

    if tool_id == "bepinex_mcp_plugin":
        csproj = next(
            (
                root / "plugins" / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.csproj"
                for root in get_bepinex_bridge_roots()
                if (root / "plugins" / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.csproj").is_file()
            ),
            WORKSPACE_DIR / "bridges" / "bepinex-mcp" / "plugins" / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.csproj",
        )
        if not csproj.is_file():
            raise HTTPException(status_code=404, detail=f"Plugin project not found: {csproj}")
        plugin_dll = Path(DEFAULT_PLUGIN_BUILD_DIR) / "BepInExMCP.IL2CPP.dll"
        if req.keep_backup and plugin_dll.is_file():
            bak = plugin_dll.with_suffix(f".dll.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(plugin_dll, bak)
        proc = subprocess.run(
            ["dotnet", "build", str(csproj), "-c", "Release", "--nologo"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0 or not plugin_dll.is_file():
            raise HTTPException(status_code=500, detail=f"dotnet build failed:\n{proc.stdout}\n{proc.stderr}")
        new_sha = sha256_file(plugin_dll)
        if req.require_checksum and expected and new_sha != expected:
            raise HTTPException(status_code=400, detail=f"Checksum mismatch: expected {expected}, got {new_sha}")
        saved["bepinex_mcp_plugin"] = {
            "version": f"Release ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
            "sha256": new_sha,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_tool_versions(saved)
        summary = f"Rebuilt BepInExMCP.IL2CPP Release plugin (SHA-256: {new_sha[:16]}...)"
        append_audit_log(action="app.tools.upgrade", status="ok", summary=summary, sha256=new_sha, details={"tool_id": tool_id})
        return {"status": "ok", "summary": summary, "sha256": new_sha, "build_output": proc.stdout[-1500:]}

    repo_map = {
        "bepinex6_il2cpp": ("BepInEx/BepInEx", "BepInEx-Unity.IL2CPP-win-x64"),
        "ue4ss": ("UE4SS-RE/RE-UE4SS", "UE4SS"),
        "melonloader": ("LavaGang/MelonLoader", "MelonLoader.x64"),
    }
    if tool_id not in repo_map:
        raise HTTPException(status_code=400, detail=f"Unknown tool_id: {tool_id}")

    repo, asset_prefix = repo_map[tool_id]
    download_url = req.custom_url.strip()
    release_tag = "custom"
    github_digest = ""

    if not download_url:
        api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        gh_req = urllib.request.Request(api_url, headers={"User-Agent": "TheModdingTool/1.0"})
        try:
            with urllib.request.urlopen(gh_req, timeout=20) as resp:
                rel_data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to query GitHub release for {repo}: {exc}")
        release_tag = rel_data.get("tag_name") or "latest"
        for asset in rel_data.get("assets", []):
            name = asset.get("name", "")
            if asset_prefix.lower() in name.lower() and name.lower().endswith(".zip"):
                download_url = asset.get("browser_download_url", "")
                digest = asset.get("digest") or ""
                if digest.startswith("sha256:"):
                    github_digest = digest.split(":", 1)[1].strip().lower()
                break
        if not download_url:
            raise HTTPException(status_code=404, detail=f"No matching .zip asset ({asset_prefix}) found in {repo} {release_tag}.")

    if req.require_https and not download_url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail=f"Blocked insecure non-HTTPS download URL: {download_url}")

    effective_expected = expected or github_digest
    if req.require_checksum and not effective_expected:
        raise HTTPException(
            status_code=400,
            detail="Checksum verification is required, but no Expected SHA-256 was provided and the GitHub release asset did not publish a SHA-256 digest.",
        )

    dl_dir = get_app_data_root() / "tools" / tool_id
    dl_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = dl_dir / f"{tool_id}_{release_tag}.zip.tmp"
    dl_req = urllib.request.Request(download_url, headers={"User-Agent": "TheModdingTool/1.0"})
    try:
        with urllib.request.urlopen(dl_req, timeout=60) as resp, tmp_zip.open("wb") as out_f:
            shutil.copyfileobj(resp, out_f)
    except Exception as exc:
        if tmp_zip.exists():
            tmp_zip.unlink()
        raise HTTPException(status_code=502, detail=f"Download failed: {exc}")

    actual_sha = sha256_file(tmp_zip)
    if req.require_checksum and effective_expected and actual_sha != effective_expected:
        tmp_zip.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Security Check Failed: SHA-256 mismatch! Expected {effective_expected}, got {actual_sha}. Download discarded.",
        )

    if tool_id == "bepinex6_il2cpp":
        assets_dir = WORKSPACE_DIR / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        target_zip = assets_dir / "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.793+dbc82f0.zip"
        if req.keep_backup and target_zip.is_file():
            shutil.copy2(target_zip, target_zip.with_suffix(f".zip.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
        shutil.move(str(tmp_zip), str(target_zip))
        final_path = target_zip
    else:
        final_path = dl_dir / f"{tool_id}_{release_tag}.zip"
        if req.keep_backup and final_path.is_file():
            shutil.copy2(final_path, final_path.with_suffix(f".zip.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
        shutil.move(str(tmp_zip), str(final_path))
        with zipfile.ZipFile(final_path, "r") as zf:
            zf.extractall(dl_dir / "extracted")

    saved[tool_id] = {
        "version": release_tag,
        "sha256": actual_sha,
        "expected_sha256": effective_expected or actual_sha,
        "source_url": download_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_tool_versions(saved)
    summary = f"Upgraded {tool_id} to {release_tag} (SHA-256 verified: {actual_sha[:16]}...)"
    append_audit_log(action="app.tools.upgrade", status="ok", summary=summary, sha256=actual_sha, details=saved[tool_id])
    return {"status": "ok", "summary": summary, "version": release_tag, "sha256": actual_sha, "path": str(final_path)}


@router.get("/api/app/version")
async def get_app_version() -> dict[str, Any]:
    """Returns current SemVer metadata from version.json."""
    vm = _load_workspace_module("version_manager", "version_manager.py")
    return {"status": "ok", **vm.load_version_data()}


class BumpVersionRequest(BaseModel):
    bump_type: str = Field(default="patch")
    note: str = Field(default="Manual version bump from App Management")


@router.post("/api/app/version/bump")
async def bump_app_version(req: BumpVersionRequest) -> dict[str, Any]:
    """Advances the app version (patch, minor, major, or launch -> 1.0.0)."""
    vm = _load_workspace_module("version_manager", "version_manager.py")
    try:
        res = vm.bump_version(req.bump_type, req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    summary = f"Advanced app version to v{res['version']} ({res['channel']})"
    append_audit_log(action="app.version.bump", status="ok", summary=summary, details=res)
    return {"status": "ok", "summary": summary, **res}


@router.get("/api/app/diagnostics")
async def get_app_diagnostics() -> dict[str, Any]:
    """Returns runtime health and storage diagnostics for App Management."""
    import subprocess

    dotnet_ver = "Not installed"
    try:
        r = subprocess.run(["dotnet", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            dotnet_ver = r.stdout.strip()
    except Exception:
        pass

    ver_info = {"version": "0.1.0", "channel": "pre-launch", "launched": False}
    try:
        vm = _load_workspace_module("version_manager", "version_manager.py")
        vdata = vm.load_version_data()
        ver_info = {
            "version": vdata.get("version", "0.1.0"),
            "channel": vdata.get("channel", "pre-launch"),
            "launched": bool(vdata.get("launched", False)),
        }
    except Exception:
        pass

    built_mods_count = len(list(BUILT_MODS_DIR.glob("*"))) if BUILT_MODS_DIR.is_dir() else 0
    return {
        "status": "ok",
        "workspace_dir": str(WORKSPACE_DIR),
        "app_data_dir": str(get_app_data_root()),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "dotnet_version": dotnet_ver,
        "built_mods_count": built_mods_count,
        "pid": os.getpid(),
        "app_version": ver_info["version"],
        "app_channel": ver_info["channel"],
        "app_launched": ver_info["launched"],
    }


@router.post("/api/app/maintenance/clear-built-mods")
async def clear_built_mods_cache() -> dict[str, Any]:
    """Clears temporary built mod artifacts in %LOCALAPPDATA%\\TheModdingTool\\built_mods."""
    removed = 0
    if BUILT_MODS_DIR.is_dir():
        for item in list(BUILT_MODS_DIR.iterdir()):
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            removed += 1
    summary = f"Cleared {removed} item(s) from built mods cache."
    append_audit_log(action="app.maintenance.clear_built_mods", status="ok", summary=summary)
    return {"status": "ok", "summary": summary, "removed": removed}


@router.post("/api/app/maintenance/flush-all")
async def flush_all_app_caches() -> dict[str, Any]:
    """Flushes all in-memory and disk caches (games registry, PC specs, Steam assets, game icons, built mods, audit cache)."""
    global PC_SPECS_RESPONSE_CACHE, _PC_SPECS_MEM_CACHE, AUDIT_LOG_CACHE

    invalidate_games_cache()
    PC_SPECS_RESPONSE_CACHE = None
    _PC_SPECS_MEM_CACHE = None
    AUDIT_LOG_CACHE = None
    STEAM_ASSETS_CACHE.clear()

    removed_items: list[str] = []
    for fpath in (PC_SPECS_CACHE_PATH, STEAM_ASSETS_CACHE_PATH, GAMES_STORE_PATH):
        if fpath.is_file():
            fpath.unlink(missing_ok=True)
            removed_items.append(fpath.name)

    for dpath in (GAME_ICONS_DIR, BUILT_MODS_DIR):
        if dpath.is_dir():
            for item in list(dpath.iterdir()):
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                removed_items.append(item.name)

    # Re-seed clean default game profile immediately
    load_managed_games()
    summary = f"Flushed all game and PC data caches ({len(removed_items)} item(s) removed)."
    append_audit_log(action="app.maintenance.flush_all", status="ok", summary=summary, details={"removed": removed_items[:30]})
    return {"status": "ok", "summary": summary, "removed_count": len(removed_items)}


@router.post("/api/app/restart")
async def restart_app_server() -> dict[str, Any]:
    """Restarts the app cleanly (process restart when supervised, or full runtime/cache reset in-process)."""
    global PC_SPECS_RESPONSE_CACHE, _PC_SPECS_MEM_CACHE, AUDIT_LOG_CACHE
    import threading

    invalidate_games_cache()
    PC_SPECS_RESPONSE_CACHE = None
    _PC_SPECS_MEM_CACHE = None
    AUDIT_LOG_CACHE = None
    STEAM_ASSETS_CACHE.clear()
    load_managed_games()

    append_audit_log(action="app.restart", status="ok", summary="Restarted app from App Management.")

    if os.environ.get("MODTOOL_SUPERVISED") == "1":
        def _delayed_exit():
            import time
            time.sleep(0.4)
            os._exit(42)

        threading.Thread(target=_delayed_exit, daemon=True).start()
        return {"status": "ok", "mode": "process_restart", "summary": "Server restarting on http://127.0.0.1:8765..."}

    def _delayed_reload_touch():
        import time
        time.sleep(0.35)
        try:
            os.utime(Path(__file__).resolve(), None)
        except Exception:
            pass

    threading.Thread(target=_delayed_reload_touch, daemon=True).start()
    return {"status": "ok", "mode": "uvicorn_reload", "summary": "App caches cleared and server reload triggered."}


WATCHER_PROC_HANDLE: dict[str, Any] = {"proc": None}


@router.get("/api/app/watcher/status")
async def get_watcher_status() -> dict[str, Any]:
    """Checks whether backup_manager.py --watch is running."""
    proc = WATCHER_PROC_HANDLE.get("proc")
    managed_running = proc is not None and proc.poll() is None
    return {"status": "ok", "running": managed_running, "pid": proc.pid if managed_running else None}


@router.post("/api/app/watcher/toggle")
async def toggle_backup_watcher() -> dict[str, Any]:
    """Starts or stops the managed incremental backup & SemVer watcher process."""
    import subprocess

    proc = WATCHER_PROC_HANDLE.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        WATCHER_PROC_HANDLE["proc"] = None
        append_audit_log(action="app.watcher.stop", status="ok", summary="Stopped managed backup watcher.")
        return {"status": "ok", "running": False, "summary": "Stopped managed backup watcher."}

    bm_path = WORKSPACE_DIR / "backup_manager.py"
    if not bm_path.is_file():
        raise HTTPException(status_code=404, detail="backup_manager.py not found.")
    new_proc = subprocess.Popen(
        [sys.executable, str(bm_path), "--watch"],
        cwd=str(WORKSPACE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    WATCHER_PROC_HANDLE["proc"] = new_proc
    append_audit_log(action="app.watcher.start", status="ok", summary=f"Started backup watcher (PID {new_proc.pid}).")
    return {"status": "ok", "running": True, "pid": new_proc.pid, "summary": f"Started backup watcher (PID {new_proc.pid})."}


@router.get("/api/health")
async def health_check() -> JSONResponse:
    """Lightweight health check endpoint with no-store cache header."""
    return JSONResponse(content={"status": "ok"}, status_code=200, headers={"Cache-Control": "no-store"})



class DevModeRequest(BaseModel):
    enabled: bool = False


def _load_dev_mode() -> bool:
    if DEV_MODE_PATH.is_file():
        try:
            return bool(json.loads(DEV_MODE_PATH.read_text(encoding="utf-8")).get("enabled", False))
        except Exception:
            return False
    return False


@router.get("/api/app/dev-mode")
async def get_dev_mode() -> dict[str, Any]:
    return {"status": "ok", "enabled": _load_dev_mode()}


@router.post("/api/app/dev-mode")
async def set_dev_mode(req: DevModeRequest) -> dict[str, Any]:
    DEV_MODE_PATH.write_text(json.dumps({"enabled": bool(req.enabled)}, indent=2), encoding="utf-8")
    append_audit_log(
        action="app.dev_mode",
        status="ok",
        summary=f"Developer mode {'enabled' if req.enabled else 'disabled'}",
    )
    return {"status": "ok", "enabled": bool(req.enabled)}


def register_protocol_handler() -> dict[str, Any]:
    """Register HKCU\\Software\\Classes\\moddingtool on Windows to launch start_app.bat --no-browser."""
    if sys.platform != "win32":
        return {
            "ok": False,
            "supported": False,
            "protocol": "moddingtool",
            "detail": "Windows-only custom URL protocol registration.",
        }

    import winreg

    bat_path = (WORKSPACE_DIR / "start_app.bat").resolve()
    command = f'cmd.exe /c ""{bat_path}" --no-browser"'
    base_key_path = r"Software\Classes\moddingtool"
    cmd_key_path = rf"{base_key_path}\shell\open\command"

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base_key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:The Modding Tool Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, cmd_key_path, 0, winreg.KEY_SET_VALUE) as cmd_key:
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)

    return {
        "ok": True,
        "supported": True,
        "protocol": "moddingtool",
        "registry_path": rf"HKCU\{base_key_path}",
        "command": command,
    }


def _perform_process_shutdown() -> None:
    """Signal the supervisor (if present) and exit the current server process cleanly."""
    sig_file = os.environ.get("MODTOOL_SHUTDOWN_SIGNAL_FILE", "").strip()
    if sig_file:
        try:
            Path(sig_file).write_text("stop", encoding="utf-8")
        except OSError:
            pass
    os._exit(0)


def _schedule_process_shutdown(delay_sec: float = 0.35) -> None:
    import threading

    timer = threading.Timer(delay_sec, _perform_process_shutdown)
    timer.daemon = True
    timer.start()


@router.post("/api/system/register-protocol")
async def register_protocol_endpoint() -> dict[str, Any]:
    """Register the user-level moddingtool:// URL protocol handler in HKCU."""
    try:
        result = register_protocol_handler()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to register moddingtool:// protocol: {exc}") from exc

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("detail") or "Protocol registration unavailable")

    append_audit_log(
        action="app.protocol.register",
        status="ok",
        summary=f"Registered {result['registry_path']} -> {result['command']}",
        details=result,
    )
    return result


@router.post("/api/system/shutdown")
async def shutdown_server() -> dict[str, Any]:
    """Log SERVER_STOPPED to audit.jsonl and trigger a clean process exit after returning HTTP 200."""
    append_audit_log(
        action="SERVER_STOPPED",
        status="ok",
        summary="Server stopped via /api/system/shutdown.",
        details={"pid": os.getpid()},
    )
    _schedule_process_shutdown()
    return {"ok": True}


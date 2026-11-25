"""Thin HTTP routes for system health, defaults, logs, PC specs, tool installers, version, maintenance, watcher, and shutdown."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import app.core.audit as _audit
import app.core.config as _cfg
from app.core.audit import append_audit_log, clear_audit_log_cache, log_action_error, read_audit_logs
from app.core.config import (
    AUDIT_APP_LOG_PATH,
    AUDIT_GAME_LOG_PATH,
    AUDIT_LOG_PATH,
    BUILT_MODS_DIR,
    DEFAULT_BEPINEX_SHA256,
    DEFAULT_BEPINEX_ZIP,
    DEFAULT_GAME_DIR,
    DEFAULT_MCP_APP,
    DEFAULT_PLUGIN_BUILD_DIR,
    DEFAULT_VENV_PYTHON,
    DEV_MODE_PATH,
    GAME_ICONS_DIR,
    GAMES_STORE_PATH,
    PC_SPECS_CACHE_PATH,
    STEAM_ASSETS_CACHE,
    STEAM_ASSETS_CACHE_PATH,
    WORKSPACE_DIR,
    _load_workspace_module,
    atomic_write_json,
    clear_pc_specs_cache,
    get_app_data_root,
    invalidate_games_cache,
    load_json_sanitized,
    load_managed_games,
    sha256_text,
)
from app.core.constants import UNREAL_HARDWARE_REFERENCE_ARRAY, _UE_QUALITY_LABELS
from app.engines.unreal import (
    _format_ue_quality,
    evaluate_scalability_traffic_light,
    match_pc_to_unreal_reference_tier,
)
from app.services.conflict_guard import inspect_conflict_guard
from app.services.jobs import get_job
from app.services.pc_specs import clear_local_pc_specs_cache, scrape_pc_specs
from app.services.tool_installer import (
    get_modding_tools_status_impl,
    install_modding_tool_impl,
    upgrade_modding_tool_impl,
)

router = APIRouter()

WATCHER_PROC_HANDLE: dict[str, Any] = {"proc": None}


class InstallToolRequest(BaseModel):
    tool_id: str = Field(default="")
    game_id: str = Field(default="battleground")
    game_dir: str = Field(default=DEFAULT_GAME_DIR)
    force: bool = Field(default=False)


class OpenToolFolderRequest(BaseModel):
    path: str


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


class UpgradeModdingToolRequest(BaseModel):
    tool_id: str
    require_https: bool = Field(default=True)
    require_checksum: bool = Field(default=True)
    expected_sha256: str = Field(default="")
    custom_url: str = Field(default="")
    keep_backup: bool = Field(default=True)


class BumpVersionRequest(BaseModel):
    bump_type: str = Field(default="patch")
    note: str = Field(default="Manual version bump from App Management")


class DevModeRequest(BaseModel):
    enabled: bool = False


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


@router.post("/api/audit/client-error")
async def log_client_error(req: ClientErrorRequest) -> dict[str, Any]:
    """Record a sanitized browser/client error as an ACTION_FAILED audit entry."""
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


@router.post("/api/tools/check-conflicts")
async def check_tool_conflicts(req: InstallToolRequest) -> dict[str, Any]:
    """Pre-flight scan of game directory for Vortex/MO2 markers, proxy DLLs, and ~mods overrides."""
    return inspect_conflict_guard(req.game_dir, intended_action=req.tool_id)


@router.post("/api/tools/install")
async def install_modding_tool(req: InstallToolRequest) -> dict[str, Any]:
    return await install_modding_tool_impl(
        req.tool_id,
        req.game_id,
        req.game_dir,
        force=req.force,
        pc_specs_fn=scrape_pc_specs,
    )


@router.post("/api/tools/open-folder")
async def open_tool_folder(req: OpenToolFolderRequest) -> dict[str, Any]:
    """Open the specified tool or game directory in Windows Explorer."""
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


@router.get("/api/pc-specs")
async def get_pc_specs() -> dict[str, Any]:
    """Return scraped PC hardware specifications plus the Unreal Hardware Reference Array."""
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
    """Force a fresh scrape of PC hardware & OS specifications and log it."""
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


@router.get("/api/app/tools/status")
async def get_modding_tools_status() -> dict[str, Any]:
    return get_modding_tools_status_impl()


@router.post("/api/app/tools/upgrade")
async def upgrade_modding_tool(req: UpgradeModdingToolRequest) -> dict[str, Any]:
    return upgrade_modding_tool_impl(
        req.tool_id,
        require_https=req.require_https,
        require_checksum=req.require_checksum,
        expected_sha256=req.expected_sha256,
        custom_url=req.custom_url,
        keep_backup=req.keep_backup,
    )


@router.get("/api/app/version")
async def get_app_version() -> dict[str, Any]:
    """Return current SemVer metadata from version.json."""
    vm = _load_workspace_module("version_manager", "version_manager.py")
    return {"status": "ok", **vm.load_version_data()}


@router.post("/api/app/version/bump")
async def bump_app_version(req: BumpVersionRequest) -> dict[str, Any]:
    """Advance the app version (patch, minor, major, or launch -> 1.0.0)."""
    vm = _load_workspace_module("version_manager", "version_manager.py")
    try:
        res = vm.bump_version(req.bump_type, req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    summary = f"Advanced app version to v{res['version']} ({res['channel']})"
    append_audit_log(action="app.version.bump", status="ok", summary=summary, details=res)
    return {"status": "ok", "summary": summary, **res}


@router.get("/api/jobs/{job_id}")
async def get_background_job_status(job_id: str) -> dict[str, Any]:
    """Return status, percentage, stage, and result for a background job."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found or expired: {job_id}")
    return job


@router.get("/api/app/diagnostics")
async def get_app_diagnostics() -> dict[str, Any]:
    """Return runtime health and storage diagnostics for App Management."""
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
    """Clear temporary built mod artifacts in %LOCALAPPDATA%\\TheModdingTool\\built_mods."""
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
    """Flush all in-memory and disk caches."""
    invalidate_games_cache()
    clear_pc_specs_cache()
    clear_local_pc_specs_cache()
    clear_audit_log_cache()
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

    load_managed_games()
    summary = f"Flushed all game and PC data caches ({len(removed_items)} item(s) removed)."
    append_audit_log(action="app.maintenance.flush_all", status="ok", summary=summary, details={"removed": removed_items[:30]})
    return {"status": "ok", "summary": summary, "removed_count": len(removed_items)}


@router.post("/api/app/restart")
async def restart_app_server() -> dict[str, Any]:
    """Restart the app cleanly (process restart when supervised, or reload touch in-process)."""
    invalidate_games_cache()
    clear_pc_specs_cache()
    clear_local_pc_specs_cache()
    clear_audit_log_cache()
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


@router.get("/api/app/watcher/status")
async def get_watcher_status() -> dict[str, Any]:
    """Check whether backup_manager.py --watch is running."""
    proc = WATCHER_PROC_HANDLE.get("proc")
    managed_running = proc is not None and proc.poll() is None
    return {"status": "ok", "running": managed_running, "pid": proc.pid if managed_running else None}


@router.post("/api/app/watcher/toggle")
async def toggle_backup_watcher() -> dict[str, Any]:
    """Start or stop the managed incremental backup & SemVer watcher process."""
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


def _load_dev_mode() -> bool:
    if DEV_MODE_PATH.is_file():
        try:
            data = load_json_sanitized(DEV_MODE_PATH, persist=True)
            return bool((data or {}).get("enabled", False)) if isinstance(data, dict) else False
        except Exception:
            return False
    return False


@router.get("/api/app/dev-mode")
async def get_dev_mode() -> dict[str, Any]:
    return {"status": "ok", "enabled": _load_dev_mode()}


@router.post("/api/app/dev-mode")
async def set_dev_mode(req: DevModeRequest) -> dict[str, Any]:
    atomic_write_json(DEV_MODE_PATH, {"enabled": bool(req.enabled)})
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

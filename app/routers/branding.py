"""Branding, custom favicon/banner upload, resizing, reset, and UI config endpoints."""

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

from app.core.audit import append_audit_log, clear_audit_log_cache, read_audit_logs
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
    POC_DIR,
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
    backup_dir_for_game,
    clear_pc_specs_cache,
    default_managed_games,
    get_app_data_root,
    invalidate_games_cache,
    load_managed_games,
    manifest_path_for_game,
    save_managed_games,
    sha256_file,
    sha256_text,
)


router = APIRouter()

APP_UI_CONFIG_PATH = get_app_data_root() / "app_ui_config.json"
BRANDING_DIR = WORKSPACE_DIR / "branding"
BRANDING_BANNERS_DIR = BRANDING_DIR / "banners"
BRANDING_FAVICONS_DIR = BRANDING_DIR / "favicons"
BRANDING_BANNERS_DIR.mkdir(parents=True, exist_ok=True)
BRANDING_FAVICONS_DIR.mkdir(parents=True, exist_ok=True)


def default_app_ui_config() -> dict[str, Any]:
    return {
        "app_name": "Unity IL2CPP Modding Manager & Live MCP Console",
        "app_subtitle": "FastAPI + Persistent MCP Stdio Bridge (bepinex-mcp)",
        "accent_color": "#3b82f6",
        "compact_mode": False,
        "startup_tab": "manager",
        "banner_image_url": "",
        "banner_height_px": 110,
        "favicon_url": "",
        "tab_labels": {
            "manager": "Game Manager",
            "game": "Active Game",
            "modbuilder": "Build a Mod",
            "specs": "PC Specs",
            "logs": "Audit Log",
        },
        "visibility": {
            "header_subtitle": True,
            "header_quick_load": True,
            "header_active_badge": True,
            "header_server_badge": True,
            "tab_manager": True,
            "tab_game": True,
            "tab_modbuilder": True,
            "tab_specs": True,
            "tab_logs": True,
            "sec_pipeline_status": True,
            "sec_provision_restore": True,
            "sec_live_mcp": True,
        },
        "in_progress": {
            "manager": False,
            "game": False,
            "modbuilder": False,
            "specs": False,
            "logs": False,
        },
    }


@router.get("/api/app-ui-config")
async def get_app_ui_config() -> dict[str, Any]:
    """Returns persisted UI customization settings or defaults."""
    cfg = default_app_ui_config()
    if APP_UI_CONFIG_PATH.is_file():
        try:
            saved = json.loads(APP_UI_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for k in ("app_name", "app_subtitle", "accent_color", "compact_mode", "startup_tab", "banner_image_url", "banner_height_px", "favicon_url"):
                    if k in saved:
                        cfg[k] = saved[k]
                if isinstance(saved.get("tab_labels"), dict):
                    cfg["tab_labels"].update(saved["tab_labels"])
                if isinstance(saved.get("visibility"), dict):
                    cfg["visibility"].update(saved["visibility"])
                if isinstance(saved.get("in_progress"), dict):
                    cfg["in_progress"].update(saved["in_progress"])
        except Exception:
            pass
    if not cfg.get("banner_image_url") and list(BRANDING_BANNERS_DIR.glob("banner.*")):
        cfg["banner_image_url"] = "/api/app-ui-config/asset/banner"
    if not cfg.get("favicon_url") and list(BRANDING_FAVICONS_DIR.glob("favicon.*")):
        cfg["favicon_url"] = "/api/app-ui-config/asset/favicon"
    return {"status": "ok", "config": cfg, "config_path": str(APP_UI_CONFIG_PATH)}


@router.post("/api/app-ui-config")
async def save_app_ui_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Saves UI customization settings (app name, visibility toggles, theme) to disk."""
    cfg = default_app_ui_config()
    incoming = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if isinstance(incoming, dict):
        for k in ("app_name", "app_subtitle", "accent_color", "compact_mode", "startup_tab"):
            if k in incoming:
                cfg[k] = incoming[k]
        if isinstance(incoming.get("tab_labels"), dict):
            cfg["tab_labels"].update(incoming["tab_labels"])
        if isinstance(incoming.get("visibility"), dict):
            cfg["visibility"].update(incoming["visibility"])
        if isinstance(incoming.get("in_progress"), dict):
            cfg["in_progress"].update(incoming["in_progress"])

    APP_UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_UI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    cfg_hash = sha256_text(json.dumps(cfg, sort_keys=True))
    append_audit_log(
        action="app_ui.save",
        status="ok",
        summary=f"Saved App Editor settings ('{cfg.get('app_name')}')",
        sha256=cfg_hash,
        details={"app_name": cfg.get("app_name"), "startup_tab": cfg.get("startup_tab")},
    )
    return {"status": "ok", "config": cfg, "config_path": str(APP_UI_CONFIG_PATH), "sha256": cfg_hash}


class UploadAppAssetRequest(BaseModel):
    kind: str
    filename: str = Field(default="asset.png")
    data_url: str


@router.post("/api/app-ui-config/upload-asset")
async def upload_app_asset(req: UploadAppAssetRequest) -> dict[str, Any]:
    """Saves an uploaded responsive banner image into branding/banners or favicon into branding/favicons."""
    import base64

    kind = req.kind.strip().lower()
    if kind not in ("banner", "favicon"):
        raise HTTPException(status_code=400, detail="Asset kind must be 'banner' or 'favicon'.")
    raw_url = req.data_url.strip()
    if not raw_url.startswith("data:") or "," not in raw_url:
        raise HTTPException(status_code=400, detail="Expected base64 data URL.")
    header, b64_data = raw_url.split(",", 1)
    if len(b64_data) > 7_000_000:
        raise HTTPException(status_code=400, detail="Asset exceeds 5 MB limit.")

    ext = ".png"
    if "image/jpeg" in header or "image/jpg" in header:
        ext = ".jpg"
    elif "image/webp" in header:
        ext = ".webp"
    elif "image/svg+xml" in header:
        ext = ".svg"
    elif "image/x-icon" in header or "vnd.microsoft.icon" in header or req.filename.lower().endswith(".ico"):
        ext = ".ico"

    target_dir = BRANDING_BANNERS_DIR if kind == "banner" else BRANDING_FAVICONS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for old in target_dir.glob(f"{kind}.*"):
        old.unlink(missing_ok=True)
    dest = target_dir / f"{kind}{ext}"
    dest.write_bytes(base64.b64decode(b64_data))
    url = f"/api/app-ui-config/asset/{kind}?v={int(datetime.now().timestamp())}"

    # Persist immediately into app_ui_config.json
    cfg = (await get_app_ui_config())["config"]
    if kind == "banner":
        cfg["banner_image_url"] = url
    else:
        cfg["favicon_url"] = url
    APP_UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_UI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    append_audit_log(action="app_ui.upload_asset", status="ok", summary=f"Uploaded custom {kind} ({dest})")
    return {"status": "ok", "kind": kind, "url": url, "path": str(dest), "config": cfg}


@router.get("/api/app-ui-config/asset/{kind}")
async def serve_app_asset(kind: str):
    """Serves the uploaded custom banner from branding/banners or favicon from branding/favicons."""
    if kind not in ("banner", "favicon"):
        return Response(status_code=404)
    target_dir = BRANDING_BANNERS_DIR if kind == "banner" else BRANDING_FAVICONS_DIR
    matches = list(target_dir.glob(f"{kind}.*"))
    if not matches:
        return Response(status_code=404)
    asset = matches[0]
    ext = asset.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")
    return FileResponse(asset, media_type=mime, headers={"Cache-Control": "no-cache"})


class RemoveAppAssetRequest(BaseModel):
    kind: str


@router.post("/api/app-ui-config/remove-asset")
async def remove_app_asset(req: RemoveAppAssetRequest) -> dict[str, Any]:
    """Deletes the uploaded banner or favicon from branding/ and clears it in app_ui_config.json."""
    kind = req.kind.strip().lower()
    if kind not in ("banner", "favicon"):
        raise HTTPException(status_code=400, detail="Asset kind must be 'banner' or 'favicon'.")

    target_dir = BRANDING_BANNERS_DIR if kind == "banner" else BRANDING_FAVICONS_DIR
    removed_files: list[str] = []
    if target_dir.is_dir():
        for old in target_dir.glob(f"{kind}.*"):
            old.unlink(missing_ok=True)
            removed_files.append(str(old))

    cfg = (await get_app_ui_config())["config"]
    if kind == "banner":
        cfg["banner_image_url"] = ""
    else:
        cfg["favicon_url"] = ""
    APP_UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_UI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    summary = f"Removed custom {kind}" + (f" ({len(removed_files)} file(s))" if removed_files else "")
    append_audit_log(action="app_ui.remove_asset", status="ok", summary=summary)
    return {"status": "ok", "kind": kind, "removed_files": removed_files, "config": cfg}


@router.post("/api/app-ui-config/reset")
async def reset_app_ui_config() -> dict[str, Any]:
    """Resets UI customization settings back to defaults."""
    if APP_UI_CONFIG_PATH.is_file():
        APP_UI_CONFIG_PATH.unlink()
    cfg = default_app_ui_config()
    append_audit_log(
        action="app_ui.reset",
        status="ok",
        summary="Reset App Editor settings to defaults",
    )
    return {"status": "ok", "config": cfg, "config_path": str(APP_UI_CONFIG_PATH)}




BANNER_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def load_ui_settings() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "banner_enabled": True,
        "banner_file": "",
        "banner_fit": "contain",
    }
    if UI_SETTINGS_PATH.is_file():
        try:
            raw = json.loads(UI_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                defaults.update(raw)
        except Exception:
            pass
    if defaults.get("banner_fit") not in ("contain", "cover"):
        defaults["banner_fit"] = "contain"
    defaults["banner_enabled"] = bool(defaults.get("banner_enabled", True))
    defaults["banner_file"] = str(defaults.get("banner_file") or "")
    return defaults


def save_ui_settings(settings: dict[str, Any]) -> None:
    UI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    UI_SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class BannerSettingsRequest(BaseModel):
    banner_enabled: bool | None = None
    banner_fit: str | None = None


@router.post("/api/ui/banner")
async def upload_ui_banner(file: UploadFile = File(...)) -> dict[str, Any]:
    """Saves a custom banner image to %LOCALAPPDATA%\\ModToolPoC\\custom_banner.<ext> and updates ui_settings.json."""
    orig_name = str(file.filename or "banner.png")
    ext = Path(orig_name).suffix.lower()
    if ext not in BANNER_MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image extension '{ext}'. Allowed: png, jpg, jpeg, webp, svg.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded banner file is empty.")

    app_root = get_app_data_root()
    for old_file in app_root.glob("custom_banner.*"):
        if old_file.is_file():
            try:
                old_file.unlink()
            except Exception:
                pass

    target_name = f"custom_banner{ext}"
    target_path = app_root / target_name
    target_path.write_bytes(data)

    settings = load_ui_settings()
    settings["banner_enabled"] = True
    settings["banner_file"] = target_name
    if settings.get("banner_fit") not in ("contain", "cover"):
        settings["banner_fit"] = "contain"
    save_ui_settings(settings)

    append_audit_log(
        action="ui.banner_upload",
        status="ok",
        summary=f"Uploaded custom banner '{target_name}' ({len(data)} bytes)",
        details={"banner_file": target_name, "original_name": orig_name, "size_bytes": len(data)},
    )
    return {
        "status": "ok",
        "banner_enabled": settings["banner_enabled"],
        "banner_file": settings["banner_file"],
        "banner_fit": settings["banner_fit"],
    }


@router.get("/api/ui/banner")
async def get_ui_banner() -> Response:
    """Returns the saved custom banner image with Cache-Control: no-store, or 204 if none is set."""
    settings = load_ui_settings()
    banner_file = str(settings.get("banner_file") or "").strip()
    if not banner_file:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    app_root = get_app_data_root()
    banner_path = app_root / Path(banner_file).name
    if not banner_path.is_file():
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    media_type = BANNER_MEDIA_TYPES.get(banner_path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        banner_path,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/ui/banner/settings")
async def get_ui_banner_settings() -> dict[str, Any]:
    settings = load_ui_settings()
    app_root = get_app_data_root()
    has_file = bool(settings.get("banner_file") and (app_root / Path(settings["banner_file"]).name).is_file())
    return {
        "status": "ok",
        "banner_enabled": settings["banner_enabled"],
        "banner_file": settings["banner_file"],
        "banner_fit": settings["banner_fit"],
        "has_banner": has_file,
    }


@router.post("/api/ui/banner/settings")
async def update_ui_banner_settings(req: BannerSettingsRequest) -> dict[str, Any]:
    """Persists banner_enabled and/or banner_fit ('contain' | 'cover') to ui_settings.json."""
    settings = load_ui_settings()
    if req.banner_enabled is not None:
        settings["banner_enabled"] = bool(req.banner_enabled)
    if req.banner_fit is not None:
        fit = req.banner_fit.strip().lower()
        if fit not in ("contain", "cover"):
            raise HTTPException(status_code=400, detail="banner_fit must be 'contain' or 'cover'.")
        settings["banner_fit"] = fit
    save_ui_settings(settings)

    append_audit_log(
        action="ui.banner_settings",
        status="ok",
        summary=f"Updated banner settings (enabled={settings['banner_enabled']}, fit={settings['banner_fit']})",
        details=settings,
    )
    return {
        "status": "ok",
        "banner_enabled": settings["banner_enabled"],
        "banner_file": settings["banner_file"],
        "banner_fit": settings["banner_fit"],
    }


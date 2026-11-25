"""Thin HTTP routes for managed games, discovery scans, Unreal INI settings, assets, and unified library."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.core.audit import append_audit_log
from app.core.config import (
    DEFAULT_BEPINEX_SHA256,
    DEFAULT_BEPINEX_ZIP,
    DEFAULT_GAME_DIR,
    DEFAULT_MCP_APP,
    DEFAULT_PLUGIN_BUILD_DIR,
    DEFAULT_VENV_PYTHON,
    GAMES_RESPONSE_CACHE,
    GAMES_STORE_PATH,
    LIBRARY_CACHE_PATH,
    load_managed_games,
    save_managed_games,
    sha256_text,
)
from app.core.constants import (
    NON_GAME_LOCALAPPDATA_FOLDERS,
    NON_GAME_NAME_PATTERNS,
    NON_GAME_STEAM_APPIDS,
    UNREAL_HARDWARE_REFERENCE_ARRAY,
    _UE_QUALITY_LABELS,
)
from app.engines.generic import fingerprint_library_entry, scan_installed_mods
from app.engines.unreal import (
    _format_ue_quality,
    apply_unreal_ini_updates,
    discover_local_unreal_ini_profiles,
    evaluate_scalability_traffic_light,
    inspect_unreal_game_settings,
    match_pc_to_unreal_reference_tier,
    parse_unreal_ini_file,
    restore_unreal_ini_backup,
)
from app.services import discovery as _discovery
from app.services.discovery import (
    STEAM_REF_CSV,
    _canonical_entry_id,
    _fetch_gog_owned_titles,
    _fetch_steam_owned_titles,
    _find_primary_game_exe,
    _find_steam_appid_and_assets,
    _locate_steam_root,
    _normalize_library_title,
    build_managed_game_from_folder,
    discover_platform_roots,
    enrich_uninstalled_with_pcgw,
    is_game_candidate,
    is_non_game_entry,
    load_library_cache,
    load_scan_exclusions,
    load_steam_reference_sheet,
    lookup_sheet_reference,
    make_slug,
    save_library_cache,
    save_scan_exclusions,
    scan_epic_installed,
    scan_gog_installed,
    scan_manual_folders,
)
from app.services.conflict_guard import inspect_conflict_guard
from app.services.game_snapshots import (
    create_game_snapshot,
    list_game_snapshots,
    restore_game_snapshot,
)
from app.services.game_store import (
    enrich_game_status,
    evaluate_modding_apps_for_game,
    extract_game_icon_png,
    inspect_game_modding_info,
    prewarm_game_icons_batch,
    prune_non_games_from_store,
    rescan_game_sync,
    scan_game_conflicts,
    select_and_enrich_game,
)
from app.services.pc_specs import scrape_pc_specs

router = APIRouter()


class ManagedGameConfig(BaseModel):
    id: str
    name: str
    tab_label: str = Field(default="")
    has_dedicated_tab: bool = Field(default=False)
    runtime: str = Field(default="Unity_IL2CPP")
    game_dir: str = Field(default=DEFAULT_GAME_DIR)
    bepinex_zip_path: str = Field(default=DEFAULT_BEPINEX_ZIP)
    expected_sha256: str = Field(default=DEFAULT_BEPINEX_SHA256)
    plugin_build_dir: str = Field(default=DEFAULT_PLUGIN_BUILD_DIR)
    venv_python_path: str = Field(default=DEFAULT_VENV_PYTHON)
    mcp_app_path: str = Field(default=DEFAULT_MCP_APP)
    game_ip: str = Field(default="localhost")
    default_entity: str = Field(default="paladin_piece")
    default_variable: str = Field(default="stat_hp_max")
    default_go_id: str = Field(default="-269924")
    default_component: str = Field(default="Jade.GameView.CameraTargetCharacter")
    default_member: str = Field(default="targetPiece.stats.Health.maxValue")
    default_value: str = Field(default="23")
    default_profile: str = Field(default="battlemarked_stat_hp_max")
    default_selector: dict[str, Any] = Field(default_factory=dict)
    notes: str = Field(default="")


class SelectGameRequest(BaseModel):
    game_id: str


class ScanGamesRequest(BaseModel):
    custom_path: str = Field(default="")
    auto_populate: bool = Field(default=True)
    unity_only: bool = Field(default=False)
    discovery_scope: str = Field(default="all")


class BrowseFolderRequest(BaseModel):
    initial_dir: str = Field(default="")
    title: str = Field(default="Select Game or Platform Library Folder")


class ImportUnrealSettingsRequest(BaseModel):
    game_id: str = Field(default="")
    custom_ini_path: str = Field(default="")
    reference_tier_id: str = Field(default="auto")


class ApplyUnrealSettingsRequest(BaseModel):
    game_id: str
    ini_path: str
    updates: dict[str, str] = Field(default_factory=dict)


class GameNotesRequest(BaseModel):
    favorite: bool | None = None
    tags: list[str] | None = None
    user_notes: str | None = None


class CreateSnapshotRequest(BaseModel):
    label: str = Field(default="")


class LibraryScanRequest(BaseModel):
    custom_roots: list[str] = Field(default_factory=list)
    single_folder: str = ""
    save_custom_roots: bool = True


class LibraryImportOwnedRequest(BaseModel):
    steam_id: str = ""
    steam_api_key: str = ""
    include_gog: bool = True
    enrich_metadata: bool = True


class LibraryImportFileRequest(BaseModel):
    content: str = ""
    entries: list[dict[str, Any]] = Field(default_factory=list)
    enrich_metadata: bool = True


def scan_steam_installed() -> list[dict[str, Any]]:
    """Delegate to discovery service while allowing tests to patch `_locate_steam_root`."""
    return _discovery.scan_steam_installed(steam_root=_locate_steam_root())


SCAN_PROGRESS: dict[str, Any] = {
    "active": False,
    "kind": "",
    "phase": "",
    "current": 0,
    "total": 0,
    "percent": 0,
    "current_item": "",
}


@router.get("/api/games")
async def list_games(request: Request):
    now = time.monotonic()
    cached = GAMES_RESPONSE_CACHE.get("payload")
    etag = GAMES_RESPONSE_CACHE.get("etag")
    if cached is not None and (now - float(GAMES_RESPONSE_CACHE.get("ts", 0.0))) < 300.0:
        if etag and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(content=cached, headers={"ETag": etag} if etag else {})

    store = load_managed_games()
    games = [enrich_game_status(g, refresh_live=False, pc_specs_fn=scrape_pc_specs) for g in store.get("games", [])]
    payload = {
        "active_game_id": store.get("active_game_id", "battleground"),
        "store_path": str(GAMES_STORE_PATH),
        "games": games,
    }
    etag = f'W/"{sha256_text(json.dumps(payload, sort_keys=True, default=str))[:16]}"'
    GAMES_RESPONSE_CACHE["ts"] = now
    GAMES_RESPONSE_CACHE["payload"] = payload
    GAMES_RESPONSE_CACHE["etag"] = etag
    return JSONResponse(content=payload, headers={"ETag": etag})


@router.post("/api/games/select")
@router.post("/api/games/load")
async def select_game(req: SelectGameRequest) -> dict[str, Any]:
    return await select_and_enrich_game(req.game_id, pc_specs_fn=scrape_pc_specs)


@router.post("/api/games/rescan")
async def rescan_game_metadata(req: SelectGameRequest) -> dict[str, Any]:
    """Explicitly rescan the game folder on disk to refresh cached metadata."""
    return rescan_game_sync(req.game_id, pc_specs_fn=scrape_pc_specs)


@router.get("/api/games/scan-progress")
async def get_scan_progress() -> dict[str, Any]:
    """Return live progress for an active Game scan or Game info scan."""
    return {"status": "ok", **SCAN_PROGRESS}


@router.post("/api/games/scan-info")
async def scan_all_games_info() -> dict[str, Any]:
    """Scrape and populate Game Info metadata for all managed games."""
    store = load_managed_games()
    games_list = store.get("games", [])
    total_games = len(games_list)
    SCAN_PROGRESS.update({
        "active": True,
        "kind": "game_info",
        "phase": "Inspecting game metadata...",
        "current": 0,
        "total": total_games,
        "percent": 0 if total_games else 100,
        "current_item": "",
    })
    now_iso = datetime.now(timezone.utc).isoformat()
    scraped_count = 0
    missing_count = 0
    scraped_games: list[dict[str, Any]] = []

    try:
        for idx, g in enumerate(games_list, start=1):
            g_name = str(g.get("name", g.get("id", "")))
            SCAN_PROGRESS.update({
                "phase": f"Scraping Game Info ({idx}/{total_games})",
                "current": idx,
                "total": total_games,
                "percent": round((idx / total_games) * 100) if total_games else 100,
                "current_item": g_name,
            })
            gdir = Path(str(g.get("game_dir", "")).strip())
            if gdir.is_dir():
                scraped_info = await asyncio.to_thread(
                    inspect_game_modding_info, gdir, g_name, pc_specs_fn=scrape_pc_specs
                )
                scraped_info["metadata_scraped"] = True
                g["cached_modding_info"] = scraped_info
                g["metadata_scraped"] = True
                g["missing_on_disk"] = False
                g["last_scraped_at"] = now_iso
                scraped_count += 1
                scraped_games.append({
                    "id": g.get("id"),
                    "name": g.get("name"),
                    "status": "scraped",
                    "mcp_category": scraped_info.get("mcp_category"),
                    "unity_version": scraped_info.get("unity_version"),
                    "modding_readiness": scraped_info.get("modding_readiness"),
                })
            else:
                g["missing_on_disk"] = True
                missing_count += 1
                scraped_games.append({
                    "id": g.get("id"),
                    "name": g.get("name"),
                    "status": (
                        "missing_on_disk (retained cached info)"
                        if g.get("metadata_scraped")
                        else "missing_on_disk (no cached info)"
                    ),
                })
    finally:
        SCAN_PROGRESS.update({
            "active": False,
            "percent": 100,
            "current": total_games,
            "phase": "Game Info scan complete",
        })

    save_managed_games(store)
    total = len(store.get("games", []))
    summary = (
        f"Populated Game Info metadata for {scraped_count} of {total} managed game(s)"
        + (f" ({missing_count} missing on disk; cached info retained)" if missing_count else "")
    )
    batch_hash = sha256_text(json.dumps(scraped_games, sort_keys=True, default=str))
    append_audit_log(
        action="game_manager.scan_all_info",
        status="warning" if missing_count else "ok",
        summary=summary,
        sha256=batch_hash,
        details={"scraped_count": scraped_count, "missing_count": missing_count, "total_games": total},
    )
    return {
        "status": "ok",
        "summary": summary,
        "scraped_count": scraped_count,
        "missing_count": missing_count,
        "total_games": total,
        "results": scraped_games,
        "games": [enrich_game_status(g, pc_specs_fn=scrape_pc_specs) for g in store.get("games", [])],
    }


@router.post("/api/games")
async def upsert_game(cfg: ManagedGameConfig) -> dict[str, Any]:
    store = load_managed_games()
    game_dict = cfg.model_dump()
    if not game_dict["tab_label"]:
        game_dict["tab_label"] = game_dict["name"]

    replaced = False
    for idx, existing in enumerate(store["games"]):
        if existing["id"] == cfg.id:
            for keep_key in ("cached_modding_info", "metadata_scraped", "last_scraped_at", "missing_on_disk", "platform_label"):
                if keep_key in existing and keep_key not in game_dict:
                    game_dict[keep_key] = existing[keep_key]
            store["games"][idx] = game_dict
            replaced = True
            break
    if not replaced:
        game_dict["metadata_scraped"] = False
        game_dict["missing_on_disk"] = not Path(cfg.game_dir.strip()).is_dir()
        store["games"].append(game_dict)

    store["active_game_id"] = cfg.id
    save_managed_games(store)
    action_label = "updated" if replaced else "added"
    append_audit_log(
        action=f"game_manager.{action_label}",
        status="ok",
        summary=f"Managed game '{cfg.name}' ({cfg.id}) {action_label}",
        details={"game_id": cfg.id, "game_dir": cfg.game_dir},
    )
    return {
        "status": "ok",
        "active_game_id": cfg.id,
        "game": enrich_game_status(game_dict, pc_specs_fn=scrape_pc_specs),
        "games": [enrich_game_status(g, pc_specs_fn=scrape_pc_specs) for g in store["games"]],
    }


@router.delete("/api/games/{game_id}")
async def delete_game(game_id: str) -> dict[str, Any]:
    if game_id == "battleground":
        raise HTTPException(status_code=400, detail="Default 'battleground' profile cannot be deleted.")
    store = load_managed_games()
    before = len(store["games"])
    store["games"] = [g for g in store["games"] if g["id"] != game_id]
    if len(store["games"]) == before:
        raise HTTPException(status_code=404, detail=f"Game '{game_id}' not found.")
    if store.get("active_game_id") == game_id:
        store["active_game_id"] = store["games"][0]["id"]
    save_managed_games(store)
    append_audit_log(
        action="game_manager.deleted",
        status="ok",
        summary=f"Removed managed game '{game_id}' from Game Manager",
    )
    return {
        "status": "ok",
        "active_game_id": store["active_game_id"],
        "games": [enrich_game_status(g, pc_specs_fn=scrape_pc_specs) for g in store["games"]],
    }


@router.post("/api/games/scan")
async def scan_for_games(req: ScanGamesRequest) -> dict[str, Any]:
    store = load_managed_games()

    def norm_path_str(p_str: str) -> str:
        try:
            return str(Path(p_str).resolve()).lower()
        except Exception:
            return p_str.strip().lower()

    existing_by_dir: dict[str, dict[str, Any]] = {}
    for g in store.get("games", []):
        if g.get("game_dir"):
            existing_by_dir[norm_path_str(g["game_dir"])] = g
    existing_ids = {g["id"] for g in store.get("games", [])}

    effective_scope = (
        "unity_all"
        if (req.unity_only and (req.discovery_scope or "all") == "all")
        else (req.discovery_scope or "all").strip().lower()
    )

    scanned_roots: list[dict[str, str]] = []
    candidate_folders: list[tuple[str, Path]] = []

    if req.custom_path.strip():
        cpath = Path(req.custom_path.strip())
        if not cpath.is_dir():
            raise HTTPException(status_code=400, detail=f"Folder does not exist: {cpath}")
        scanned_roots.append({"platform": "Manual / Custom Path", "path": str(cpath.resolve())})
        has_root_game_markers = (
            (cpath / "GameAssembly.dll").is_file()
            or any(cpath.glob("*_Data"))
            or (cpath / "Engine").is_dir()
            or (cpath / "Content").is_dir()
            or any(p.is_file() for p in cpath.glob("*.exe"))
            or any(
                c.is_dir() and (c / "Binaries").is_dir() and (c / "Content" / "Paks").is_dir()
                for c in cpath.iterdir()
            )
        )
        if (cpath / "Content").is_dir():
            candidate_folders.append(("Xbox / Manual Path", cpath / "Content"))
        elif has_root_game_markers:
            is_direct, _ = is_game_candidate(cpath, discovery_scope="all", platform_label="Manual Path")
            if is_direct:
                candidate_folders.append(("Manual Path", cpath))
        else:
            for child in cpath.iterdir():
                if child.is_dir():
                    if (child / "Content").is_dir():
                        candidate_folders.append(("Xbox Library", child / "Content"))
                    else:
                        candidate_folders.append(("Manual Library", child))
    else:
        for platform_label, root_dir in discover_platform_roots():
            scanned_roots.append({"platform": platform_label, "path": str(root_dir)})
            try:
                for child in root_dir.iterdir():
                    if not child.is_dir():
                        continue
                    if (child / "Content").is_dir():
                        candidate_folders.append((platform_label, child / "Content"))
                    else:
                        candidate_folders.append((platform_label, child))
            except Exception:
                continue

    discovered: list[dict[str, Any]] = []
    added_games: list[dict[str, Any]] = []
    seen_dirs: set[str] = set()
    total_candidates = len(candidate_folders)

    SCAN_PROGRESS.update({
        "active": True,
        "kind": "games_scan",
        "phase": "Scanning platform game folders...",
        "current": 0,
        "total": total_candidates,
        "percent": 0 if total_candidates else 100,
        "current_item": "",
    })

    try:
        for idx, (platform_label, folder) in enumerate(candidate_folders, start=1):
            SCAN_PROGRESS.update({
                "phase": f"Scanning {platform_label} ({idx}/{total_candidates})",
                "current": idx,
                "total": total_candidates,
                "percent": round((idx / total_candidates) * 100) if total_candidates else 100,
                "current_item": folder.name,
            })

            norm_dir = norm_path_str(str(folder))
            if norm_dir in seen_dirs:
                continue
            seen_dirs.add(norm_dir)

            ok, runtime_kind = await asyncio.to_thread(
                is_game_candidate,
                folder,
                effective_scope,
                platform_label,
            )
            if not ok:
                continue

            already_managed = norm_dir in existing_by_dir
            if not already_managed and req.auto_populate:
                entry = build_managed_game_from_folder(folder, platform_label, runtime_kind)
                base_slug = entry["id"]
                slug = base_slug
                counter = 2
                while slug in existing_ids:
                    slug = f"{base_slug}_{counter}"
                    counter += 1
                entry["id"] = slug
                existing_ids.add(slug)
                store["games"].append(entry)
                existing_by_dir[norm_dir] = entry
                enriched_entry = enrich_game_status(entry, pc_specs_fn=scrape_pc_specs)
                added_games.append(enriched_entry)
                discovered.append({**enriched_entry, "scan_status": "added"})
            else:
                existing_entry = existing_by_dir.get(
                    norm_dir, build_managed_game_from_folder(folder, platform_label, runtime_kind)
                )
                existing_entry["missing_on_disk"] = False
                discovered.append({
                    **enrich_game_status(existing_entry, pc_specs_fn=scrape_pc_specs),
                    "scan_status": "already_managed" if already_managed else "found",
                })
    finally:
        SCAN_PROGRESS.update({
            "active": False,
            "percent": 100,
            "current": total_candidates,
            "phase": "Platform scan complete",
        })

    missing_games: list[str] = []
    for g in store.get("games", []):
        gdir_str = str(g.get("game_dir", "")).strip()
        gdir = Path(gdir_str) if gdir_str else None
        is_missing = (gdir is None) or (not gdir.is_dir())
        if not req.custom_path.strip() and not req.unity_only:
            is_missing = is_missing or (norm_path_str(gdir_str) not in seen_dirs and not (gdir and gdir.is_dir()))
        g["missing_on_disk"] = is_missing
        if is_missing:
            missing_games.append(g["name"])

    save_managed_games(store)

    summary = (
        f"Auto-populate [Scope: {effective_scope}] checked {len(scanned_roots)} platform directory root(s): "
        f"found {len(discovered)} matching game(s), added {len(added_games)} new game(s), "
        f"missing {len(missing_games)} previously listed game(s)"
    )
    scan_hash = sha256_text(
        json.dumps(
            {
                "scope": effective_scope,
                "custom_path": req.custom_path.strip(),
                "discovered": [d.get("game_dir") for d in discovered],
            },
            sort_keys=True,
        )
    )
    append_audit_log(
        action="game_manager.scan_custom_path" if req.custom_path.strip() else "game_manager.scan_platforms",
        status="warning" if missing_games else "ok",
        summary=summary,
        sha256=scan_hash,
        details={
            "discovery_scope": effective_scope,
            "custom_path": req.custom_path.strip() or None,
            "scanned_roots": scanned_roots,
            "discovered_count": len(discovered),
            "added_count": len(added_games),
            "missing_games": missing_games,
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "scanned_roots": scanned_roots,
        "discovered_count": len(discovered),
        "added_count": len(added_games),
        "missing_count": len(missing_games),
        "missing_games": missing_games,
        "discovered": discovered,
        "games": [enrich_game_status(g, pc_specs_fn=scrape_pc_specs) for g in store["games"]],
        "active_game_id": store.get("active_game_id", "battleground"),
    }


@router.post("/api/games/browse")
async def browse_for_folder(req: BrowseFolderRequest) -> dict[str, Any]:
    """Open a native Windows folder picker dialog and return the selected directory path."""
    def _pick_folder() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        init_dir = req.initial_dir.strip() if req.initial_dir.strip() and Path(req.initial_dir.strip()).is_dir() else str(Path.home())
        selected = filedialog.askdirectory(initialdir=init_dir, title=req.title)
        root.destroy()
        return selected or ""

    chosen = await asyncio.to_thread(_pick_folder)
    if not chosen:
        return {"status": "cancelled", "path": ""}
    norm_path = str(Path(chosen).resolve())
    append_audit_log(
        action="game_manager.browse_folder",
        status="ok",
        summary=f"Selected folder via native browser: {norm_path}",
        sha256=sha256_text(norm_path.lower()),
        details={"path": norm_path, "title": req.title},
    )
    return {"status": "ok", "path": norm_path}


@router.post("/api/games/unreal-settings")
async def import_unreal_game_settings(req: ImportUnrealSettingsRequest) -> dict[str, Any]:
    """Import or refresh Unreal Engine GameUserSettings.ini / Engine.ini settings for a game."""
    store = load_managed_games()
    game_id = req.game_id.strip() or store.get("active_game_id", "battleground")
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Managed game '{game_id}' not found.")

    gdir = Path(str(target.get("game_dir", "")).strip())
    info = target.get("cached_modding_info") or inspect_game_modding_info(
        gdir, str(target.get("name", "")), pc_specs_fn=scrape_pc_specs
    )
    tier_id = req.reference_tier_id.strip() or "auto"
    ue_settings = await asyncio.to_thread(
        inspect_unreal_game_settings,
        gdir,
        info,
        req.custom_ini_path.strip(),
        tier_id,
        pc_specs_fn=scrape_pc_specs,
    )
    info["is_unreal_engine"] = True
    info["unreal_settings"] = ue_settings
    target["cached_modding_info"] = info
    target["metadata_scraped"] = True
    save_managed_games(store)

    ini_label = ue_settings.get("ini_path") or f"Project '{ue_settings.get('detected_project_name')}' (Default Baseline)"
    tl_sum = ue_settings.get("traffic_light_summary", {})
    ue_hash = sha256_text(json.dumps(ue_settings, sort_keys=True, default=str))
    summary = (
        f"Imported Unreal settings for '{target.get('name')}' [{ini_label}] vs "
        f"{ue_settings['active_reference_tier']['tier_name']} -> "
        f"{tl_sum.get('overall_light', 'green').upper()} ({tl_sum.get('green_count', 0)} Optimal / {tl_sum.get('amber_count', 0)} Sub-Optimal / {tl_sum.get('red_count', 0)} Bottleneck)"
    )
    append_audit_log(
        action="unreal_settings.import",
        status="ok" if tl_sum.get("overall_light") == "green" else ("error" if tl_sum.get("overall_light") == "red" else "warning"),
        summary=summary,
        sha256=ue_hash,
        details={
            "game_id": game_id,
            "ini_path": ue_settings.get("ini_path"),
            "reference_tier_id": tier_id,
            "traffic_light_summary": tl_sum,
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "unreal_settings": ue_settings,
        "game": enrich_game_status(target, pc_specs_fn=scrape_pc_specs),
    }


@router.get("/api/games/{game_id}/icon")
async def get_game_icon(game_id: str):
    """Extract and cache a 32x32 PNG icon from the game's primary executable (or .ico)."""
    cached_png = await extract_game_icon_png(game_id)
    if cached_png is not None:
        return FileResponse(cached_png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)


@router.get("/api/games/{game_id}/steam-assets")
async def get_game_steam_assets(game_id: str) -> dict[str, Any]:
    """Return available local Steam or game-folder artwork URLs."""
    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if not target:
        return {"status": "ok", "appid": None, "has_assets": False}

    game_dir = Path(target.get("game_dir", ""))
    appid, assets = _find_steam_appid_and_assets(game_dir)
    primary_exe = _find_primary_game_exe(game_dir, str(target.get("name", "")))
    has_any = bool(assets) or (primary_exe is not None)

    base = f"/api/games/{game_id}/steam-asset"
    return {
        "status": "ok",
        "appid": appid,
        "has_assets": has_any,
        "hero_url": f"{base}/hero" if "hero" in assets else None,
        "logo_url": f"{base}/logo" if "logo" in assets else None,
        "header_url": f"{base}/header" if "header" in assets else None,
        "capsule_url": f"{base}/capsule" if "capsule" in assets else None,
        "exe_icon_url": f"/api/games/{game_id}/icon" if primary_exe else None,
        "primary_exe": primary_exe.name if primary_exe else None,
    }


@router.get("/api/games/{game_id}/steam-asset/{asset_kind}")
async def serve_game_steam_asset(game_id: str, asset_kind: str):
    """Serve a local Steam librarycache artwork image (hero, logo, header, capsule)."""
    if asset_kind not in ("hero", "logo", "header", "capsule"):
        return Response(status_code=404)
    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if not target:
        return Response(status_code=404)

    _, assets = _find_steam_appid_and_assets(Path(target.get("game_dir", "")))
    asset_path = assets.get(asset_kind)
    if not asset_path or not asset_path.is_file():
        return Response(status_code=404)

    media_type = "image/png" if asset_path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(asset_path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@router.post("/api/unreal-settings/apply")
async def apply_unreal_ini_settings(req: ApplyUnrealSettingsRequest) -> dict[str, Any]:
    """Back up the target Unreal GameUserSettings.ini and write updated settings."""
    ini_file = Path(req.ini_path.strip())
    if not ini_file.is_file():
        raise HTTPException(status_code=404, detail=f"Unreal INI file not found: {ini_file}")

    slug = make_slug(req.game_id)
    store = load_managed_games()
    target = next(
        (g for g in store.get("games", []) if g.get("id") == req.game_id or make_slug(str(g.get("id", ""))) == slug),
        None,
    )
    snap_gdir = Path(str(target.get("game_dir", "")).strip()) if target else ini_file.parent
    if not snap_gdir.is_dir():
        snap_gdir = ini_file.parent
    pre_snapshot_id = ""
    try:
        snap = create_game_snapshot(
            slug,
            snap_gdir,
            label=f"Pre-INI edit ({ini_file.name})",
            engine="unreal",
            extra_ini_paths=[ini_file],
        )
        pre_snapshot_id = snap.get("snapshot_id", "")
    except ValueError:
        pre_snapshot_id = ""

    bak_path = apply_unreal_ini_updates(ini_file, slug, req.updates)
    summary = f"Applied {len(req.updates)} Unreal INI setting(s) to {ini_file.name} (backup: {bak_path.name})"
    append_audit_log(
        action="unreal_settings.apply",
        status="ok",
        summary=summary,
        details={**req.updates, "snapshot_id": pre_snapshot_id},
    )
    return {
        "status": "ok",
        "summary": summary,
        "backup_path": str(bak_path),
        "ini_path": str(ini_file),
        "snapshot_id": pre_snapshot_id,
    }


def _find_managed_game_for_slug(slug: str) -> dict[str, Any] | None:
    store = load_managed_games()
    target_slug = make_slug(slug)
    return next(
        (
            g for g in store.get("games", [])
            if g.get("id") == slug or make_slug(str(g.get("id", ""))) == target_slug
        ),
        None,
    )


@router.get("/api/games/{slug}/snapshots")
async def get_game_snapshots(slug: str) -> dict[str, Any]:
    """List per-game snapshots captured for *slug*."""
    try:
        snaps = list_game_snapshots(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "slug": make_slug(slug), "snapshots": snaps, "count": len(snaps)}


@router.post("/api/games/{slug}/snapshots")
async def post_create_game_snapshot(slug: str, req: CreateSnapshotRequest | None = None) -> dict[str, Any]:
    """Create a manual lightweight modded-surface snapshot for *slug*."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    if not gdir.is_dir():
        raise HTTPException(status_code=400, detail=f"Game directory does not exist: {gdir}")

    info = target.get("cached_modding_info") if isinstance(target.get("cached_modding_info"), dict) else {}
    ue_ini = (info.get("unreal_settings") or {}).get("ini_path") if isinstance(info, dict) else ""
    extra_inis = [ue_ini] if ue_ini else None
    engine_hint = str(info.get("mcp_category") or target.get("runtime") or "")
    label = req.label if req is not None else ""
    try:
        res = create_game_snapshot(
            slug,
            gdir,
            label=label,
            engine=engine_hint,
            extra_ini_paths=extra_inis,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return res


@router.post("/api/games/{slug}/snapshots/{snapshot_id}/restore")
async def post_restore_game_snapshot(slug: str, snapshot_id: str) -> dict[str, Any]:
    """Restore modded game surfaces to a specific saved snapshot."""
    target = _find_managed_game_for_slug(slug)
    gdir = Path(str(target.get("game_dir", "")).strip()) if target else None
    try:
        res = restore_game_snapshot(
            slug,
            snapshot_id,
            target_game_dir=gdir if (gdir and gdir.is_dir()) else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return res


@router.get("/api/games/{slug}/conflict-guard")
async def get_game_conflict_guard(slug: str) -> dict[str, Any]:
    """Inspect a managed game directory for Vortex/MO2 markers, proxy DLLs, and ~mods .pak overrides."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    return inspect_conflict_guard(str(target.get("game_dir", "")).strip())


@router.post("/api/unreal-settings/restore")
async def restore_unreal_ini_settings(req: ApplyUnrealSettingsRequest) -> dict[str, Any]:
    """Restore the backed-up Unreal GameUserSettings.ini."""
    ini_file = Path(req.ini_path.strip())
    try:
        bak_path = restore_unreal_ini_backup(ini_file, make_slug(req.game_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    summary = f"Restored {ini_file.name} from backup {bak_path.name}"
    append_audit_log(action="unreal_settings.restore", status="ok", summary=summary)
    return {"status": "ok", "summary": summary}


@router.get("/api/games/{game_id}/conflicts")
async def check_game_mod_conflicts(game_id: str) -> dict[str, Any]:
    return scan_game_conflicts(game_id)


@router.post("/api/games/{game_id}/launch")
async def launch_game_process(game_id: str) -> dict[str, Any]:
    """Launch the game via Steam URI (if Steam AppID known) or its primary executable."""
    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Game '{game_id}' not found.")

    gdir = Path(str(target.get("game_dir", "")).strip())
    appid, _ = _find_steam_appid_and_assets(gdir)
    if appid:
        os.startfile(f"steam://rungameid/{appid}")
        summary = f"Launched '{target.get('name')}' via Steam (steam://rungameid/{appid})"
        append_audit_log(action="game.launch", status="ok", summary=summary)
        return {"status": "ok", "method": "steam", "summary": summary}

    exe = _find_primary_game_exe(gdir, str(target.get("name", "")))
    if not exe or not exe.is_file():
        raise HTTPException(status_code=404, detail=f"No launchable executable found in '{gdir}'.")

    subprocess.Popen([str(exe)], cwd=str(exe.parent))
    summary = f"Launched '{target.get('name')}' directly ({exe.name})"
    append_audit_log(action="game.launch", status="ok", summary=summary)
    return {"status": "ok", "method": "exe", "summary": summary, "exe": str(exe)}


@router.post("/api/games/{game_id}/notes")
async def save_game_notes(game_id: str, req: GameNotesRequest) -> dict[str, Any]:
    """Save per-game favorite flag, custom tags, and notes."""
    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Game '{game_id}' not found.")

    if req.favorite is not None:
        target["favorite"] = bool(req.favorite)
    if req.tags is not None:
        target["tags"] = [t.strip() for t in req.tags if t.strip()]
    if req.user_notes is not None:
        target["user_notes"] = req.user_notes

    save_managed_games(store)
    return {"status": "ok", "game": enrich_game_status(target, pc_specs_fn=scrape_pc_specs)}


@router.post("/api/games/prewarm-icons")
async def prewarm_game_icons() -> dict[str, Any]:
    return await prewarm_game_icons_batch()


@router.post("/api/games/prune-non-games")
async def prune_non_games() -> dict[str, Any]:
    return prune_non_games_from_store()


@router.get("/api/library")
async def get_library_entries() -> dict[str, Any]:
    cache = load_library_cache()
    return {
        "status": "ok",
        "entries": cache.get("entries", []),
        "custom_roots": cache.get("custom_roots", []),
        "last_scan_at": cache.get("last_scan_at"),
        "last_import_at": cache.get("last_import_at"),
        "cache_path": str(LIBRARY_CACHE_PATH),
    }


@router.post("/api/library/scan-installed")
async def scan_library_installed(req: LibraryScanRequest) -> dict[str, Any]:
    cache = load_library_cache()
    custom_roots = list(cache.get("custom_roots", []))
    for r in req.custom_roots:
        r_clean = str(r or "").strip()
        if r_clean and r_clean not in custom_roots:
            custom_roots.append(r_clean)
    if req.single_folder.strip() and req.save_custom_roots:
        sf = req.single_folder.strip()
        if sf not in custom_roots:
            custom_roots.append(sf)

    raw_installed: list[dict[str, Any]] = []
    raw_installed.extend(scan_steam_installed())
    raw_installed.extend(scan_epic_installed())
    raw_installed.extend(scan_gog_installed())
    raw_installed.extend(scan_manual_folders(custom_roots, single_folder=req.single_folder))

    deduped_installed: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    installed_titles: set[str] = set()
    for item in raw_installed:
        rp = str(item.get("root_path") or "").strip()
        if not rp:
            continue
        norm_rp = os.path.normcase(os.path.normpath(rp))
        if norm_rp in seen_paths:
            continue
        seen_paths.add(norm_rp)
        fp = fingerprint_library_entry(Path(rp))
        item.update(fp)
        deduped_installed.append(item)
        installed_titles.add(_normalize_library_title(item.get("title", "")))

    kept_uninstalled = [
        e for e in cache.get("entries", [])
        if e.get("install_state") == "owned_uninstalled"
        and _normalize_library_title(e.get("title", "")) not in installed_titles
    ]

    all_entries = deduped_installed + kept_uninstalled
    cache["entries"] = all_entries
    if req.save_custom_roots:
        cache["custom_roots"] = custom_roots
    cache["last_scan_at"] = datetime.now(timezone.utc).isoformat()
    save_library_cache(cache)

    append_audit_log(
        action="library.scan_installed",
        status="ok",
        summary=f"Scanned installed games: {len(deduped_installed)} installed entry(s) discovered",
        details={"installed_count": len(deduped_installed), "custom_roots": custom_roots},
    )
    return {
        "status": "ok",
        "installed_count": len(deduped_installed),
        "total_count": len(all_entries),
        "entries": all_entries,
        "custom_roots": custom_roots,
    }


@router.post("/api/library/import-owned")
async def import_library_owned(req: LibraryImportOwnedRequest) -> dict[str, Any]:
    cache = load_library_cache()
    existing = cache.get("entries", [])
    installed_entries = [e for e in existing if e.get("install_state") == "installed"]
    installed_titles = {_normalize_library_title(e.get("title", "")) for e in installed_entries}
    installed_steam_ids = {str((e.get("external_ids") or {}).get("steam_appid") or "") for e in installed_entries} - {""}

    imported: list[dict[str, Any]] = []
    imported.extend(_fetch_steam_owned_titles(req.steam_id, req.steam_api_key))
    if req.include_gog:
        imported.extend(_fetch_gog_owned_titles())

    deduped_owned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in imported:
        norm_t = _normalize_library_title(item.get("title", ""))
        sid = str((item.get("external_ids") or {}).get("steam_appid") or "")
        if norm_t in installed_titles or (sid and sid in installed_steam_ids):
            continue
        if item["id"] in seen_ids:
            continue
        seen_ids.add(item["id"])
        item.setdefault("engine", "Unknown")
        item.setdefault("detection_confidence", "estimated_metadata")
        item.setdefault("anti_cheat_status", "Clean")
        item.setdefault("recommended_adapter", "unknown")
        deduped_owned.append(item)

    if req.enrich_metadata and deduped_owned:
        enrich_uninstalled_with_pcgw(deduped_owned, cache.setdefault("pcgw_cache", {}))

    by_id = {e["id"]: e for e in existing if e.get("install_state") == "owned_uninstalled"}
    for item in deduped_owned:
        by_id[item["id"]] = item

    all_entries = installed_entries + list(by_id.values())
    cache["entries"] = all_entries
    cache["last_import_at"] = datetime.now(timezone.utc).isoformat()
    save_library_cache(cache)

    append_audit_log(
        action="library.import_owned",
        status="ok",
        summary=f"Imported {len(deduped_owned)} owned/uninstalled title(s)",
        details={"imported_count": len(deduped_owned)},
    )
    return {
        "status": "ok",
        "imported_count": len(deduped_owned),
        "total_count": len(all_entries),
        "entries": all_entries,
    }


@router.post("/api/library/import-file")
async def import_library_file(req: LibraryImportFileRequest) -> dict[str, Any]:
    raw_rows: list[dict[str, Any]] = list(req.entries or [])
    if req.content.strip():
        txt = req.content.strip()
        if txt.startswith("[") or txt.startswith("{"):
            parsed = json.loads(txt)
            if isinstance(parsed, list):
                raw_rows.extend([r for r in parsed if isinstance(r, dict)])
            elif isinstance(parsed, dict) and isinstance(parsed.get("entries"), list):
                raw_rows.extend([r for r in parsed["entries"] if isinstance(r, dict)])
        else:
            reader = csv.DictReader(io.StringIO(txt))
            for row in reader:
                raw_rows.append(dict(row))

    cache = load_library_cache()
    existing = cache.get("entries", [])
    installed_entries = [e for e in existing if e.get("install_state") == "installed"]
    installed_titles = {_normalize_library_title(e.get("title", "")) for e in installed_entries}

    new_entries: list[dict[str, Any]] = []
    for row in raw_rows:
        title = str(row.get("title") or row.get("name") or "").strip()
        if not title or _normalize_library_title(title) in installed_titles:
            continue
        plat_raw = str(row.get("platform") or "import").strip().lower()
        plat = plat_raw if plat_raw in ("steam", "epic", "gog", "manual", "import") else "import"
        ext_id = str(row.get("external_id") or "").strip()
        ext_ids = {f"{plat}_id": ext_id} if ext_id else {}
        if plat == "steam" and ext_id:
            ext_ids = {"steam_appid": ext_id}
        new_entries.append({
            "id": _canonical_entry_id(plat, ext_id, "", title),
            "title": title,
            "source_platform": plat,
            "external_ids": ext_ids,
            "install_state": "owned_uninstalled",
            "root_path": None,
            "engine": "Unknown",
            "detection_confidence": "estimated_metadata",
            "anti_cheat_status": "Clean",
            "recommended_adapter": "unknown",
        })

    if req.enrich_metadata and new_entries:
        enrich_uninstalled_with_pcgw(new_entries, cache.setdefault("pcgw_cache", {}))

    by_id = {e["id"]: e for e in existing if e.get("install_state") == "owned_uninstalled"}
    for item in new_entries:
        by_id[item["id"]] = item

    all_entries = installed_entries + list(by_id.values())
    cache["entries"] = all_entries
    cache["last_import_at"] = datetime.now(timezone.utc).isoformat()
    save_library_cache(cache)

    append_audit_log(
        action="library.import_file",
        status="ok",
        summary=f"Imported {len(new_entries)} title(s) from JSON/CSV",
        details={"imported_count": len(new_entries)},
    )
    return {
        "status": "ok",
        "imported_count": len(new_entries),
        "total_count": len(all_entries),
        "entries": all_entries,
    }

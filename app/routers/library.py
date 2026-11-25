"""Thin HTTP routes for managed games, discovery scans, Unreal INI settings, assets, and unified library."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.core.audit import append_audit_log, read_audit_logs
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
    WORKSPACE_DIR,
    _load_workspace_module,
    get_app_data_root,
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
from app.services.endpoint_applier import (
    apply_endpoint_change,
    get_deployed_mod_download_path,
    validate_endpoint_state,
)
from app.services import endpoints as _endpoints_svc
from app.services.endpoint_catalog import (
    categorize_endpoints_in_catalog,
    export_portable_endpoint_profile,
    import_community_artifact,
)
from app.services import bridge_diff as _bridge_diff_svc
from app.services import mod_auditor as _mod_auditor_svc
from app.services.endpoints import apply_or_reset_endpoint, scan_game_endpoints
from app.services import il2cpp_indexer as _il2cpp_svc
from app.services.game_snapshots import (
    create_game_snapshot,
    list_game_snapshots,
    restore_game_snapshot,
)
from app.services.installed_mods import (
    remove_installed_mod,
    scan_game_installed_mods,
    toggle_installed_mod,
)
from app.services.jobs import start_background_job
from app.services.log_tail import build_ai_modding_context, read_game_log_tail
from app.services.pc_optimizer import apply_pc_optimisation, evaluate_pc_optimisation
from app.services.tool_installer import build_setup_verification_checklist
from app.services.unreal_ini import (
    apply_unreal_ini_tweaks,
    list_unreal_ini_tweaks,
    preview_unreal_ini_tweaks,
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


class UnrealIniTweaksRequest(BaseModel):
    tweak_states: dict[str, bool] = Field(default_factory=dict)
    updates: dict[str, str] = Field(default_factory=dict)
    engine_ini_path: str = Field(default="")
    gus_ini_path: str = Field(default="")
    label: str = Field(default="Pre-INI tweak edit")


class VerifyIntegrityRequest(BaseModel):
    launch: bool = Field(default=True)


class ModToggleRequest(BaseModel):
    rel_path: str
    enabled: bool | None = None


class BridgeDiffPromoteItem(BaseModel):
    diff_id: str = Field(default="")
    category: str = Field(default="")
    label: str = Field(default="")
    object_id: str = Field(default="")
    type_name: str = Field(default="")
    field_path: str = Field(default="")
    display_name: str = Field(default="")
    before_value: str | None = None
    after_value: str | None = None
    value_type: str = Field(default="")
    collection_key: str | None = None
    collection_key_label: str = Field(default="")


class BridgeDiffPromoteRequest(BaseModel):
    items: list[BridgeDiffPromoteItem] = Field(default_factory=list)
    diff_id: str = Field(default="")
    category: str = Field(default="")
    label: str = Field(default="")


class ModRemoveRequest(BaseModel):
    rel_path: str


class PcOptimiseApplyRequest(BaseModel):
    mode: str = Field(default="optimal")
    setting_key: str = Field(default="")
    custom_ini_path: str = Field(default="")
    reference_tier_id: str = Field(default="auto")


class EndpointScanRequest(BaseModel):
    reset_defaults: bool = Field(default=False)


class EndpointUpdateRequest(BaseModel):
    endpoint_id: str
    value: str | None = None
    reset_to_default: bool = Field(default=False)


class EndpointApplyRequest(BaseModel):
    endpoint_id: str
    new_value: str | None = None
    action: str = Field(default="apply")
    force: bool = Field(default=False)


class EndpointValidateRequest(BaseModel):
    endpoint_id: str
    expected_value: str | None = None


class EndpointImportCommunityRequest(BaseModel):
    file_path: str = ""
    content: str = ""
    filename: str = ""


class EndpointCategorizeItem(BaseModel):
    endpoint_id: str
    category: str
    simple_description: str | None = None
    simple_label: str | None = None
    symbol: str | None = None


class EndpointCategorizeRequest(BaseModel):
    endpoint_id: str = ""
    category: str = ""
    simple_description: str | None = None
    simple_label: str | None = None
    symbol: str | None = None
    updates: list[EndpointCategorizeItem] = Field(default_factory=list)


class LibraryScanRequest(BaseModel):
    custom_roots: list[str] = Field(default_factory=list)
    single_folder: str = ""
    save_custom_roots: bool = True
    async_job: bool = False


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


@router.get("/api/games/{slug}/unreal-ini/tweaks")
async def get_game_unreal_ini_tweaks(slug: str, gus_ini_path: str = "", engine_ini_path: str = "") -> dict[str, Any]:
    """List structured Unreal Engine INI tweak presets and current status for *slug*."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    return list_unreal_ini_tweaks(
        gdir,
        engine_ini_path=Path(engine_ini_path) if engine_ini_path.strip() else None,
        gus_ini_path=Path(gus_ini_path) if gus_ini_path.strip() else None,
        game_name=str(target.get("name") or ""),
    )


@router.post("/api/games/{slug}/unreal-ini/preview")
async def post_preview_unreal_ini_tweaks(slug: str, req: UnrealIniTweaksRequest) -> dict[str, Any]:
    """Return unified diff preview for requested Unreal Engine INI tweaks."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    return preview_unreal_ini_tweaks(
        gdir,
        tweak_states=req.tweak_states,
        updates=req.updates,
        engine_ini_path=Path(req.engine_ini_path) if req.engine_ini_path.strip() else None,
        gus_ini_path=Path(req.gus_ini_path) if req.gus_ini_path.strip() else None,
        game_name=str(target.get("name") or ""),
    )


@router.post("/api/games/{slug}/unreal-ini/apply")
async def post_apply_unreal_ini_tweaks(slug: str, req: UnrealIniTweaksRequest) -> dict[str, Any]:
    """Snapshot existing Unreal INI configs and apply fenced tweaks / scalability updates."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    return apply_unreal_ini_tweaks(
        slug,
        gdir,
        tweak_states=req.tweak_states,
        updates=req.updates,
        engine_ini_path=Path(req.engine_ini_path) if req.engine_ini_path.strip() else None,
        gus_ini_path=Path(req.gus_ini_path) if req.gus_ini_path.strip() else None,
        game_name=str(target.get("name") or ""),
        label=req.label or "Pre-INI tweak edit",
    )


@router.get("/api/games/{slug}/ai-context")
async def get_game_ai_context(slug: str) -> dict[str, Any]:
    """Build a structured Markdown AI modding context pack for *slug*."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    return build_ai_modding_context(target)


def _launch_platform_uri(uri: str) -> bool:
    if sys.platform == "win32":
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    return False


@router.post("/api/games/{slug}/verify-integrity")
async def post_verify_game_integrity(slug: str, req: VerifyIntegrityRequest | None = None) -> dict[str, Any]:
    """Trigger Steam file validation (`steam://validate/{app_id}`) or return platform verification instructions."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    game_name = str(target.get("name") or slug)

    appid = str(target.get("steam_appid") or "").strip()
    if not appid and gdir.is_dir():
        found_id, _ = _find_steam_appid_and_assets(gdir)
        appid = str(found_id or "").strip()

    source_low = str(target.get("source") or target.get("platform_label") or "").lower()
    if appid.isdigit():
        validate_uri = f"steam://validate/{appid}"
        should_launch = True if req is None else bool(req.launch)
        launched = _launch_platform_uri(validate_uri) if should_launch else False
        summary = f"Steam integrity validation prepared for AppID {appid} ({validate_uri})"
        append_audit_log(
            action="VERIFY_INTEGRITY",
            status="ok",
            summary=summary,
            details={"slug": slug, "platform": "steam", "app_id": appid, "validate_uri": validate_uri, "launched": launched},
        )
        return {
            "status": "ok",
            "platform": "steam",
            "app_id": appid,
            "validate_uri": validate_uri,
            "launched": launched,
            "summary": summary,
            "instructions": [
                f"URI: {validate_uri}",
                "Steam will verify all local game files against its depot manifests.",
            ],
        }

    if "epic" in source_low or (gdir.is_dir() and (gdir / ".egstore").is_dir()):
        platform = "epic"
        instructions = [
            "Open the Epic Games Launcher -> Library.",
            f"Click the '...' menu on '{game_name}' -> Manage -> Verify.",
        ]
    elif "gog" in source_low or (gdir.is_dir() and list(gdir.glob("goggame-*.info"))):
        platform = "gog"
        instructions = [
            "Open GOG GALAXY -> Owned Games.",
            f"Select '{game_name}' -> Settings/More -> Manage Installation -> Verify / Repair.",
        ]
    else:
        platform = "custom"
        instructions = [
            f"Manual/Custom install detected at '{gdir}'.",
            "Use your storefront/archive checksum or click Full Restore (Rollback All) to revert modded files to your earliest clean baseline snapshot.",
        ]

    summary = f"Provided {platform} integrity verification steps for '{game_name}'"
    append_audit_log(
        action="VERIFY_INTEGRITY",
        status="ok",
        summary=summary,
        details={"slug": slug, "platform": platform, "game_dir": str(gdir)},
    )
    return {
        "status": "ok",
        "platform": platform,
        "app_id": "",
        "validate_uri": "",
        "launched": False,
        "summary": summary,
        "instructions": instructions,
    }


@router.post("/api/games/{slug}/full-restore")
async def post_full_restore_earliest_snapshot(slug: str) -> dict[str, Any]:
    """Restore a managed game to its earliest saved clean baseline snapshot."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    try:
        snaps = list_game_snapshots(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not snaps:
        raise HTTPException(
            status_code=400,
            detail=f"No clean baseline snapshot found for '{slug}'. Create a snapshot first before Full Restore.",
        )

    earliest = sorted(snaps, key=lambda s: (str(s.get("created_at") or ""), str(s.get("snapshot_id") or "")))[0]
    gdir = Path(str(target.get("game_dir", "")).strip())
    try:
        res = restore_game_snapshot(
            slug,
            str(earliest["snapshot_id"]),
            target_game_dir=gdir if gdir.is_dir() else None,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_audit_log(
        action="game.full_restore",
        status="ok",
        summary=f"Full Restore reverted '{target.get('name', slug)}' to earliest baseline snapshot '{earliest['snapshot_id']}'",
        details={"slug": slug, "snapshot_id": earliest["snapshot_id"]},
    )
    res["baseline_snapshot"] = earliest
    return res


@router.post("/api/games/{slug}/ai-pack")
async def post_download_ai_info_pack(slug: str) -> Response:
    """Generate and return a downloadable .zip AI Info Pack for uploading to an LLM."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    safe_slug = make_slug(slug) or "game"
    gdir = Path(str(target.get("game_dir", "")).strip())

    ctx = build_ai_modding_context(target)
    mods_report = scan_game_installed_mods(gdir, slug=safe_slug)
    snapshots = list_game_snapshots(safe_slug)
    info = target.get("cached_modding_info") if isinstance(target.get("cached_modding_info"), dict) else {}
    proj_name = str(info.get("detected_project_name") or "")
    tail_all = read_game_log_tail(gdir, lines=200, errors_only=False, detected_project_name=proj_name)
    tail_errs = read_game_log_tail(gdir, lines=200, errors_only=True, detected_project_name=proj_name)
    setup_status = build_setup_verification_checklist(
        gdir, str(target.get("name") or safe_slug), slug=safe_slug, info=info or None, snapshots=snapshots
    )

    ver_info: dict[str, Any] = {}
    try:
        vm = _load_workspace_module("version_manager", "version_manager.py")
        ver_info = vm.load_version_data()
    except Exception:
        ver_info = {"version": "unknown"}

    app_info = {
        "app_name": "The Modding Tool",
        "version": ver_info.get("version"),
        "channel": ver_info.get("channel"),
        "workspace_dir": str(WORKSPACE_DIR),
        "app_data_dir": str(get_app_data_root()),
        "game": {
            "id": target.get("id"),
            "slug": safe_slug,
            "name": target.get("name"),
            "game_dir": str(gdir),
            "runtime": target.get("runtime"),
            "detected_engine": info.get("mcp_category"),
        },
        "setup_checklist": setup_status,
    }

    recent_audit = read_audit_logs(limit=40, category="all")
    vault_app_root = (
        _mod_auditor_svc.APP_DATA_ROOT
        if _mod_auditor_svc.APP_DATA_ROOT != get_app_data_root()
        else _endpoints_svc.APP_DATA_ROOT
    )
    stored_audits = _mod_auditor_svc.list_stored_mod_audits(safe_slug, app_data_root=vault_app_root)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("game_summary.md", ctx.get("markdown", ""))
        zf.writestr("app_info.json", json.dumps(app_info, indent=2, default=str))
        zf.writestr("installed_mods.json", json.dumps(mods_report, indent=2, default=str))
        zf.writestr("snapshots.json", json.dumps(snapshots, indent=2, default=str))
        zf.writestr("logs/game_log_tail.log", tail_all.get("text") or "(No game log lines available.)")
        zf.writestr("logs/game_log_errors.log", tail_errs.get("text") or "(No matching error lines found.)")
        zf.writestr("logs/audit_recent.json", json.dumps(recent_audit, indent=2, default=str))
        if stored_audits:
            zf.writestr("stored_mod_audits.json", json.dumps(stored_audits, indent=2, default=str))
            blueprints_md = "\n\n---\n\n".join(
                str(rep.get("blueprint_markdown") or "").strip()
                for rep in stored_audits
                if rep.get("blueprint_markdown")
            )
            zf.writestr("mod_blueprints.md", blueprints_md or "# Stored Reference Mod Blueprints\n")

    append_audit_log(
        action="game.ai_pack",
        status="ok",
        summary=f"Created downloadable AI Info Pack archive ({safe_slug}_ai_info_pack.zip)",
        details={"slug": safe_slug, "mods_count": mods_report.get("total_count", 0), "snapshots_count": len(snapshots)},
    )
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_slug}_ai_info_pack.zip"'},
    )


@router.get("/api/games/{slug}/setup-status")
async def get_game_setup_status(slug: str) -> dict[str, Any]:
    """Return guided step-by-step setup readiness checks for *slug*."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    info = target.get("cached_modding_info") if isinstance(target.get("cached_modding_info"), dict) else None
    return build_setup_verification_checklist(gdir, str(target.get("name") or slug), slug=slug, info=info)


@router.get("/api/games/{slug}/mods")
async def get_game_installed_mods(slug: str) -> dict[str, Any]:
    """Scan and list installed Unity, Unreal, and Vortex/MO2 mods for *slug*."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    primary_exe = _find_primary_game_exe(gdir, str(target.get("name") or "")) if gdir.is_dir() else None
    clean_slug = make_slug(str(target.get("id") or slug))
    res = scan_game_installed_mods(gdir, primary_exe=primary_exe, slug=clean_slug)
    res["slug"] = make_slug(slug)
    return res


@router.post("/api/games/{slug}/mods/toggle")
async def post_toggle_game_mod(slug: str, req: ModToggleRequest) -> dict[str, Any]:
    """Enable or disable a detected mod inside *slug* using `.disabled` or `enabled.txt`."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    try:
        return toggle_installed_mod(gdir, req.rel_path, enabled=req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/games/{slug}/mods/remove")
async def post_remove_game_mod(slug: str, req: ModRemoveRequest) -> dict[str, Any]:
    """Create a pre-removal snapshot and safely remove a detected mod file/folder."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    gdir = Path(str(target.get("game_dir", "")).strip())
    try:
        return remove_installed_mod(slug, gdir, req.rel_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/games/{slug}/pc-optimisation")
async def get_game_pc_optimisation(
    slug: str,
    force_refresh: bool = False,
    reference_tier_id: str = "auto",
    custom_ini_path: str = "",
) -> dict[str, Any]:
    """Evaluate game graphics/runtime settings against live PC specs with traffic-light ratings."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    return evaluate_pc_optimisation(
        target,
        force_specs_refresh=force_refresh,
        pc_specs_fn=scrape_pc_specs,
        custom_ini_path=custom_ini_path,
        reference_tier_id=reference_tier_id,
    )


@router.post("/api/games/{slug}/pc-optimisation/apply")
async def post_apply_game_pc_optimisation(slug: str, req: PcOptimiseApplyRequest) -> dict[str, Any]:
    """Apply optimal or default PC optimisation settings after auto-creating a snapshot."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    return apply_pc_optimisation(
        slug,
        target,
        mode=req.mode,
        setting_key=req.setting_key,
        custom_ini_path=req.custom_ini_path,
        reference_tier_id=req.reference_tier_id,
    )


@router.post("/api/games/{slug}/endpoints/scan")
async def post_scan_game_endpoints(slug: str, req: EndpointScanRequest | None = None) -> dict[str, Any]:
    """Discover and categorize modding endpoints across INI/CFG configs, bridge, dumps, and error logs."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    reset_flag = bool(req.reset_defaults) if req is not None else False
    res = scan_game_endpoints(target, reset_defaults=reset_flag)
    res["bridge_connected"] = bool(
        any(e.get("delivery_type") == "Bridge" and e.get("category") != "error_logs" for e in res.get("endpoints", []))
        or _bridge_diff_svc.is_live_bridge_connected(target)
    )
    return res


@router.get("/api/games/{slug}/endpoints")
async def get_game_endpoints(slug: str, category: str = "all") -> dict[str, Any]:
    """Return categorized modding endpoints with persisted baseline default values for *slug*."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    res = scan_game_endpoints(target, reset_defaults=False)
    res["bridge_connected"] = bool(
        any(e.get("delivery_type") == "Bridge" and e.get("category") != "error_logs" for e in res.get("endpoints", []))
        or _bridge_diff_svc.is_live_bridge_connected(target)
    )
    clean_cat = (category or "all").strip().lower()
    if clean_cat and clean_cat != "all":
        res["endpoints"] = [e for e in res.get("endpoints", []) if e.get("category") == clean_cat]
        res["filtered_count"] = len(res["endpoints"])
    else:
        res["filtered_count"] = len(res.get("endpoints", []))
    return res


@router.post("/api/games/{slug}/endpoints/update")
async def post_update_game_endpoint(slug: str, req: EndpointUpdateRequest) -> dict[str, Any]:
    """Update or reset a single discovered Config endpoint back to its stored baseline default."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    try:
        return apply_or_reset_endpoint(
            slug,
            target,
            req.endpoint_id,
            value=req.value,
            reset_to_default=req.reset_to_default,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/games/{slug}/endpoints/apply")
async def post_apply_game_endpoint(slug: str, req: EndpointApplyRequest) -> dict[str, Any]:
    """Contextually apply or rollback an endpoint via Config, Mod File, or live Bridge delivery."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    try:
        return await apply_endpoint_change(
            target,
            req.endpoint_id,
            new_value=req.new_value,
            action=req.action,
            force=req.force,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/games/{slug}/endpoints/validate")
async def post_validate_game_endpoint(slug: str, req: EndpointValidateRequest) -> dict[str, Any]:
    """Validate persistence, mod deployment, or live bridge state for *endpoint_id*."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    try:
        return await validate_endpoint_state(
            target,
            req.endpoint_id,
            expected_value=req.expected_value,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/games/{slug}/endpoints/download")
async def get_download_endpoint_mod(slug: str, endpoint_id: str) -> FileResponse:
    """Download the generated .zip archive for a deployed Mod File endpoint."""
    zp = get_deployed_mod_download_path(slug, endpoint_id)
    if zp is None or not zp.is_file():
        raise HTTPException(status_code=404, detail=f"No generated mod package found for endpoint '{endpoint_id}'.")
    return FileResponse(zp, media_type="application/zip", filename=zp.name)


@router.post("/api/games/{slug}/endpoints/import-community")
async def post_import_community_endpoints(slug: str, request: Request) -> dict[str, Any]:
    """Import a community `.ct`, `.cfg`, `.lua`, `.ini`, or `.json` schema/profile into the game's catalog."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")

    ct_header = (request.headers.get("content-type") or "").lower()
    file_path = ""
    content = ""
    filename = ""
    if "multipart/form-data" in ct_header:
        form = await request.form()
        file_path = str(form.get("file_path") or "")
        up_file = form.get("file")
        if up_file is not None and hasattr(up_file, "read"):
            filename = str(getattr(up_file, "filename", "") or form.get("filename") or "")
            raw_bytes = await up_file.read()
            content = raw_bytes.decode("utf-8-sig", errors="ignore")
        else:
            filename = str(form.get("filename") or "")
            content = str(form.get("content") or "")
    else:
        try:
            raw_body = await request.json()
        except Exception:
            raw_body = {}
        req_model = EndpointImportCommunityRequest(**(raw_body if isinstance(raw_body, dict) else {}))
        file_path = req_model.file_path
        content = req_model.content
        filename = req_model.filename

    try:
        res = import_community_artifact(
            target,
            file_path=file_path,
            content=content,
            filename=filename,
            app_data_root=_endpoints_svc.APP_DATA_ROOT,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    report = scan_game_endpoints(target, reset_defaults=False)
    return {**res, "report": report}


@router.post("/api/games/{slug}/endpoints/categorize")
async def post_categorize_game_endpoints(slug: str, req: EndpointCategorizeRequest) -> dict[str, Any]:
    """Assign category/description overrides to single or multiple endpoints and persist to endpoint_knowledge.json."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")

    upd_items: list[dict[str, Any]] = [u.model_dump() for u in req.updates]
    if req.endpoint_id.strip():
        upd_items.append({
            "endpoint_id": req.endpoint_id,
            "category": req.category,
            "simple_description": req.simple_description,
            "simple_label": req.simple_label,
            "symbol": req.symbol,
        })
    if not upd_items:
        raise HTTPException(status_code=400, detail="Provide 'endpoint_id' + 'category' or non-empty 'updates'.")

    current_report = scan_game_endpoints(target, reset_defaults=False)
    try:
        res = categorize_endpoints_in_catalog(
            target,
            upd_items,
            current_report.get("endpoints", []),
            app_data_root=_endpoints_svc.APP_DATA_ROOT,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated_report = scan_game_endpoints(target, reset_defaults=False)
    return {**res, "report": updated_report}


@router.get("/api/games/{slug}/endpoints/export-profile")
async def get_export_endpoint_profile(slug: str) -> JSONResponse:
    """Export a sanitized, path-free portable JSON endpoint profile keyed by AppID/slug."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    report = scan_game_endpoints(target, reset_defaults=False)
    profile = export_portable_endpoint_profile(
        target,
        report,
        save_to_profiles_dir=True,
        app_data_root=_endpoints_svc.APP_DATA_ROOT,
    )
    fname = f"{make_slug(str(profile.get('app_id') or slug))}_endpoint_profile.json"
    return JSONResponse(
        content=profile,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/api/games/{slug}/il2cpp/dump")
async def post_run_il2cpp_dump(slug: str) -> dict[str, Any]:
    """Run headless Il2CppDumper for a Unity IL2CPP game and build `il2cpp_index.json`."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    try:
        res = _il2cpp_svc.run_il2cpp_dumper(
            target,
            tools_root=_il2cpp_svc.TOOLS_ROOT,
            app_data_root=_endpoints_svc.APP_DATA_ROOT,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "status": "ok",
        "indexed": True,
        "slug": res["slug"],
        "dump_dir": res["dump_dir"],
        "dump_cs_path": res["dump_cs_path"],
        "script_json_path": res["script_json_path"],
        "index_path": res["index_path"],
        "counts": res["counts"],
    }


@router.get("/api/games/{slug}/il2cpp/index")
async def get_game_il2cpp_index(slug: str) -> dict[str, Any]:
    """Return the structured IL2CPP symbol/enum/static-root/method index for *slug*."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    safe_slug = make_slug(str(target.get("id") or slug)) or "game"
    idx = _il2cpp_svc.load_il2cpp_index(safe_slug, app_data_root=_endpoints_svc.APP_DATA_ROOT)
    if not idx:
        return {
            "status": "not_indexed",
            "indexed": False,
            "slug": safe_slug,
            "counts": {"enums": 0, "static_roots": 0, "classes": 0, "patchable_methods": 0},
        }
    return {
        "status": "ok",
        "indexed": True,
        **idx,
    }


@router.post("/api/games/{slug}/bridge-diff/baseline")
async def post_bridge_diff_baseline(slug: str) -> dict[str, Any]:
    """Capture a baseline live-bridge memory snapshot before performing an in-game action."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    try:
        return _bridge_diff_svc.capture_bridge_baseline(target, slug, app_data_root=_endpoints_svc.APP_DATA_ROOT)
    except _bridge_diff_svc.BridgeOfflineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/games/{slug}/bridge-diff/compare")
async def post_bridge_diff_compare(slug: str) -> dict[str, Any]:
    """Capture a second live-bridge pass and return filtered, ranked changed fields."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    try:
        return _bridge_diff_svc.compare_bridge_snapshot(target, slug, app_data_root=_endpoints_svc.APP_DATA_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _bridge_diff_svc.BridgeOfflineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/games/{slug}/bridge-diff/promote")
async def post_bridge_diff_promote(slug: str, req: BridgeDiffPromoteRequest) -> dict[str, Any]:
    """Promote selected Action Diff candidates into the categorized endpoints catalog & knowledge cache."""
    target = _find_managed_game_for_slug(slug)
    if not target:
        raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")
    items = [item.model_dump() for item in req.items]
    if req.diff_id.strip():
        items.append({"diff_id": req.diff_id, "category": req.category, "label": req.label})
    try:
        return _bridge_diff_svc.promote_bridge_diffs(target, slug, items, app_data_root=_endpoints_svc.APP_DATA_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
async def prewarm_game_icons(async_job: bool = False) -> dict[str, Any]:
    if not async_job:
        return await prewarm_game_icons_batch()

    def _worker(report: Any) -> dict[str, Any]:
        report(percent=15, stage="Checking cached game icons...")
        res = asyncio.run(prewarm_game_icons_batch())
        extracted = int(res.get("extracted", 0))
        report(percent=95, stage=f"Pre-warmed {extracted} icon(s)...", log_line=res.get("summary", ""))
        return res

    job = start_background_job("prewarm_icons", _worker, initial_stage="Queueing icon pre-warm...")
    return {"status": "accepted", "job_id": job["job_id"], "job": job}


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


def _execute_library_scan(req: LibraryScanRequest, report: Any = None) -> dict[str, Any]:
    def _emit(pct: int, stage: str, log_line: str | None = None) -> None:
        if report is not None:
            report(percent=pct, stage=stage, log_line=log_line)

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
    _emit(10, "Scanning Steam libraries...")
    steam_items = scan_steam_installed()
    raw_installed.extend(steam_items)
    _emit(30, f"Scanning Epic Games ({len(steam_items)} Steam found)...", f"Steam: {len(steam_items)}")
    epic_items = scan_epic_installed()
    raw_installed.extend(epic_items)
    _emit(45, f"Scanning GOG Galaxy ({len(epic_items)} Epic found)...", f"Epic: {len(epic_items)}")
    gog_items = scan_gog_installed()
    raw_installed.extend(gog_items)
    _emit(60, f"Scanning custom folders ({len(gog_items)} GOG found)...", f"GOG: {len(gog_items)}")
    manual_items = scan_manual_folders(custom_roots, single_folder=req.single_folder)
    raw_installed.extend(manual_items)

    deduped_installed: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    installed_titles: set[str] = set()
    total_raw = max(len(raw_installed), 1)
    for idx, item in enumerate(raw_installed, start=1):
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
        if idx == 1 or idx % 5 == 0 or idx == total_raw:
            pct = 60 + int((idx / total_raw) * 32)
            _emit(pct, f"Fingerprinting games ({idx}/{total_raw})...")

    kept_uninstalled = [
        e for e in cache.get("entries", [])
        if e.get("install_state") == "owned_uninstalled"
        and _normalize_library_title(e.get("title", "")) not in installed_titles
    ]

    _emit(95, "Saving unified library cache...")
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


@router.post("/api/library/scan-installed")
async def scan_library_installed(req: LibraryScanRequest) -> dict[str, Any]:
    if not req.async_job:
        return _execute_library_scan(req)

    job = start_background_job(
        "library_scan",
        lambda report: _execute_library_scan(req, report=report),
        initial_stage="Starting multi-drive library scan...",
    )
    return {"status": "accepted", "job_id": job["job_id"], "job": job}


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

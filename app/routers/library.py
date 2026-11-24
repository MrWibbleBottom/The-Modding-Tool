"""Managed games, Steam VDF/ACF discovery, CSV reference lookup, Unreal INI settings, and unified library."""

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


from app.routers.system import (
    UNREAL_HARDWARE_REFERENCE_ARRAY,
    _format_ue_quality,
    evaluate_scalability_traffic_light,
    match_pc_to_unreal_reference_tier,
    scrape_pc_specs,
)

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


STEAM_REF_CSV = POC_DIR / "steam_games_reference.csv"
_STEAM_REF_CACHE: list[dict[str, str]] | None = None


def load_steam_reference_sheet() -> list[dict[str, str]]:
    global _STEAM_REF_CACHE
    if _STEAM_REF_CACHE is not None:
        return _STEAM_REF_CACHE
    import csv

    rows: list[dict[str, str]] = []
    if STEAM_REF_CSV.is_file():
        with STEAM_REF_CSV.open("r", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                rows.append(row)
    _STEAM_REF_CACHE = rows
    return rows


def lookup_sheet_reference(product_name: str, folder_name: str) -> dict[str, str] | None:
    import re

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    rows = load_steam_reference_sheet()
    targets = [norm(product_name), norm(folder_name)]
    targets = [t for t in targets if t]

    # 1. Exact normalized match
    for t in targets:
        for r in rows:
            title_norm = norm(r.get("Game Title", ""))
            if title_norm == t:
                return r

    # 2. Substring / franchise match (e.g. Battlemarked -> Demeo, Disco Elysium The Final Cut -> Disco Elysium)
    for t in targets:
        for r in rows:
            title_norm = norm(r.get("Game Title", ""))
            if not title_norm or len(title_norm) < 4:
                continue
            if title_norm in t or t in title_norm:
                return r

    if any("battlemarked" in t or "demeo" in t for t in targets):
        for r in rows:
            if "demeo" in r.get("Game Title", "").lower():
                return r

    return None


def _clean_nexus_package_name(raw_name: str) -> str:
    """Normalizes Vortex/Nexus subfolder names like 'AAA-Better Inventory Capacity-467-1-1-1674452260' into a readable mod name."""
    import re
    name = raw_name.strip()
    if name.startswith("AAA-"):
        name = name[4:]
    # Strip trailing Nexus modId-version-timestamp suffix (e.g. -467-1-1-1674452260 or .zip-204-1-01-1624317233)
    cleaned = re.sub(r"(?:\.zip|\.7z|\.rar)?-\d+(?:-\d+){1,4}-\d{8,}$", "", name, flags=re.IGNORECASE)
    return cleaned.strip(" -_") or raw_name


def scan_installed_mods(game_dir: Path, primary_exe: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Scans engine- and mod-manager-specific folders (~mods, LogicMods, UE4SS, BepInEx, MelonLoader, ReShade, Mods/) for installed mods."""
    mods: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    folders_scanned: list[str] = []
    managed_by: list[str] = []

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(game_dir)).replace("\\", "/")
        except Exception:
            return str(p).replace("\\", "/")

    def _add_mod(
        *,
        name: str,
        file_name: str,
        category: str,
        path_obj: Path,
        enabled: bool = True,
        package: str = "",
        size_kb: float | None = None,
        group: str = "game_mod",
    ) -> None:
        rel_path = _rel(path_obj)
        key = rel_path.lower()
        if key in seen_paths:
            return
        seen_paths.add(key)
        if size_kb is None and path_obj.is_file():
            try:
                size_kb = round(path_obj.stat().st_size / 1024, 1)
            except Exception:
                size_kb = 0.0
        mods.append({
            "name": name,
            "file_name": file_name,
            "category": category,
            "group": group,
            "path": rel_path,
            "enabled": enabled,
            "package": package,
            "size_kb": size_kb or 0.0,
        })

    # 1. Unreal Engine Pak Mods (~mods, LogicMods, Paks/Mods, *_P.pak)
    for paks_dir in game_dir.glob("*/Content/Paks"):
        if not paks_dir.is_dir():
            continue
        for sub_name, cat_label in (("~mods", "Unreal Pak (~mods)"), ("LogicMods", "Unreal LogicMod"), ("Mods", "Unreal Pak (Mods)")):
            sub_dir = paks_dir / sub_name
            if not sub_dir.is_dir():
                continue
            folders_scanned.append(_rel(sub_dir))
            if (sub_dir / "vortex.deployment.json").is_file() and "Vortex" not in managed_by:
                managed_by.append("Vortex")
            pak_stems: set[tuple[Path, str]] = set()
            all_files = sorted(
                [p for ext in ("*.pak", "*.ucas", "*.utoc") for p in sub_dir.rglob(ext) if p.is_file()],
                key=lambda x: (x.suffix.lower() != ".pak", str(x).lower()),
            )
            for mf in all_files:
                stem_key = (mf.parent, mf.stem.lower())
                if mf.suffix.lower() in (".ucas", ".utoc") and stem_key in pak_stems:
                    continue
                if mf.suffix.lower() == ".pak":
                    pak_stems.add(stem_key)
                pkg_raw = mf.parent.name if mf.parent != sub_dir else ""
                pkg_clean = _clean_nexus_package_name(pkg_raw) if pkg_raw else ""
                _add_mod(
                    name=pkg_clean or mf.stem,
                    file_name=mf.name,
                    category=cat_label,
                    path_obj=mf,
                    enabled=True,
                    package=pkg_raw,
                )
        # Custom *_P.pak directly in Paks/
        for patch_pak in sorted(paks_dir.glob("*_P.pak")):
            if patch_pak.is_file():
                _add_mod(
                    name=patch_pak.stem,
                    file_name=patch_pak.name,
                    category="Unreal Patch Pak",
                    path_obj=patch_pak,
                    enabled=True,
                )

    # 2. Unreal UE4SS / Lua / Native Mods
    for ue_mods_glob in ("*/Binaries/Win64/Mods", "*/Binaries/Win64/ue4ss/Mods"):
        for ue_mods_dir in game_dir.glob(ue_mods_glob):
            if not ue_mods_dir.is_dir():
                continue
            folders_scanned.append(_rel(ue_mods_dir))
            for mod_sub in sorted(ue_mods_dir.iterdir()):
                if mod_sub.is_dir() and mod_sub.name.lower() != "shared":
                    enabled = (mod_sub / "enabled.txt").is_file()
                    _add_mod(
                        name=mod_sub.name,
                        file_name=mod_sub.name,
                        category="UE4SS Mod",
                        path_obj=mod_sub,
                        enabled=enabled,
                        size_kb=0.0,
                    )

    # 3. Unity Mods (BepInEx/plugins, MelonLoader/Mods)
    bep_plugins = game_dir / "BepInEx" / "plugins"
    if bep_plugins.is_dir():
        folders_scanned.append(_rel(bep_plugins))
        for dll in sorted(bep_plugins.rglob("*.dll")):
            if dll.is_file():
                _add_mod(
                    name=dll.stem,
                    file_name=dll.name,
                    category="BepInEx Plugin",
                    path_obj=dll,
                    enabled=True,
                )
    melon_mods = game_dir / "MelonLoader" / "Mods"
    if melon_mods.is_dir():
        folders_scanned.append(_rel(melon_mods))
        for dll in sorted(melon_mods.glob("*.dll")):
            if dll.is_file():
                _add_mod(
                    name=dll.stem,
                    file_name=dll.name,
                    category="MelonLoader Mod",
                    path_obj=dll,
                    enabled=True,
                )

    # 4. Graphics, Upscaler & Overlay Mods (ReShade, Addons, OptiScaler, DLSS Enabler, Special K, DXVK)
    rs_dirs = [game_dir]
    if primary_exe and primary_exe.parent.is_dir() and primary_exe.parent not in rs_dirs:
        rs_dirs.append(primary_exe.parent)
    for sub in ("*/Binaries/Win64", "Game", "Retail", "Windows"):
        for cand in game_dir.glob(sub):
            if cand.is_dir() and cand not in rs_dirs:
                rs_dirs.append(cand)
    for rdir in rs_dirs:
        rel_rdir = _rel(rdir)
        has_injector = False
        # ReShade core
        rs_ini = rdir / "ReShade.ini"
        rs_log = rdir / "ReShade.log"
        if rs_ini.is_file() or rs_log.is_file():
            has_injector = True
            _add_mod(
                name="ReShade",
                file_name=rs_ini.name if rs_ini.is_file() else rs_log.name,
                category="ReShade",
                path_obj=rs_ini if rs_ini.is_file() else rs_log,
                enabled=True,
                group="injector_overlay",
            )
        # ReShade addons (*.addon64 / *.addon32 / *.addon)
        for ext in ("*.addon64", "*.addon32", "*.addon"):
            for addon in sorted(rdir.glob(ext)):
                if addon.is_file():
                    has_injector = True
                    _add_mod(
                        name=addon.stem,
                        file_name=addon.name,
                        category="ReShade Addon",
                        path_obj=addon,
                        enabled=True,
                        group="injector_overlay",
                    )
        # OptiScaler
        opti_ini = rdir / "OptiScaler.ini"
        opti_log = rdir / "OptiScaler.log"
        if opti_ini.is_file() or opti_log.is_file():
            has_injector = True
            _add_mod(
                name="OptiScaler",
                file_name=opti_ini.name if opti_ini.is_file() else opti_log.name,
                category="Upscaler Mod",
                path_obj=opti_ini if opti_ini.is_file() else opti_log,
                enabled=True,
                group="injector_overlay",
            )
        # DLSS Enabler / FSR3 FrameGen
        dlss_en_ini = rdir / "dlss-enabler.ini"
        dlss_en_log = rdir / "dlss-enabler.log"
        if dlss_en_ini.is_file() or dlss_en_log.is_file():
            has_injector = True
            _add_mod(
                name="DLSS Enabler",
                file_name=dlss_en_ini.name if dlss_en_ini.is_file() else dlss_en_log.name,
                category="Upscaler Mod",
                path_obj=dlss_en_ini if dlss_en_ini.is_file() else dlss_en_log,
                enabled=True,
                group="injector_overlay",
            )
        fsr3_dll = rdir / "dlssg_to_fsr3_amd_is_better.dll"
        if fsr3_dll.is_file():
            has_injector = True
            _add_mod(
                name="dlssg-to-fsr3 (FrameGen)",
                file_name=fsr3_dll.name,
                category="FrameGen Mod",
                path_obj=fsr3_dll,
                enabled=True,
                group="injector_overlay",
            )
        # Game-specific DLSS ini (e.g. DaysGoneDLSS.ini)
        for dlss_ini in sorted(rdir.glob("*DLSS.ini")):
            if dlss_ini.is_file():
                has_injector = True
                _add_mod(
                    name=dlss_ini.stem,
                    file_name=dlss_ini.name,
                    category="Upscaler Mod",
                    path_obj=dlss_ini,
                    enabled=True,
                    group="injector_overlay",
                )
        # Special K / DXVK
        for tool_file, tool_name, tool_cat in (
            ("SpecialK.ini", "Special K", "Graphics Overlay"),
            ("dxvk.conf", "DXVK", "Graphics Wrapper"),
        ):
            tf = rdir / tool_file
            if tf.is_file():
                has_injector = True
                _add_mod(
                    name=tool_name,
                    file_name=tf.name,
                    category=tool_cat,
                    path_obj=tf,
                    enabled=True,
                    group="injector_overlay",
                )
        if has_injector and rel_rdir not in folders_scanned:
            folders_scanned.append(rel_rdir)

    # 5. Generic top-level Mods / Plugins / Addons folders
    for top_name in ("Mods", "mods", "Plugins", "plugins", "Addons", "addons"):
        top_dir = game_dir / top_name
        if not top_dir.is_dir():
            continue
        rel_top = _rel(top_dir)
        if rel_top not in folders_scanned:
            folders_scanned.append(rel_top)
        for item in sorted(top_dir.iterdir()):
            if item.name.startswith(".") or item.name.lower() in ("shared", "__pycache__"):
                continue
            if item.is_dir():
                _add_mod(
                    name=item.name,
                    file_name=item.name,
                    category=f"Folder ({top_name})",
                    path_obj=item,
                    enabled=True,
                    size_kb=0.0,
                )
            elif item.is_file() and item.suffix.lower() in (".pak", ".dll", ".asi", ".jar", ".zip", ".lua", ".esp", ".esm", ".esl"):
                _add_mod(
                    name=item.stem,
                    file_name=item.name,
                    category=f"File ({top_name})",
                    path_obj=item,
                    enabled=True,
                )

    by_category: dict[str, int] = {}
    game_mods_count = 0
    injector_mods_count = 0
    for m in mods:
        cat = m["category"]
        by_category[cat] = by_category.get(cat, 0) + 1
        if m.get("group") == "injector_overlay":
            injector_mods_count += 1
        else:
            game_mods_count += 1

    summary = {
        "total": len(mods),
        "game_mods_count": game_mods_count,
        "injector_mods_count": injector_mods_count,
        "folders_scanned": folders_scanned,
        "by_category": by_category,
        "managed_by": managed_by,
    }
    return mods[:500], summary


def inspect_game_modding_info(game_dir: Path, display_name: str = "") -> dict[str, Any]:
    """Determines platform, Unity build version, scripting backend, metadata version, and required MCP discovery & modding tools from disk + reference sheet."""
    import re
    import struct

    raw_folder_name = game_dir.parent.name if game_dir.name.lower() == "content" and game_dir.parent.name else game_dir.name
    info: dict[str, Any] = {
        "dir_exists": game_dir.is_dir(),
        "developer": "Unknown",
        "product_name": raw_folder_name,
        "executable": "Not found",
        "platform": "Unknown",
        "architecture": "Unknown",
        "graphics_api": "Standard",
        "xr_sdk": "None",
        "unity_version": "Unknown",
        "build_guid": "Unknown",
        "scripting_backend": "Unknown",
        "metadata_version": None,
        "scripting_assemblies_count": 0,
        "interop_assemblies_count": 0,
        "assembly_hash": None,
        "doorstop_installed": False,
        "doorstop_version": None,
        "bepinex_installed": False,
        "bepinex_version": None,
        "mcp_bridge_installed": False,
        "required_modding_tool": "Unknown",
        "required_bridge_plugin": "Unknown",
        "modding_readiness": "Missing Folder",
        "mcp_category": "Unknown",
        "sheet_engine_tech": "Unknown",
        "recommended_mcp_stack": "Unknown",
        "discovery_mcp_tools": "Unknown",
        "modding_mcp_tools": "Unknown",
        "sheet_reference_match": "Auto-detected from Game Files",
    }
    if not game_dir.is_dir():
        return info

    # Check anti-cheat binaries
    detected_anticheat: list[str] = []
    try:
        for root, _dirs, files in os.walk(game_dir):
            for fname in files:
                if fname.lower() in ANTI_CHEAT_BINARIES:
                    detected_anticheat.append(fname)
    except Exception:
        pass

    # 1. Executable & PE Architecture
    primary_exe = _find_primary_game_exe(game_dir, display_name or raw_folder_name)
    exes = [primary_exe] if primary_exe else [
        p
        for p in game_dir.glob("*.exe")
        if "unitycrashhandler" not in p.name.lower()
    ]
    target_pe = exes[0] if exes else (game_dir / "GameAssembly.dll")
    if exes and exes[0]:
        info["executable"] = exes[0].name
        info["executable_rel"] = exes[0].relative_to(game_dir).as_posix() if exes[0].is_relative_to(game_dir) else exes[0].name
        if "steam" in str(game_dir).lower():
            info["platform_store"] = "Steam"
        elif "gog" in str(game_dir).lower():
            info["platform_store"] = "GOG Galaxy"
        elif "xboxgames" in str(game_dir).lower():
            info["platform_store"] = "Xbox / PC Game Pass"
        elif "epic" in str(game_dir).lower():
            info["platform_store"] = "Epic Games"
    if target_pe.is_file():
        try:
            header = target_pe.open("rb").read(1024)
            if header[:2] == b"MZ":
                pe_off = struct.unpack("<I", header[0x3C:0x40])[0]
                machine = struct.unpack("<H", header[pe_off + 4 : pe_off + 6])[0]
                if machine == 0x8664:
                    info["architecture"] = "x64 (64-bit)"
                    info["platform"] = f"Windows x64 ({info.get('platform_store', 'PC')})"
                elif machine == 0x014C:
                    info["architecture"] = "x86 (32-bit)"
                    info["platform"] = f"Windows x86 ({info.get('platform_store', 'PC')})"
                elif machine == 0xAA64:
                    info["architecture"] = "ARM64"
                    info["platform"] = f"Windows ARM64 ({info.get('platform_store', 'PC')})"
        except Exception:
            pass

    # Detect ReShade, addons (*.addon64 / *.addon32 / *.addon), and shaders
    reshade_dirs = [game_dir]
    if primary_exe and primary_exe.parent.is_dir() and primary_exe.parent not in reshade_dirs:
        reshade_dirs.append(primary_exe.parent)
    for sub in ("*/Binaries/Win64", "Game", "Retail", "Windows"):
        for cand in game_dir.glob(sub):
            if cand.is_dir() and cand not in reshade_dirs:
                reshade_dirs.append(cand)

    rs_info: dict[str, Any] = {
        "installed": False,
        "version": None,
        "bin_dir": None,
        "injector_dll": None,
        "addons": [],
        "shaders_count": 0,
        "ini_path": None,
    }
    for rdir in reshade_dirs:
        rs_ini = rdir / "ReShade.ini"
        rs_log = rdir / "ReShade.log"
        rs_shaders = rdir / "reshade-shaders"
        addons = sorted(
            [p.name for ext in ("*.addon64", "*.addon32", "*.addon") for p in rdir.glob(ext) if p.is_file()]
        )
        inj = next(
            (dll for dll in ("dxgi.dll", "d3d11.dll", "d3d12.dll", "d3d9.dll", "opengl32.dll", "ReShade64.dll", "ReShade32.dll") if (rdir / dll).is_file()),
            None,
        )
        if rs_ini.is_file() or rs_log.is_file() or rs_shaders.is_dir() or addons:
            rs_info["installed"] = True
            rs_info["bin_dir"] = str(rdir)
            rs_info["injector_dll"] = inj
            rs_info["addons"] = addons
            if rs_ini.is_file():
                rs_info["ini_path"] = str(rs_ini)
            if rs_shaders.is_dir():
                rs_info["shaders_count"] = len(list(rs_shaders.rglob("*.fx")))
            if rs_log.is_file():
                try:
                    log_head = rs_log.read_text(encoding="utf-8", errors="replace")[:2048]
                    vm = re.search(r"Initializing crosire's ReShade version\s+'([^']+)'", log_head, re.IGNORECASE)
                    if vm:
                        rs_info["version"] = vm.group(1).strip()
                except Exception:
                    pass
            break
    info["reshade"] = rs_info

    # Detect installed mods across relevant engine/mod-manager folders (~mods, LogicMods, UE4SS, BepInEx, MelonLoader, ReShade, generic Mods/)
    installed_mods, installed_mods_summary = scan_installed_mods(game_dir, primary_exe=primary_exe)
    info["installed_mods"] = installed_mods
    info["installed_mods_summary"] = installed_mods_summary

    # Detect Unreal Pak/IoStore count, UE4SS loader, and Lua mods
    pak_files = list(game_dir.glob("*/Content/Paks/*.pak")) + list(game_dir.glob("*/Content/Paks/*.utoc"))
    info["pak_count"] = len(pak_files)
    ue4ss_dll = next(game_dir.glob("*/Binaries/Win64/UE4SS.dll"), None) or next(game_dir.glob("*/Binaries/Win64/dwmapi.dll"), None)
    info["ue4ss_installed"] = bool(ue4ss_dll and ue4ss_dll.is_file())
    ue_mods = [m for m in game_dir.glob("*/Binaries/Win64/Mods/*") if m.is_dir() and m.name.lower() != "shared"]
    info["ue_mods_count"] = len(ue_mods)

    if (game_dir / "D3D12").is_dir():
        info["graphics_api"] = "Direct3D 12 (D3D12)"

    # 2. *_Data folder inspection
    data_dirs = [p for p in game_dir.glob("*_Data") if p.is_dir()]
    if data_dirs:
        ddir = data_dirs[0]
        app_info_file = ddir / "app.info"
        if app_info_file.is_file():
            lines = app_info_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) >= 1 and lines[0].strip():
                info["developer"] = lines[0].strip()
            if len(lines) >= 2 and lines[1].strip():
                info["product_name"] = lines[1].strip()

        boot_cfg = ddir / "boot.config"
        if boot_cfg.is_file():
            for line in boot_cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k == "build-guid":
                        info["build_guid"] = v
                    elif k == "xrsdk-pre-init-library":
                        info["xr_sdk"] = v

        # Extract Unity engine build version from data.unity3d / globalgamemanagers / mainData
        for asset_name in ("data.unity3d", "globalgamemanagers", "mainData"):
            asset_file = ddir / asset_name
            if asset_file.is_file():
                try:
                    chunk = asset_file.open("rb").read(256)
                    m = re.search(rb"(\d{4}\.\d+\.\d+[a-z]\d+)", chunk)
                    if m:
                        info["unity_version"] = m.group(1).decode("ascii")
                        break
                except Exception:
                    pass

        sa_file = ddir / "ScriptingAssemblies.json"
        if sa_file.is_file():
            try:
                sa_data = json.loads(sa_file.read_text(encoding="utf-8", errors="replace"))
                info["scripting_assemblies_count"] = len(sa_data.get("names", []))
            except Exception:
                pass

        gm_file = ddir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        if gm_file.is_file():
            try:
                gm_hdr = gm_file.open("rb").read(8)
                magic, ver = struct.unpack("<II", gm_hdr)
                if magic == 0xFAB11BAF:
                    info["metadata_version"] = ver
            except Exception:
                pass

    # 3. Scripting backend & required modding tools + Reference Sheet Lookup
    has_ga = (game_dir / "GameAssembly.dll").is_file()
    has_mono_managed = any(
        (d / "Managed" / "Assembly-CSharp.dll").is_file() for d in data_dirs
    )
    has_ue4_pak = any(game_dir.rglob("*.pak")) and any(
        (game_dir / "Engine").is_dir() or "shipping.exe" in p.name.lower() for p in game_dir.rglob("*.exe")
    )
    has_godot_pck = any(game_dir.glob("*.pck"))
    has_creation_esm = any(game_dir.rglob("*.esm"))

    sheet_row = lookup_sheet_reference(info["product_name"], display_name or game_dir.name)
    if sheet_row:
        info["sheet_reference_match"] = (
            f"Matched Sheet: '{sheet_row['Game Title']}' ({sheet_row['MCP Category']})"
        )
        info["mcp_category"] = sheet_row.get("MCP Category", "Unity")
        info["sheet_engine_tech"] = sheet_row.get("Engine / Tech", "Unity (Mono/IL2CPP)")
        info["recommended_mcp_stack"] = sheet_row.get(
            "Recommended MCP Stack", "bepinex-mcp + il2cpp-dump-analyzer + GhidraMCP"
        )

    if detected_anticheat or info["mcp_category"] == "Online / Anti-Cheat":
        info["mcp_category"] = "Online / Anti-Cheat"
        info["sheet_engine_tech"] = "Live-Service / Anti-Cheat"
        info["recommended_mcp_stack"] = "None (Server-Side / Anti-Cheat)"
        ac_label = ", ".join(detected_anticheat) if detected_anticheat else "Sheet Reference: Live-Service / Anti-Cheat"
        info["discovery_mcp_tools"] = f"Blocked — Anti-Cheat / Live-Service Protected ({ac_label})"
        info["modding_mcp_tools"] = "None (Server-Side / Anti-Cheat Protected)"
        info["required_modding_tool"] = "Blocked (Online / Anti-Cheat)"
        info["required_bridge_plugin"] = "None"
    elif has_ga:
        info["scripting_backend"] = (
            f"IL2CPP (Native C++ / Metadata v{info['metadata_version']})"
            if info["metadata_version"]
            else "IL2CPP (Native C++)"
        )
        info["required_modding_tool"] = "BepInEx 6 Unity.IL2CPP (win-x64) + Il2CppInterop"
        info["required_bridge_plugin"] = "BepInExMCP.IL2CPP.dll (.NET 6 + Roslyn + HarmonyX)"
        info["mcp_category"] = "Unity"
        info["sheet_engine_tech"] = "Unity (Mono/IL2CPP)"
        info["recommended_mcp_stack"] = "bepinex-mcp + il2cpp-dump-analyzer + GhidraMCP"
        info["discovery_mcp_tools"] = (
            "bepinex-mcp (search_gameobjects, find_objects_with_component, inspect_components, "
            "get_component_member, search_runtime_types, describe_runtime_type) + "
            "il2cpp-dump-analyzer / il2cpp-decompiler (global-metadata.dat) + GhidraMCP (GameAssembly.dll)"
        )
        info["modding_mcp_tools"] = (
            "bepinex-mcp (set_component_value, call_component_method, run_bridge_batch, "
            "mod_patch_method [prefix/postfix], watch_component_member, save_mod_profile, apply_mod_profile)"
        )
    elif has_mono_managed:
        info["scripting_backend"] = "Mono (.NET Managed Assemblies)"
        info["required_modding_tool"] = "BepInEx 5 (x64 / x86 Mono)"
        info["required_bridge_plugin"] = "BepInExMCP.dll (.NET Standard 2.0)"
        info["mcp_category"] = "Unity"
        info["sheet_engine_tech"] = "Unity (Mono/IL2CPP)"
        info["recommended_mcp_stack"] = "bepinex-mcp + gamecode-rag (+ il2cpp-dump-analyzer / GhidraMCP)"
        info["discovery_mcp_tools"] = (
            "bepinex-mcp (search_gameobjects, inspect_components, get_component_member, "
            "search_runtime_types, describe_runtime_type) + gamecode-rag (Assembly-CSharp.dll index)"
        )
        info["modding_mcp_tools"] = (
            "bepinex-mcp (set_component_value, call_component_method, mod_patch_method "
            "[prefix/postfix/transpiler/finalizer], mod_inject_class, save_mod_profile, apply_mod_profile)"
        )
    elif has_ue4_pak or info["mcp_category"] == "Unreal Engine 4/5":
        info["scripting_backend"] = "Unreal Engine 4/5 (C++ / Blueprints)"
        info["required_modding_tool"] = "UE4SS (Unreal Engine 4/5 Scripting System)"
        info["required_bridge_plugin"] = "ue4ss-bridge / UE4SS-mcp"
        info["mcp_category"] = "Unreal Engine 4/5"
        info["sheet_engine_tech"] = "Unreal Engine 4/5"
        info["recommended_mcp_stack"] = "ue4ss-bridge / UE4SS-mcp + Filesystem MCP"
        info["discovery_mcp_tools"] = "ue4ss-bridge / UE4SS-mcp (UObject / Blueprint reflection & dump) + Filesystem MCP"
        info["modding_mcp_tools"] = "ue4ss-bridge / UE4SS-mcp (Lua / Blueprint hooks & property edits) + Filesystem MCP (.pak)"
    elif has_creation_esm or info["mcp_category"] == "Creation Engine":
        info["scripting_backend"] = "Creation Engine / Papyrus"
        info["required_modding_tool"] = "Creation Kit + Script Extender (SKSE / F4SE)"
        info["required_bridge_plugin"] = "SkyrimCK-MCP / creation-kit-mcp"
        info["mcp_category"] = "Creation Engine"
        info["sheet_engine_tech"] = "Creation / Gamebryo"
        info["recommended_mcp_stack"] = "SkyrimCK-MCP / creation-kit-mcp + Filesystem MCP"
        info["discovery_mcp_tools"] = "SkyrimCK-MCP / creation-kit-mcp (ESM/ESP record & Papyrus inspection) + Filesystem MCP"
        info["modding_mcp_tools"] = "SkyrimCK-MCP / creation-kit-mcp (Plugin record & script patching) + Filesystem MCP"
    elif has_godot_pck or info["mcp_category"] == "Godot":
        info["scripting_backend"] = "Godot Engine (GDScript / C#)"
        info["required_modding_tool"] = "Godot-MCP / PCK Loader"
        info["required_bridge_plugin"] = "Godot-MCP"
        info["mcp_category"] = "Godot"
        info["sheet_engine_tech"] = "Godot Engine"
        info["recommended_mcp_stack"] = "Godot-MCP + Filesystem MCP"
        info["discovery_mcp_tools"] = "Godot-MCP (Scene tree & GDScript node inspection) + Filesystem MCP"
        info["modding_mcp_tools"] = "Godot-MCP (Live node property & script patching) + Filesystem MCP"
    elif info["mcp_category"] == "Data-Driven / Scriptable":
        info["scripting_backend"] = "Scriptable / Open Data (Lua / XML / JSON / SQL)"
        info["required_modding_tool"] = "Native Mod Loader / Data Override"
        info["required_bridge_plugin"] = "Filesystem MCP + Terminal MCP"
        info["discovery_mcp_tools"] = "Filesystem MCP + Terminal MCP (Config, Lua, XML, JSON & asset schema search)"
        info["modding_mcp_tools"] = "Filesystem MCP + Terminal MCP (Direct script/data file editing & CLI build tools)"
    else:
        info["scripting_backend"] = info["sheet_engine_tech"] if info["sheet_engine_tech"] != "Unknown" else "Custom / Native C++"
        info["required_modding_tool"] = "Native Disassembler / Memory Debugger"
        info["required_bridge_plugin"] = "GhidraMCP + Cheat-Engine-Mcp"
        info["mcp_category"] = info["mcp_category"] if info["mcp_category"] != "Unknown" else "Custom / Legacy C++"
        info["recommended_mcp_stack"] = (
            info["recommended_mcp_stack"]
            if info["recommended_mcp_stack"] != "Unknown"
            else "GhidraMCP + Cheat-Engine-Mcp (+ Filesystem MCP)"
        )
        info["discovery_mcp_tools"] = "GhidraMCP (Static binary decompilation) + Cheat-Engine-Mcp (Dynamic memory/pointer scanning)"
        info["modding_mcp_tools"] = "Cheat-Engine-Mcp (Live memory/AOB injection) + Filesystem MCP"

    # 4. Doorstop & BepInEx live installation status
    info["doorstop_installed"] = (game_dir / "winhttp.dll").is_file() and (
        game_dir / "doorstop_config.ini"
    ).is_file()
    dv_file = game_dir / ".doorstop_version"
    if dv_file.is_file():
        info["doorstop_version"] = dv_file.read_text(encoding="utf-8", errors="replace").strip()

    bep_dir = game_dir / "BepInEx"
    info["bepinex_installed"] = (bep_dir / "core").is_dir()
    log_file = bep_dir / "LogOutput.log"
    if log_file.is_file():
        try:
            first_line = log_file.open("r", encoding="utf-8", errors="replace").readline()
            m_bep = re.search(r"BepInEx\s+([^\s]+)", first_line)
            if m_bep:
                info["bepinex_version"] = m_bep.group(1)
        except Exception:
            pass

    interop_dir = bep_dir / "interop"
    if interop_dir.is_dir():
        info["interop_assemblies_count"] = len(list(interop_dir.glob("*.dll")))
        ah_file = interop_dir / "assembly-hash.txt"
        if ah_file.is_file():
            info["assembly_hash"] = ah_file.read_text(encoding="utf-8", errors="replace").strip()

    plugin_dir = bep_dir / "plugins" / "BepInExMCP.IL2CPP"
    plugin_dll = plugin_dir / "BepInExMCP.IL2CPP.dll"
    mono_plugin_dll = bep_dir / "plugins" / "BepInExMCP.dll"
    roslyn_ok = (plugin_dir / "Microsoft.CodeAnalysis.dll").is_file() and (
        plugin_dir / "Microsoft.CodeAnalysis.CSharp.dll"
    ).is_file()
    info["mcp_bridge_installed"] = plugin_dll.is_file() or mono_plugin_dll.is_file()
    info["roslyn_Compiler_installed"] = roslyn_ok
    info["manifest_tracked"] = manifest_path_for_game(game_dir).is_file()

    # Build crucial modding steps checklist for the Game Info Panel
    ac_clear = (len(detected_anticheat) == 0) and (info["mcp_category"] != "Online / Anti-Cheat")
    binaries_ok = has_ga or has_mono_managed or (info["executable"] != "Not found")
    mod_files_ok = info["doorstop_installed"] and info["bepinex_installed"]
    interop_ok = (info["interop_assemblies_count"] > 0 and bool(info["assembly_hash"])) if has_ga else info["bepinex_installed"]
    bridge_ok = (plugin_dll.is_file() and roslyn_ok) if has_ga else info["mcp_bridge_installed"]

    is_unreal = bool(
        has_ue4_pak
        or info.get("mcp_category") == "Unreal Engine 4/5"
        or "Unreal" in str(info.get("sheet_engine_tech", ""))
        or "Unreal" in str(info.get("scripting_backend", ""))
        or ((game_dir / "Engine" / "Config").is_dir() and not has_ga and not has_mono_managed)
    )
    info["is_unreal_engine"] = is_unreal

    if has_ga or has_mono_managed:
        info["crucial_steps"] = [
            {
                "step": "1. Anti-Cheat & Safety Check",
                "state": "ok" if ac_clear else "blocked",
                "label": "Clear (Safe to Mod)" if ac_clear else "Blocked (Anti-Cheat / Live-Service)",
                "detail": "Zero anti-cheat drivers detected" if ac_clear else info["discovery_mcp_tools"],
            },
            {
                "step": "2. Engine & Metadata Files",
                "state": "ok" if binaries_ok else "missing",
                "label": "Present" if binaries_ok else "Missing",
                "detail": (
                    f"GameAssembly.dll + global-metadata.dat (v{info['metadata_version']})"
                    if has_ga
                    else "Managed/Assembly-CSharp.dll"
                ),
            },
            {
                "step": "3. Mod Loader Files (Doorstop & BepInEx)",
                "state": "ok" if mod_files_ok else "missing",
                "label": (
                    f"Present (BepInEx {info['bepinex_version'] or '6.x'} + Doorstop {info['doorstop_version'] or 'OK'})"
                    if mod_files_ok
                    else "Not Present — Run Provision & Backup"
                ),
                "detail": "winhttp.dll + doorstop_config.ini + BepInEx/core",
            },
            {
                "step": "4. First-Run Bootstrap & Interop DLLs",
                "state": "ok" if interop_ok else "pending",
                "label": (
                    f"Generated ({info['interop_assemblies_count']} Interop DLLs)"
                    if (has_ga and interop_ok)
                    else ("Ready (Mono Runtime)" if (has_mono_managed and interop_ok) else "Pending — Launch Game Once After Provisioning")
                ),
                "detail": f"BepInEx/interop/assembly-hash.txt ({(info['assembly_hash'] or 'not generated')[:12]})",
            },
            {
                "step": "5. Live MCP Bridge Plugin In Place",
                "state": "ok" if bridge_ok else "missing",
                "label": (
                    "In Place (BepInExMCP.IL2CPP.dll + Roslyn Compiler)"
                    if (has_ga and bridge_ok)
                    else ("In Place (BepInExMCP.dll)" if bridge_ok else "Not In Place — Deploy via Provision & Backup")
                ),
                "detail": "BepInEx/plugins/BepInExMCP.IL2CPP/ (HTTP 127.0.0.1:8080/mcp)",
            },
            {
                "step": "6. Backup & Clean Restore Manifest",
                "state": "ok" if info["manifest_tracked"] else "pending",
                "label": "Tracked (Zero-Residual Restore Ready)" if info["manifest_tracked"] else "No Active Provision Manifest",
                "detail": "Tracks added & backed-up colliding files in %LOCALAPPDATA%\\ModToolPoC\\manifests",
            },
        ]
    elif is_unreal:
        ue4ss_ok = bool(info.get("ue4ss_installed"))
        ue_mods_cnt = int(info.get("ue_mods_count", 0))
        info["crucial_steps"] = [
            {
                "step": "1. Anti-Cheat & Safety Check",
                "state": "ok" if ac_clear else "blocked",
                "label": "Clear (Safe to Mod)" if ac_clear else "Blocked (Anti-Cheat / Live-Service)",
                "detail": "Zero anti-cheat drivers detected" if ac_clear else info["discovery_mcp_tools"],
            },
            {
                "step": "2. Unreal Engine & Content Archives",
                "state": "ok" if binaries_ok else "missing",
                "label": f"Unreal Engine 4/5 ({info.get('pak_count', 0)} .pak archive(s))",
                "detail": f"{info.get('executable_rel') or info.get('executable')} (Content/Paks)",
            },
            {
                "step": "3. Unreal Mod Loader (RE-UE4SS)",
                "state": "ok" if ue4ss_ok else "na",
                "label": "RE-UE4SS Installed" if ue4ss_ok else "Optional — Install RE-UE4SS for Lua Mods",
                "detail": "*/Binaries/Win64/UE4SS.dll + dwmapi.dll",
            },
            {
                "step": "4. Unreal Lua / Blueprint Mods",
                "state": "ok" if ue_mods_cnt > 0 else "na",
                "label": f"{ue_mods_cnt} Mod Folder(s) Active" if ue_mods_cnt > 0 else "No Lua Mods Installed Yet",
                "detail": "*/Binaries/Win64/Mods/ (Build via Build a Mod tab)",
            },
            {
                "step": "5. Unreal INI & Hardware Advisor",
                "state": "ok",
                "label": "GameUserSettings.ini Inspection Ready",
                "detail": "Compare & apply optimal scalability settings in Unreal section below",
            },
            {
                "step": "6. INI Backup & Restore",
                "state": "ok",
                "label": "Automatic Pre-Edit Backup Enabled",
                "detail": "Backups saved in %LOCALAPPDATA%\\ModToolPoC\\unreal_ini_backups",
            },
        ]
    else:
        info["crucial_steps"] = [
            {
                "step": "1. Anti-Cheat & Safety Check",
                "state": "ok" if ac_clear else "blocked",
                "label": "Clear (Safe to Mod)" if ac_clear else "Blocked (Anti-Cheat / Live-Service)",
                "detail": "Zero anti-cheat drivers detected" if ac_clear else info["discovery_mcp_tools"],
            },
            {
                "step": "2. Engine & Binary Files",
                "state": "ok" if binaries_ok else "missing",
                "label": f"{info.get('mcp_category') or 'PC'} Binary Detected",
                "detail": f"{info.get('executable_rel') or info.get('executable')}",
            },
            {
                "step": "3. Recommended Modding Loader",
                "state": "na",
                "label": info.get("required_modding_tool") or "External Tooling",
                "detail": info.get("recommended_mcp_stack") or "See Recommended Modding Apps below",
            },
            {
                "step": "4. Recommended MCP / Tooling",
                "state": "na",
                "label": info.get("required_bridge_plugin") or "External Tooling",
                "detail": info.get("modding_mcp_tools") or "Config / Data Overrides",
            },
        ]

    if not ac_clear:
        info["modding_readiness"] = "Blocked (Anti-Cheat)"
    elif info["mcp_bridge_installed"] and interop_ok:
        info["modding_readiness"] = "Ready"
    elif info["bepinex_installed"]:
        info["modding_readiness"] = "Partially Provisioned"
    elif has_ga or has_mono_managed:
        info["modding_readiness"] = "Unprovisioned"
    else:
        info["modding_readiness"] = "External Tooling"

    info["modding_apps"] = evaluate_modding_apps_for_game(game_dir, info)
    info["unreal_settings"] = (
        inspect_unreal_game_settings(game_dir, info) if is_unreal else None
    )
    return info


def evaluate_modding_apps_for_game(game_dir: Path, info: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns all modding apps/tools with live installation status and whether each is recommended for this game."""
    dir_exists = game_dir.is_dir()
    has_ga = (game_dir / "GameAssembly.dll").is_file() if dir_exists else ("IL2CPP" in str(info.get("scripting_backend", "")))
    backend = str(info.get("scripting_backend", ""))
    cat = str(info.get("mcp_category", ""))
    tech = str(info.get("sheet_engine_tech", ""))
    is_mono = ("Mono" in backend) and not has_ga
    is_unity = (cat == "Unity") or has_ga or is_mono
    is_ue4_5 = bool(info.get("is_unreal_engine")) or (cat == "Unreal Engine 4/5") or ("Unreal Engine 4/5" in tech)
    if cat == "Online / Anti-Cheat":
        is_unity = False
        is_ue4_5 = False
        is_ue3 = False
        is_creation = False
        is_godot = False
        is_re_engine = False
        is_fromsoft = False
        is_custom_cpp = False
    is_ue3 = ("Unreal Engine 3" in tech) or ("Unreal Engine 3" in backend)
    is_creation = (cat == "Creation Engine")
    is_godot = (cat == "Godot")
    is_re_engine = ("RE Engine" in tech) or ("RE Engine" in backend)
    is_fromsoft = ("FromSoftware" in tech) or ("FromSoftware" in backend)
    is_custom_cpp = (cat == "Custom / Legacy C++") or is_ue3 or is_fromsoft or is_re_engine

    bep_il2cpp_installed = bool(info.get("doorstop_installed") and info.get("bepinex_installed") and has_ga)
    bridge_il2cpp_installed = bool(info.get("mcp_bridge_installed") and info.get("roslyn_Compiler_installed"))
    uexp_installed = (
        bool(list((game_dir / "BepInEx" / "plugins").rglob("*UnityExplorer*.dll")))
        if dir_exists and (game_dir / "BepInEx" / "plugins").is_dir()
        else (TOOLS_ROOT / "UnityExplorer_IL2CPP").is_dir() and bool(list((TOOLS_ROOT / "UnityExplorer_IL2CPP").rglob("*.dll")))
    )
    bep_mono_installed = (
        bool(info.get("doorstop_installed") and info.get("bepinex_installed") and is_mono)
        or ((TOOLS_ROOT / "BepInEx5_Mono" / "winhttp.dll").is_file())
    )
    il2cpp_dumper_installed = (TOOLS_ROOT / "Il2CppDumper").is_dir() and bool(list((TOOLS_ROOT / "Il2CppDumper").rglob("*.exe")))
    dnspy_installed = (TOOLS_ROOT / "dnSpyEx").is_dir() and bool(list((TOOLS_ROOT / "dnSpyEx").rglob("dnSpy.exe")))
    ue4ss_installed = (
        ((TOOLS_ROOT / "UE4SS").is_dir() and bool(list((TOOLS_ROOT / "UE4SS").rglob("*.dll"))))
        or (dir_exists and bool(list(game_dir.rglob("UE4SS.dll"))))
    )
    fmodel_installed = (TOOLS_ROOT / "FModel").is_dir() and bool(list((TOOLS_ROOT / "FModel").rglob("FModel.exe")))
    ghidra_installed = (TOOLS_ROOT / "GhidraMCP").is_dir() and bool(list((TOOLS_ROOT / "GhidraMCP").glob("*")))
    x64dbg_installed = (TOOLS_ROOT / "x64dbg").is_dir() and bool(list((TOOLS_ROOT / "x64dbg").rglob("*.exe")))
    gdre_installed = (TOOLS_ROOT / "GDRETools").is_dir() and bool(list((TOOLS_ROOT / "GDRETools").rglob("*.exe")))
    modengine2_installed = (TOOLS_ROOT / "ModEngine2").is_dir() and bool(list((TOOLS_ROOT / "ModEngine2").rglob("*.exe")))
    reframework_installed = (TOOLS_ROOT / "REFramework").is_dir() and bool(list((TOOLS_ROOT / "REFramework").glob("*")))
    xedit_installed = (TOOLS_ROOT / "xEdit").is_dir() and bool(list((TOOLS_ROOT / "xEdit").glob("*")))

    apps = [
        {
            "id": "bepinex_il2cpp",
            "name": "BepInEx 6 (Unity.IL2CPP x64) + Doorstop",
            "category_badge": "Unity IL2CPP Mod Loader",
            "recommended": bool(has_ga),
            "installed": bep_il2cpp_installed,
            "install_target": str(game_dir) if dir_exists else str(TOOLS_ROOT / "BepInEx6_IL2CPP"),
            "status_text": "Installed in Game Folder" if bep_il2cpp_installed else "Not Installed in Game Folder",
            "button_label": "Re-Deploy BepInEx 6 IL2CPP" if bep_il2cpp_installed else "Install BepInEx 6 IL2CPP",
            "description": "Extracts Doorstop (winhttp.dll + doorstop_config.ini) & BepInEx 6 IL2CPP core into the active game folder.",
            "release_url": "https://builds.bepinex.dev/projects/bepinex_be",
        },
        {
            "id": "bepinex_mcp_il2cpp",
            "name": "BepInExMCP.IL2CPP Live Bridge Plugin (+ Roslyn)",
            "category_badge": "Live MCP Bridge (Unity IL2CPP)",
            "recommended": bool(has_ga),
            "installed": bridge_il2cpp_installed,
            "install_target": str(game_dir / "BepInEx" / "plugins" / "BepInExMCP.IL2CPP"),
            "status_text": "In Place (DLL + Roslyn)" if bridge_il2cpp_installed else "Not Deployed to Game",
            "button_label": "Re-Deploy MCP Bridge" if bridge_il2cpp_installed else "Deploy MCP Bridge",
            "description": "Deploys compiled BepInExMCP.IL2CPP.dll + Microsoft.CodeAnalysis.CSharp.dll into BepInEx/plugins/.",
            "release_url": "https://github.com/rkuhn153/bepinex-mcp",
        },
        {
            "id": "il2cpp_dumper",
            "name": "Il2CppDumper (Metadata & DummyDll Extractor)",
            "category_badge": "Unity IL2CPP Static Analysis",
            "recommended": bool(has_ga),
            "installed": il2cpp_dumper_installed,
            "install_target": str(TOOLS_ROOT / "Il2CppDumper"),
            "status_text": "Installed in Tools Folder" if il2cpp_dumper_installed else "Not Installed",
            "button_label": "Re-Install Il2CppDumper" if il2cpp_dumper_installed else "Install Il2CppDumper",
            "description": "Extracts C# types, methods, offsets, and DummyDlls from GameAssembly.dll + global-metadata.dat.",
            "release_url": "https://github.com/Perfare/Il2CppDumper/releases",
        },
        {
            "id": "unity_explorer_il2cpp",
            "name": "UnityExplorer (BepInEx 6 IL2CPP In-Game UI)",
            "category_badge": "Unity IL2CPP In-Game Inspector",
            "recommended": bool(has_ga),
            "installed": uexp_installed,
            "install_target": str(game_dir / "BepInEx" / "plugins") if (dir_exists and has_ga) else str(TOOLS_ROOT / "UnityExplorer_IL2CPP"),
            "status_text": "Installed in BepInEx/plugins" if uexp_installed else "Not Installed",
            "button_label": "Re-Install UnityExplorer" if uexp_installed else "Install UnityExplorer",
            "description": "In-game Scene Hierarchy, Object Inspector, C# REPL console, and FreeCam plugin for BepInEx 6 IL2CPP.",
            "release_url": "https://github.com/sinai-dev/UnityExplorer/releases",
        },
        {
            "id": "bepinex_mono",
            "name": "BepInEx 5 (Unity Mono x64) + Doorstop",
            "category_badge": "Unity Mono Mod Loader",
            "recommended": bool(is_mono),
            "installed": bep_mono_installed,
            "install_target": str(game_dir) if (dir_exists and is_mono) else str(TOOLS_ROOT / "BepInEx5_Mono"),
            "status_text": "Installed" if bep_mono_installed else "Not Installed",
            "button_label": "Re-Install BepInEx 5 Mono" if bep_mono_installed else "Install BepInEx 5 Mono",
            "description": "Official BepInEx 5.4.23.2 mod loader for Unity Mono games (Managed/Assembly-CSharp.dll).",
            "release_url": "https://github.com/BepInEx/BepInEx/releases",
        },
        {
            "id": "dnspy_ex",
            "name": "dnSpyEx (.NET & Unity Assembly Decompiler)",
            "category_badge": "Unity Mono / DummyDll Decompiler",
            "recommended": bool(is_unity),
            "installed": dnspy_installed,
            "install_target": str(TOOLS_ROOT / "dnSpyEx"),
            "status_text": "Installed in Tools Folder" if dnspy_installed else "Not Installed",
            "button_label": "Re-Install dnSpyEx" if dnspy_installed else "Install dnSpyEx",
            "description": "Decompiles, inspects, and edits .NET / Unity Mono Assembly-CSharp.dll and IL2CPP interop/DummyDlls.",
            "release_url": "https://github.com/dnSpyEx/dnSpy/releases",
        },
        {
            "id": "ghidra_mcp",
            "name": "GhidraMCP Bridge Extension (Static Native Decompiler)",
            "category_badge": "Native C++ & IL2CPP Reverse Engineering",
            "recommended": bool(has_ga or is_custom_cpp),
            "installed": ghidra_installed,
            "install_target": str(TOOLS_ROOT / "GhidraMCP"),
            "status_text": "Installed in Tools Folder" if ghidra_installed else "Not Installed",
            "button_label": "Re-Install GhidraMCP" if ghidra_installed else "Install GhidraMCP",
            "description": "Downloads the latest GhidraMCP bridge plugin for decompiling GameAssembly.dll and native C++ binaries.",
            "release_url": "https://github.com/LaurieWired/GhidraMCP/releases",
        },
        {
            "id": "ue4ss",
            "name": "RE-UE4SS (Unreal Engine 4/5 Scripting & Live Inspector)",
            "category_badge": "Unreal Engine 4/5 Mod Loader",
            "recommended": bool(is_ue4_5),
            "installed": ue4ss_installed,
            "install_target": str(TOOLS_ROOT / "UE4SS"),
            "status_text": "Installed" if ue4ss_installed else "Not Installed",
            "button_label": "Re-Install RE-UE4SS" if ue4ss_installed else "Install RE-UE4SS",
            "description": "Lua scripting system, live UObject property inspector, Blueprint dumper, and C++ header generator for UE4/5.",
            "release_url": "https://github.com/UE4SS-RE/RE-UE4SS/releases",
        },
        {
            "id": "fmodel",
            "name": "FModel (Unreal Engine .pak / .utoc Archive Explorer)",
            "category_badge": "Unreal Engine 4/5 Asset Discovery",
            "recommended": bool(is_ue4_5),
            "installed": fmodel_installed,
            "install_target": str(TOOLS_ROOT / "FModel"),
            "status_text": "Installed in Tools Folder" if fmodel_installed else "Not Installed",
            "button_label": "Re-Install FModel" if fmodel_installed else "Install FModel",
            "description": "Browses and extracts Unreal Engine 4/5 .pak and IoStore (.utoc/.ucas) game archives, DataTables, and Blueprints.",
            "release_url": "https://github.com/4sval/FModel/releases",
        },
        {
            "id": "x64dbg",
            "name": "x64dbg (Native x64/x32 Debugger & Memory Scanner)",
            "category_badge": "Custom C++ / UE3 Dynamic Debugging",
            "recommended": bool(is_custom_cpp),
            "installed": x64dbg_installed,
            "install_target": str(TOOLS_ROOT / "x64dbg"),
            "status_text": "Installed in Tools Folder" if x64dbg_installed else "Not Installed",
            "button_label": "Re-Install x64dbg" if x64dbg_installed else "Install x64dbg",
            "description": "Open-source Windows x64/x32 debugger for dynamic assembly patching, AOB scanning, and memory inspection.",
            "release_url": "https://github.com/x64dbg/x64dbg/releases",
        },
        {
            "id": "gdre_tools",
            "name": "GDRE Tools / GDSDecomp (Godot PCK & Script Extractor)",
            "category_badge": "Godot Engine Modding",
            "recommended": bool(is_godot),
            "installed": gdre_installed,
            "install_target": str(TOOLS_ROOT / "GDRETools"),
            "status_text": "Installed in Tools Folder" if gdre_installed else "Not Installed",
            "button_label": "Re-Install GDRE Tools" if gdre_installed else "Install GDRE Tools",
            "description": "Decompiles Godot .pck archives, recovers full Godot project scenes, and extracts GDScript files.",
            "release_url": "https://github.com/GDRETools/gdsdecomp/releases",
        },
        {
            "id": "mod_engine_2",
            "name": "Mod Engine 2 (FromSoftware File & Regulation Loader)",
            "category_badge": "FromSoftware Engine Modding",
            "recommended": bool(is_fromsoft),
            "installed": modengine2_installed,
            "install_target": str(TOOLS_ROOT / "ModEngine2"),
            "status_text": "Installed in Tools Folder" if modengine2_installed else "Not Installed",
            "button_label": "Re-Install Mod Engine 2" if modengine2_installed else "Install Mod Engine 2",
            "description": "Drop-in file override and DLL injection loader for Elden Ring, Dark Souls, and Armored Core VI.",
            "release_url": "https://github.com/soulsmods/ModEngine2/releases",
        },
        {
            "id": "reframework",
            "name": "REFramework (Capcom RE Engine Mod Loader & Lua API)",
            "category_badge": "Capcom RE Engine Modding",
            "recommended": bool(is_re_engine),
            "installed": reframework_installed,
            "install_target": str(TOOLS_ROOT / "REFramework"),
            "status_text": "Installed in Tools Folder" if reframework_installed else "Not Installed",
            "button_label": "Re-Install REFramework" if reframework_installed else "Install REFramework",
            "description": "Lua scripting API, reflection explorer, and VR/FreeCam framework for Capcom RE Engine titles.",
            "release_url": "https://github.com/praydog/REFramework/releases",
        },
        {
            "id": "xedit_creation",
            "name": "xEdit / SSEEdit / FO4Edit (Creation Engine Plugin Editor)",
            "category_badge": "Creation Engine (.esm / .esp)",
            "recommended": bool(is_creation),
            "installed": xedit_installed,
            "install_target": str(TOOLS_ROOT / "xEdit"),
            "status_text": "Installed in Tools Folder" if xedit_installed else "Not Installed",
            "button_label": "Re-Install xEdit" if xedit_installed else "Install xEdit",
            "description": "Inspects, resolves conflicts, and patches Creation Engine .esm, .esp, and .esl records.",
            "release_url": "https://github.com/TES5Edit/TES5Edit/releases",
        },
    ]
    ue4ss_in_game = False
    if dir_exists:
        shipping_dirs = {p.parent for p in game_dir.rglob("*-Win64-Shipping.exe")}
        win64_dirs = shipping_dirs or {
            p for p in game_dir.rglob("Win64") if p.is_dir() and p.parent.name.lower() == "binaries"
        }
        for wdir in win64_dirs:
            has_proxy = any(
                f.is_file() and f.name.lower() in ("dwmapi.dll", "dxgi.dll")
                for f in wdir.iterdir()
            )
            has_ue4ss = any(
                f.is_file() and f.name.lower() == "ue4ss.dll" for f in wdir.rglob("*.dll")
            )
            if has_proxy and has_ue4ss:
                ue4ss_in_game = True
                break
    game_installed_map = {
        "bepinex_il2cpp": bep_il2cpp_installed,
        "bepinex_mcp_il2cpp": bridge_il2cpp_installed,
        "unity_explorer_il2cpp": bool(
            dir_exists
            and (game_dir / "BepInEx" / "plugins").is_dir()
            and list((game_dir / "BepInEx" / "plugins").rglob("*UnityExplorer*.dll"))
        ),
        "bepinex_mono": bool(info.get("doorstop_installed") and info.get("bepinex_installed") and is_mono),
        "ue4ss": ue4ss_in_game,
    }
    for app_item in apps:
        aid = app_item.get("id", "")
        app_item["engine_compatible"] = bool(app_item.get("recommended", False))
        app_item["host_available"] = bool(app_item.get("installed", False))
        app_item["installed_in_game"] = bool(game_installed_map.get(aid, False))
    return apps


def enrich_game_status(game: dict[str, Any], refresh_live: bool = False) -> dict[str, Any]:
    enriched = dict(game)
    gdir = Path(str(game.get("game_dir", "")).strip())
    dir_exists = gdir.is_dir()
    missing_on_disk = bool(game.get("missing_on_disk", False)) or (not dir_exists)
    enriched["dir_exists"] = dir_exists
    enriched["missing_on_disk"] = missing_on_disk
    enriched["metadata_scraped"] = bool(game.get("metadata_scraped", False))
    enriched["last_scraped_at"] = game.get("last_scraped_at")
    enriched["has_game_assembly"] = (gdir / "GameAssembly.dll").is_file() if dir_exists else False
    enriched["interop_ready"] = (
        (gdir / "BepInEx" / "interop" / "assembly-hash.txt").is_file() if dir_exists else False
    )
    enriched["plugin_installed"] = (
        (gdir / "BepInEx" / "plugins" / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.dll").is_file()
        if dir_exists
        else False
    )
    enriched["manifest_exists"] = manifest_path_for_game(gdir).is_file() if dir_exists else False

    cached_info = game.get("cached_modding_info")
    if isinstance(cached_info, dict) and cached_info:
        mod_info = dict(cached_info)
        mod_info["dir_exists"] = dir_exists
        mod_info["missing_on_disk"] = missing_on_disk
        mod_info["metadata_scraped"] = True
        if dir_exists and refresh_live:
            prev_ue = mod_info.get("unreal_settings") if isinstance(mod_info.get("unreal_settings"), dict) else {}
            prev_tier_id = str(prev_ue.get("active_reference_tier_id", "auto")) if prev_ue else "auto"
            prev_ini_path = str(prev_ue.get("ini_path") or "") if prev_ue else ""
            live_info = inspect_game_modding_info(gdir, str(game.get("name", "")))
            for k in (
                "doorstop_installed",
                "doorstop_version",
                "bepinex_installed",
                "bepinex_version",
                "interop_assemblies_count",
                "assembly_hash",
                "mcp_bridge_installed",
                "roslyn_Compiler_installed",
                "manifest_tracked",
                "crucial_steps",
                "modding_readiness",
                "modding_apps",
                "is_unreal_engine",
                "unreal_settings",
                "reshade",
                "installed_mods",
                "installed_mods_summary",
            ):
                mod_info[k] = live_info.get(k)
            if mod_info.get("is_unreal_engine") and (prev_tier_id != "auto" or prev_ini_path):
                mod_info["unreal_settings"] = inspect_unreal_game_settings(
                    gdir,
                    mod_info,
                    custom_ini_path=prev_ini_path,
                    reference_tier_id=prev_tier_id,
                )
        elif missing_on_disk:
            mod_info["modding_readiness"] = (
                "Cannot Be Found on Disk (Showing Previously Downloaded Metadata)"
            )
            mod_info["modding_apps"] = evaluate_modding_apps_for_game(gdir, mod_info)
        enriched["modding_info"] = mod_info
        cat_low = str(mod_info.get("mcp_category") or "").lower()
        tech_low = str(mod_info.get("sheet_engine_tech") or "").lower()
        if mod_info.get("is_unreal_engine") or "unreal" in cat_low or "unreal" in tech_low:
            enriched["detected_engine"] = "unreal"
        elif enriched.get("has_game_assembly") or "il2cpp" in str(mod_info.get("scripting_backend") or "").lower():
            enriched["detected_engine"] = "unity_il2cpp"
        elif "mono" in str(mod_info.get("scripting_backend") or "").lower():
            enriched["detected_engine"] = "unity_mono"
        elif "godot" in cat_low:
            enriched["detected_engine"] = "godot"
        elif "creation" in cat_low:
            enriched["detected_engine"] = "creation_engine"
        else:
            enriched["detected_engine"] = "generic"
    else:
        raw_folder_name = (
            gdir.parent.name
            if gdir.name.lower() == "content" and gdir.parent.name
            else gdir.name
        )
        unscraped_info: dict[str, Any] = {
            "dir_exists": dir_exists,
            "missing_on_disk": missing_on_disk,
            "metadata_scraped": False,
            "developer": "Not scraped yet",
            "product_name": raw_folder_name or game.get("name", "Unknown"),
            "executable": "Click Load Game to scrape",
            "platform": game.get("platform_label", "PC"),
            "architecture": "Not scraped yet",
            "graphics_api": "Not scraped yet",
            "xr_sdk": "Not scraped yet",
            "unity_version": "Not scraped yet",
            "build_guid": "Not scraped yet",
            "scripting_backend": game.get("runtime", "Not scraped yet"),
            "metadata_version": None,
            "scripting_assemblies_count": 0,
            "interop_assemblies_count": 0,
            "assembly_hash": None,
            "doorstop_installed": False,
            "doorstop_version": None,
            "bepinex_installed": False,
            "bepinex_version": None,
            "mcp_bridge_installed": False,
            "roslyn_Compiler_installed": False,
            "manifest_tracked": False,
            "required_modding_tool": "Click Load Game to scrape folder metadata",
            "required_bridge_plugin": "Click Load Game to scrape folder metadata",
            "modding_readiness": (
                "Cannot Be Found on Disk (No Cached Metadata)"
                if missing_on_disk
                else "Not Scraped Yet — Click Load Game to Scrape Metadata"
            ),
            "mcp_category": game.get("runtime", "Pending Scrape"),
            "sheet_engine_tech": "Pending first load scrape",
            "recommended_mcp_stack": "Click Load Game to scrape & match reference sheet",
            "discovery_mcp_tools": "Metadata will be scraped from the game folder when loaded for the first time.",
            "modding_mcp_tools": "Metadata will be scraped from the game folder when loaded for the first time.",
            "sheet_reference_match": "Pending first load scrape",
            "crucial_steps": [],
        }
        unscraped_info["modding_apps"] = evaluate_modding_apps_for_game(gdir, unscraped_info)
        enriched["modding_info"] = unscraped_info
    return enriched


@router.get("/api/games")
async def list_games(request: Request):
    import time

    now = time.monotonic()
    cached = GAMES_RESPONSE_CACHE.get("payload")
    etag = GAMES_RESPONSE_CACHE.get("etag")
    if cached is not None and (now - float(GAMES_RESPONSE_CACHE.get("ts", 0.0))) < 300.0:
        if etag and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(content=cached, headers={"ETag": etag} if etag else {})

    store = load_managed_games()
    games = [enrich_game_status(g, refresh_live=False) for g in store.get("games", [])]
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
async def select_game(req: SelectGameRequest) -> dict[str, Any]:
    store = load_managed_games()
    target = next((g for g in store["games"] if g["id"] == req.game_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Managed game '{req.game_id}' not found.")

    gdir = Path(str(target.get("game_dir", "")).strip())
    dir_exists = gdir.is_dir()
    target["missing_on_disk"] = not dir_exists
    scraped_now = False

    # When loaded for the first time (and folder exists), scrape metadata from game folder
    if (not target.get("metadata_scraped")) and dir_exists:
        scraped_info = await asyncio.to_thread(inspect_game_modding_info, gdir, str(target.get("name", "")))
        scraped_info["metadata_scraped"] = True
        target["cached_modding_info"] = scraped_info
        target["metadata_scraped"] = True
        target["last_scraped_at"] = datetime.now(timezone.utc).isoformat()
        scraped_now = True

    store["active_game_id"] = req.game_id
    save_managed_games(store)

    status_note = (
        "scraped metadata on first load"
        if scraped_now
        else (
            "game folder missing on disk — loaded cached metadata"
            if not dir_exists
            else "loaded cached metadata"
        )
    )
    info_hash = sha256_text(
        json.dumps(target.get("cached_modding_info") or {"game_dir": str(gdir)}, sort_keys=True, default=str)
    )
    append_audit_log(
        action="game_manager.load_and_scan" if scraped_now else "game_manager.load",
        status="warning" if not dir_exists else "ok",
        summary=f"Loaded game '{target['name']}' ({req.game_id}) [{status_note}]",
        sha256=info_hash,
        details={
            "game_id": req.game_id,
            "game_dir": target.get("game_dir"),
            "scraped_now": scraped_now,
            "missing_on_disk": not dir_exists,
        },
    )
    return {
        "status": "ok",
        "active_game_id": req.game_id,
        "scraped_now": scraped_now,
        "missing_on_disk": not dir_exists,
        "game": enrich_game_status(target),
    }


@router.post("/api/games/rescan")
async def rescan_game_metadata(req: SelectGameRequest) -> dict[str, Any]:
    """Explicitly rescans the game folder on disk to refresh cached metadata in the Game Info Panel."""
    store = load_managed_games()
    target = next((g for g in store["games"] if g["id"] == req.game_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Managed game '{req.game_id}' not found.")

    gdir = Path(str(target.get("game_dir", "")).strip())
    if not gdir.is_dir():
        target["missing_on_disk"] = True
        save_managed_games(store)
        append_audit_log(
            action="game_manager.rescan",
            status="warning",
            summary=f"Rescan failed for '{target['name']}': folder cannot be found on disk ({gdir}); retaining cached info",
            sha256=sha256_text(str(gdir).lower()),
            details={"game_id": req.game_id, "game_dir": str(gdir)},
        )
        return {
            "status": "missing_on_disk",
            "message": f"Game folder cannot be found on disk: {gdir}. Showing previously downloaded information.",
            "active_game_id": req.game_id,
            "game": enrich_game_status(target),
        }

    scraped_info = inspect_game_modding_info(gdir, str(target.get("name", "")))
    scraped_info["metadata_scraped"] = True
    target["cached_modding_info"] = scraped_info
    target["metadata_scraped"] = True
    target["missing_on_disk"] = False
    target["last_scraped_at"] = datetime.now(timezone.utc).isoformat()
    save_managed_games(store)

    rescan_hash = sha256_text(json.dumps(scraped_info, sort_keys=True, default=str))
    append_audit_log(
        action="game_manager.rescan",
        status="ok",
        summary=(
            f"Rescanned game folder metadata for '{target['name']}' ({req.game_id}) "
            f"[{scraped_info.get('mcp_category')} | {scraped_info.get('modding_readiness')}]"
        ),
        sha256=rescan_hash,
        details={"game_id": req.game_id, "game_dir": str(gdir)},
    )
    return {
        "status": "ok",
        "message": f"Successfully rescanned metadata from {gdir}",
        "active_game_id": req.game_id,
        "game": enrich_game_status(target),
    }


SCAN_PROGRESS: dict[str, Any] = {
    "active": False,
    "kind": "",
    "phase": "",
    "current": 0,
    "total": 0,
    "percent": 0,
    "current_item": "",
}


@router.get("/api/games/scan-progress")
async def get_scan_progress() -> dict[str, Any]:
    """Returns live progress for an active Game scan or Game info scan."""
    return {"status": "ok", **SCAN_PROGRESS}


@router.post("/api/games/scan-info")
async def scan_all_games_info() -> dict[str, Any]:
    """Scrapes and populates Game Info metadata for all found games in the Game Manager."""
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
                scraped_info = await asyncio.to_thread(inspect_game_modding_info, gdir, g_name)
                scraped_info["metadata_scraped"] = True
                g["cached_modding_info"] = scraped_info
                g["metadata_scraped"] = True
                g["missing_on_disk"] = False
                g["last_scraped_at"] = now_iso
                scraped_count += 1
                scraped_games.append(
                    {
                        "id": g.get("id"),
                        "name": g.get("name"),
                        "status": "scraped",
                        "mcp_category": scraped_info.get("mcp_category"),
                        "unity_version": scraped_info.get("unity_version"),
                        "modding_readiness": scraped_info.get("modding_readiness"),
                    }
                )
            else:
                g["missing_on_disk"] = True
                missing_count += 1
                scraped_games.append(
                    {
                        "id": g.get("id"),
                        "name": g.get("name"),
                        "status": "missing_on_disk (retained cached info)"
                        if g.get("metadata_scraped")
                        else "missing_on_disk (no cached info)",
                    }
                )
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
        details={
            "scraped_count": scraped_count,
            "missing_count": missing_count,
            "total_games": total,
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "scraped_count": scraped_count,
        "missing_count": missing_count,
        "total_games": total,
        "results": scraped_games,
        "games": [enrich_game_status(g) for g in store.get("games", [])],
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
            # Preserve cached metadata and scraping state when updating settings
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
        "game": enrich_game_status(game_dict),
        "games": [enrich_game_status(g) for g in store["games"]],
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
        "games": [enrich_game_status(g) for g in store["games"]],
    }


def discover_platform_roots() -> list[tuple[str, Path]]:
    """Finds installed PC game platform libraries (Steam, Epic, GOG, Xbox, Ubisoft, EA) across local drives."""
    import re
    import winreg

    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add_root(platform_label: str, candidate: Path) -> None:
        try:
            if not candidate.is_dir():
                return
            norm = str(candidate.resolve()).lower()
            if norm not in seen:
                seen.add(norm)
                roots.append((platform_label, candidate.resolve()))
        except Exception:
            pass

    # 1. Steam via Registry + libraryfolders.vdf
    steam_base_candidates = [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
    ]
    for reg_root, reg_path, val_name in [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ]:
        try:
            with winreg.OpenKey(reg_root, reg_path) as key:
                val, _ = winreg.QueryValueEx(key, val_name)
                if val:
                    steam_base_candidates.insert(0, Path(str(val)))
        except OSError:
            pass

    for sbase in steam_base_candidates:
        add_root("Steam", sbase / "steamapps" / "common")
        vdf = sbase / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                content = vdf.read_text(encoding="utf-8", errors="replace")
                for match in re.finditer(r'"path"\s+"([^"]+)"', content, re.IGNORECASE):
                    lib_path = Path(match.group(1).replace("\\\\", "\\"))
                    add_root("Steam", lib_path / "steamapps" / "common")
            except Exception:
                pass

    # 2. GOG Galaxy via Registry
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\GalaxyClient\paths"
        ) as key:
            val, _ = winreg.QueryValueEx(key, "client")
            if val:
                add_root("GOG Galaxy", Path(str(val)).parent / "Games")
    except OSError:
        pass

    # 3. Default platform directories across drives C: .. G:
    for drive in ("C:", "D:", "E:", "F:", "G:"):
        drv = Path(drive + "\\")
        if not drv.is_dir():
            continue
        add_root("Steam", drv / "SteamLibrary" / "steamapps" / "common")
        add_root("Steam", drv / "Steam" / "steamapps" / "common")
        add_root("Steam", drv / "Program Files (x86)" / "Steam" / "steamapps" / "common")
        add_root("Epic Games", drv / "Program Files" / "Epic Games")
        add_root("Epic Games", drv / "Epic Games")
        add_root("GOG Galaxy", drv / "GOG Games")
        add_root("GOG Galaxy", drv / "Program Files (x86)" / "GOG Galaxy" / "Games")
        add_root("Xbox / PC Game Pass", drv / "XboxGames")
        add_root("Ubisoft Connect", drv / "Program Files (x86)" / "Ubisoft" / "Ubisoft Game Launcher" / "games")
        add_root("EA Games", drv / "Program Files" / "EA Games")
        add_root("Games Folder", drv / "Games")

    return roots


def is_game_candidate(
    folder: Path,
    discovery_scope: str = "all",
    platform_label: str = "",
) -> tuple[bool, str]:
    """Checks whether a directory matches the requested discovery_scope and returns (matched, runtime_kind)."""
    if not folder.is_dir():
        return (False, "")

    scope = (discovery_scope or "all").strip().lower()
    plat_lower = (platform_label or "").lower()

    # Platform-specific filter check
    if scope == "platform_steam" and "steam" not in plat_lower:
        return (False, "")
    if scope == "platform_epic" and "epic" not in plat_lower:
        return (False, "")
    if scope == "platform_gog" and "gog" not in plat_lower:
        return (False, "")
    if scope == "platform_xbox" and "xbox" not in plat_lower:
        return (False, "")
    if scope == "platform_ubisoft" and "ubisoft" not in plat_lower:
        return (False, "")
    if scope == "platform_ea" and "ea" not in plat_lower:
        return (False, "")

    has_il2cpp = (folder / "GameAssembly.dll").is_file()
    data_dirs = [p for p in folder.glob("*_Data") if p.is_dir()]
    has_mono = (not has_il2cpp) and any(
        (d / "Managed" / "Assembly-CSharp.dll").is_file()
        or (d / "data.unity3d").is_file()
        or (d / "globalgamemanagers").is_file()
        for d in data_dirs
    )
    has_unity = has_il2cpp or has_mono
    has_unity_vr = has_unity and (
        any((d / "UnitySubsystems").is_dir() for d in data_dirs)
        or bool(list(folder.glob("*OpenXR*")))
        or bool(list(folder.glob("*OculusXR*")))
    )

    skip_exe_tokens = (
        "unins", "crash", "setup", "redist", "dxsetup", "vcredist",
        "unitycrashhandler", "cleanup", "touchup", "gamelaunchhelper",
        "vrmonitor", "vrstartup", "steamvr",
    )

    def _valid_exes(paths) -> list[Path]:
        return [
            p for p in paths
            if p.is_file() and not any(skip in p.name.lower() for skip in skip_exe_tokens)
        ]

    exes = _valid_exes(folder.glob("*.exe"))
    if not exes:
        for pattern in ("*/*.exe", "*/*/*.exe", "*/*/*/*.exe"):
            exes = _valid_exes(folder.glob(pattern))
            if exes:
                break
    if not has_unity and not exes:
        return (False, "")

    raw_name = folder.parent.name if folder.name.lower() == "content" and folder.parent.name else folder.name
    raw_low = raw_name.lower()
    non_game_dir_tokens = (
        "steamworks shared",
        "steamvr",
        "steam controller configs",
        "dedicated server",
        "benchmark tool",
        "3dmark",
        "soundtrack",
        "artbook",
        "digital art book",
        "last crusade pack",
    )
    if any(tok in raw_low for tok in non_game_dir_tokens):
        return (False, "")
    if folder.parent and str(folder).lower() in load_scan_exclusions():
        return (False, "")
    sheet_row = lookup_sheet_reference(raw_name, raw_name)
    sheet_cat = sheet_row.get("MCP Category", "") if sheet_row else ""
    sheet_tech = sheet_row.get("Engine / Tech", "") if sheet_row else ""

    has_anticheat_bin = any(
        (folder / ac).exists() or bool(list(folder.glob(f"*/{ac}")))
        for ac in ANTI_CHEAT_BINARIES
    )
    is_online_ac = has_anticheat_bin or (sheet_cat == "Online / Anti-Cheat")
    is_utility_demo = (
        sheet_cat == "Utility / Demo / Artifact"
        or "Utility" in sheet_tech
        or "Demo" in sheet_tech
        or "Artifact" in sheet_tech
    )
    is_ue4_5 = (
        sheet_cat == "Unreal Engine 4/5"
        or sheet_tech == "Unreal Engine 4/5"
        or ((folder / "Engine").is_dir() and bool(list(folder.glob("*/Content/Paks/*.pak"))))
    )
    is_ue3 = (
        sheet_tech == "Unreal Engine 3"
        or any(
            (folder / sub / "CookedPC").is_dir() or (folder / sub / "CookedPCConsole").is_dir()
            for sub in ("Engine", "WillowGame", "BioGame", "BmGame", "UDKGame")
        )
    )
    is_creation = (
        sheet_cat == "Creation Engine"
        or bool(list(folder.glob("Data/*.esm")))
        or bool(list(folder.glob("*.esm")))
    )
    is_godot = (sheet_cat == "Godot") or bool(list(folder.glob("*.pck")))
    is_re_engine = ("RE Engine" in sheet_tech) or (folder / "re_chunk_000.pak").is_file()
    is_fromsoft = (
        ("FromSoftware" in sheet_tech)
        or (folder / "regulation.bin").is_file()
        or (folder / "Game" / "regulation.bin").is_file()
    )
    is_data_driven = sheet_cat == "Data-Driven / Scriptable"
    is_custom_cpp = (
        sheet_cat == "Custom / Legacy C++"
        or (
            not has_unity
            and not is_ue4_5
            and not is_ue3
            and not is_creation
            and not is_godot
            and not is_re_engine
            and not is_fromsoft
            and not is_data_driven
            and not is_online_ac
            and not is_utility_demo
        )
    )
    is_provisioned = (
        (folder / "BepInEx" / "core").is_dir()
        or (folder / "winhttp.dll").is_file()
        or (folder / "dwmapi.dll").is_file()
    )

    if has_il2cpp:
        runtime_kind = "Unity_IL2CPP"
    elif has_mono:
        runtime_kind = "Unity_Mono"
    elif is_online_ac:
        runtime_kind = "Online / Anti-Cheat"
    elif is_ue4_5:
        runtime_kind = "Unreal Engine 4/5"
    elif is_ue3:
        runtime_kind = "Unreal Engine 3"
    elif is_creation:
        runtime_kind = "Creation Engine"
    elif is_godot:
        runtime_kind = "Godot"
    elif is_re_engine:
        runtime_kind = "RE Engine"
    elif is_fromsoft:
        runtime_kind = "FromSoftware"
    elif is_data_driven:
        runtime_kind = "Data-Driven / Scriptable"
    elif is_utility_demo:
        runtime_kind = "Utility / Demo / Artifact"
    else:
        runtime_kind = "Custom / Legacy C++"

    # Evaluate scope matching
    if scope in ("all", "platform_steam", "platform_epic", "platform_gog", "platform_xbox", "platform_ubisoft", "platform_ea"):
        return (True, runtime_kind)
    if scope == "moddable_only":
        return ((not is_online_ac and not is_utility_demo), runtime_kind)
    if scope == "provisioned_only":
        return (is_provisioned, runtime_kind)
    if scope == "sheet_matched":
        return (sheet_row is not None, runtime_kind)
    if scope in ("unity_all", "true"):
        return (has_unity, runtime_kind)
    if scope == "unity_il2cpp":
        return (has_il2cpp, runtime_kind)
    if scope == "unity_mono":
        return (has_mono, runtime_kind)
    if scope == "unity_vr":
        return (has_unity_vr, runtime_kind)
    if scope == "unreal_all":
        return ((is_ue4_5 or is_ue3), runtime_kind)
    if scope == "unreal_4_5":
        return (is_ue4_5, runtime_kind)
    if scope == "unreal_3":
        return (is_ue3, runtime_kind)
    if scope == "creation_engine":
        return (is_creation, runtime_kind)
    if scope == "godot":
        return (is_godot, runtime_kind)
    if scope == "re_engine":
        return (is_re_engine, runtime_kind)
    if scope == "fromsoftware":
        return (is_fromsoft, runtime_kind)
    if scope == "data_driven":
        return (is_data_driven, runtime_kind)
    if scope == "custom_cpp":
        return (is_custom_cpp, runtime_kind)
    if scope == "online_anticheat":
        return (is_online_ac, runtime_kind)
    if scope == "utility_demo":
        return (is_utility_demo, runtime_kind)

    return (True, runtime_kind)


def make_slug(name: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "game"


def build_managed_game_from_folder(folder: Path, platform_label: str, runtime_kind: str) -> dict[str, Any]:
    """Creates a lightweight managed game entry without scraping metadata yet (scraped on first Load)."""
    raw_name = folder.parent.name if folder.name.lower() == "content" and folder.parent.name else folder.name
    slug = make_slug(raw_name)
    return {
        "id": slug,
        "name": f"{raw_name} ({platform_label})",
        "tab_label": raw_name,
        "platform_label": platform_label,
        "has_dedicated_tab": False,
        "runtime": runtime_kind,
        "game_dir": str(folder.resolve()),
        "bepinex_zip_path": DEFAULT_BEPINEX_ZIP,
        "expected_sha256": DEFAULT_BEPINEX_SHA256,
        "plugin_build_dir": DEFAULT_PLUGIN_BUILD_DIR,
        "venv_python_path": DEFAULT_VENV_PYTHON,
        "mcp_app_path": DEFAULT_MCP_APP,
        "game_ip": "localhost",
        "default_entity": "paladin_piece",
        "default_variable": "stat_hp_max",
        "default_go_id": "",
        "default_component": "UnityEngine.Transform",
        "default_member": "localScale",
        "default_value": "(1, 1, 1)",
        "default_profile": f"{slug}_default",
        "default_selector": {},
        "notes": f"Auto-populated from {platform_label} ({runtime_kind}) — Load game to scrape folder metadata",
        "metadata_scraped": False,
        "missing_on_disk": False,
        "cached_modding_info": None,
        "last_scraped_at": None,
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
        is_direct, _ = is_game_candidate(cpath, discovery_scope="all", platform_label="Manual Path")
        if is_direct:
            candidate_folders.append(("Manual Path", cpath))
        if (cpath / "Content").is_dir():
            candidate_folders.append(("Xbox / Manual Path", cpath / "Content"))
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
                added_games.append(enrich_game_status(entry))
                discovered.append({**enrich_game_status(entry), "scan_status": "added"})
            else:
                existing_entry = existing_by_dir.get(
                    norm_dir, build_managed_game_from_folder(folder, platform_label, runtime_kind)
                )
                existing_entry["missing_on_disk"] = False
                discovered.append({
                    **enrich_game_status(existing_entry),
                    "scan_status": "already_managed" if already_managed else "found",
                })
    finally:
        SCAN_PROGRESS.update({
            "active": False,
            "percent": 100,
            "current": total_candidates,
            "phase": "Platform scan complete",
        })

    # Mark any previously managed games whose folder can no longer be found on disk
    missing_games: list[str] = []
    for g in store.get("games", []):
        gdir_str = str(g.get("game_dir", "")).strip()
        gdir = Path(gdir_str) if gdir_str else None
        is_missing = (gdir is None) or (not gdir.is_dir())
        if not req.custom_path.strip() and not req.unity_only:
            # On a full default-directory scan, also check if it wasn't found in disk check
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
        "games": [enrich_game_status(g) for g in store["games"]],
        "active_game_id": store.get("active_game_id", "battleground"),
    }


@router.post("/api/games/browse")
async def browse_for_folder(req: BrowseFolderRequest) -> dict[str, Any]:
    """Opens a native Windows folder picker dialog and returns the selected directory path."""
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


def discover_local_unreal_ini_profiles() -> list[dict[str, str]]:
    """Finds all Unreal Engine GameUserSettings.ini files under %LOCALAPPDATA%\\*\\Saved\\Config."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    profiles: list[dict[str, str]] = []
    if not local_app_data.is_dir():
        return profiles

    for child in sorted(local_app_data.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        cfg_root = child / "Saved" / "Config"
        if not cfg_root.is_dir():
            continue
        for sub_name in ("Windows", "WindowsNoEditor", "WindowsClient", "WinGDK"):
            gus = cfg_root / sub_name / "GameUserSettings.ini"
            if gus.is_file():
                try:
                    if gus.stat().st_size > 10:
                        profiles.append(
                            {
                                "project_name": child.name,
                                "platform_folder": sub_name,
                                "ini_path": str(gus.resolve()),
                            }
                        )
                        break
                except OSError:
                    pass
    return profiles


def parse_unreal_ini_file(ini_path: Path) -> dict[str, dict[str, str]]:
    """Parses an Unreal Engine .ini file into {section: {key: value}}."""
    sections: dict[str, dict[str, str]] = {}
    if not ini_path.is_file():
        return sections
    current_sec = "Global"
    sections[current_sec] = {}
    try:
        for raw_line in ini_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_sec = line[1:-1].strip()
                sections.setdefault(current_sec, {})
            elif "=" in line:
                k, v = line.split("=", 1)
                sections.setdefault(current_sec, {})[k.strip()] = v.strip()
    except Exception:
        pass
    return sections


def inspect_unreal_game_settings(
    game_dir: Path,
    info: dict[str, Any],
    custom_ini_path: str = "",
    reference_tier_id: str = "auto",
) -> dict[str, Any]:
    """Locates and imports Unreal Engine settings (GameUserSettings.ini / Engine.ini), categorizes Performance & Fidelity settings, and compares against PC Specs."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    dir_exists = game_dir.is_dir()

    # 1. Identify Unreal project code name (e.g., FFRS, WFO, PioneerGame)
    project_candidates: list[str] = []
    if dir_exists:
        staged_inis = list((game_dir / "Engine" / "Config").glob("StagedBuild_*.ini")) if (game_dir / "Engine" / "Config").is_dir() else []
        for st in staged_inis:
            proj = st.stem.replace("StagedBuild_", "").strip()
            if proj and proj not in project_candidates:
                project_candidates.append(proj)
        for sub in game_dir.iterdir():
            if sub.is_dir() and sub.name.lower() not in ("engine", "resources", "bepinex"):
                if (sub / "Content" / "Paks").is_dir() or (sub / "Binaries").is_dir():
                    if sub.name not in project_candidates:
                        project_candidates.append(sub.name)

    exe_name = str(info.get("executable", "")).strip()
    if exe_name and exe_name.lower().endswith(".exe"):
        stem = Path(exe_name).stem.replace("-Win64-Shipping", "").replace("-WinGDK-Shipping", "")
        if stem.lower() not in ("gamelaunchhelper", "crashreportclient", "bssndrpt64", "unknown"):
            if stem not in project_candidates:
                project_candidates.append(stem)

    prod_name = str(info.get("product_name", "")).strip()
    if prod_name:
        if prod_name not in project_candidates:
            project_candidates.append(prod_name)
        compact = prod_name.replace(" ", "")
        if compact not in project_candidates:
            project_candidates.append(compact)

    detected_project = project_candidates[0] if project_candidates else (game_dir.name or "UnrealGame")

    # 2. Locate GameUserSettings.ini and companion Engine.ini / Scalability.ini
    found_gus_path: Path | None = None
    found_engine_ini_path: Path | None = None
    searched_locations: list[str] = []

    if custom_ini_path.strip():
        cpath = Path(custom_ini_path.strip())
        searched_locations.append(str(cpath))
        if cpath.is_file():
            found_gus_path = cpath
        elif cpath.is_dir():
            for cand_file in cpath.rglob("GameUserSettings.ini"):
                found_gus_path = cand_file
                break

    if found_gus_path is None:
        for proj in project_candidates:
            cfg_base = local_app_data / proj / "Saved" / "Config"
            for plat_sub in ("Windows", "WindowsNoEditor", "WindowsClient", "WinGDK"):
                candidate_ini = cfg_base / plat_sub / "GameUserSettings.ini"
                searched_locations.append(str(candidate_ini))
                if candidate_ini.is_file() and candidate_ini.stat().st_size > 10:
                    found_gus_path = candidate_ini
                    eng_cand = cfg_base / plat_sub / "Engine.ini"
                    if eng_cand.is_file():
                        found_engine_ini_path = eng_cand
                    break
            if found_gus_path is not None:
                break

    if found_gus_path is None and dir_exists:
        for cand_file in game_dir.rglob("GameUserSettings.ini"):
            if cand_file.is_file() and cand_file.stat().st_size > 10:
                found_gus_path = cand_file
                break

    sections = parse_unreal_ini_file(found_gus_path) if found_gus_path else {}
    flat: dict[str, str] = {}
    for sec_dict in sections.values():
        for k, v in sec_dict.items():
            flat[k] = v

    if found_engine_ini_path and found_engine_ini_path.is_file():
        eng_sections = parse_unreal_ini_file(found_engine_ini_path)
        for sec_dict in eng_sections.values():
            for k, v in sec_dict.items():
                flat.setdefault(k, v)

    pc_specs = scrape_pc_specs(force_refresh=False)
    native_w = int(pc_specs.get("display", {}).get("width", 1920))
    native_h = int(pc_specs.get("display", {}).get("height", 1080))
    native_hz = int(pc_specs.get("display", {}).get("refresh_rate_hz", 60))
    vram_gb = float(pc_specs.get("gpu", {}).get("vram_total_gb", 8.0))
    ram_gb = float(pc_specs.get("ram", {}).get("total_gb", 16.0))
    cpu_cores = int(pc_specs.get("cpu", {}).get("physical_cores", 6))
    cpu_threads = int(pc_specs.get("cpu", {}).get("logical_threads", 12))
    gpu_name = str(pc_specs.get("gpu", {}).get("name", "GPU"))
    cpu_name = str(pc_specs.get("cpu", {}).get("name", "CPU"))

    # Parse core values (with fallback defaults if INI hasn't been written yet)
    has_ini = found_gus_path is not None and len(flat) > 0
    res_x = int(float(flat.get("ResolutionSizeX", flat.get("DesiredScreenWidth", native_w)))) if has_ini else native_w
    res_y = int(float(flat.get("ResolutionSizeY", flat.get("DesiredScreenHeight", native_h)))) if has_ini else native_h
    fs_mode_raw = str(flat.get("FullscreenMode", "1"))
    fs_mode_map = {
        "0": "Exclusive Fullscreen (0)",
        "1": "Windowed Fullscreen / Borderless (1)",
        "2": "Windowed (2)",
    }
    fs_mode_label = fs_mode_map.get(fs_mode_raw, f"Mode {fs_mode_raw}")

    vsync_bool = str(flat.get("bUseVSync", "False")).lower() == "true"
    dyn_res_bool = str(flat.get("bUseDynamicResolution", "False")).lower() == "true"
    hdr_bool = str(flat.get("bUseHDRDisplayOutput", "False")).lower() == "true"
    hdr_nits = str(flat.get("HDRDisplayOutputNits", "1000"))

    fps_limit_raw = flat.get("FrameRateLimit", "0.000000")
    try:
        fps_limit_val = float(fps_limit_raw)
    except ValueError:
        fps_limit_val = 0.0
    fps_limit_label = "Unlimited (0 FPS Cap)" if fps_limit_val <= 0.1 else f"{int(fps_limit_val)} FPS Cap"

    res_scale_raw = flat.get("sg.ResolutionQuality", "100")
    try:
        res_scale_num = float(res_scale_raw)
    except ValueError:
        res_scale_num = 100.0
    res_scale_label = (
        "100% (Native / Auto 0)"
        if res_scale_num == 0 or res_scale_num == 100
        else f"{res_scale_num:.0f}% Internal 3D Scale"
    )

    view_dist_label, view_dist_lvl = _format_ue_quality(flat.get("sg.ViewDistanceQuality", "3" if has_ini else None))
    aa_label, aa_lvl = _format_ue_quality(flat.get("sg.AntiAliasingQuality", "3" if has_ini else None))
    shadow_label, shadow_lvl = _format_ue_quality(flat.get("sg.ShadowQuality", "3" if has_ini else None))
    gi_label, gi_lvl = _format_ue_quality(flat.get("sg.GlobalIlluminationQuality", "3" if has_ini else None))
    refl_label, refl_lvl = _format_ue_quality(flat.get("sg.ReflectionQuality", "3" if has_ini else None))
    post_label, post_lvl = _format_ue_quality(flat.get("sg.PostProcessQuality", "3" if has_ini else None))
    tex_label, tex_lvl = _format_ue_quality(flat.get("sg.TextureQuality", "3" if has_ini else None))
    fx_label, fx_lvl = _format_ue_quality(flat.get("sg.EffectsQuality", "3" if has_ini else None))
    fol_label, fol_lvl = _format_ue_quality(flat.get("sg.FoliageQuality", "3" if has_ini else None))
    shade_label, shade_lvl = _format_ue_quality(flat.get("sg.ShadingQuality", "3" if has_ini else None))
    land_label, land_lvl = _format_ue_quality(flat.get("sg.LandscapeQuality", "3" if has_ini else None))

    # Match PC Specs (or simulated reference tier override) against UNREAL_HARDWARE_REFERENCE_ARRAY
    ref_tier = match_pc_to_unreal_reference_tier(pc_specs, reference_tier_id)
    is_simulated_tier = (reference_tier_id or "auto").strip().lower() != "auto"
    eval_vram_gb = float(ref_tier.get("vram_budget_ceiling_gb", vram_gb)) if is_simulated_tier else vram_gb
    eval_cpu_cores = int(ref_tier.get("min_cpu_cores", cpu_cores)) if is_simulated_tier else cpu_cores
    eval_ram_gb = float(ref_tier.get("min_ram_gb", ram_gb)) if is_simulated_tier else ram_gb
    eval_hw_label = (
        f"{ref_tier['tier_name']} [Simulated Reference Profile]"
        if is_simulated_tier
        else f"{gpu_name} ({vram_gb} GB) + {cpu_name} [{ref_tier['tier_name']}]"
    )

    # Traffic-light evaluator for non-scalability performance settings
    target_res_y = int(ref_tier.get("target_res_y", 1080))
    max_safe_res_y = int(ref_tier.get("max_safe_res_y", 1440))
    if res_y > max_safe_res_y:
        res_tl = {
            "traffic_light": "red",
            "light_icon": "",
            "light_badge": "OVER-BUDGET RESOLUTION",
            "recommended_value": ref_tier["target_resolution"],
            "advice": f"{res_x}x{res_y} exceeds safe resolution ceiling ({max_safe_res_y}p) for {ref_tier['tier_name']}. Lower to {ref_tier['target_resolution']}.",
        }
    elif res_y < target_res_y:
        res_tl = {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "BELOW TARGET",
            "recommended_value": ref_tier["target_resolution"],
            "advice": f"{res_x}x{res_y} is below recommended {ref_tier['target_resolution']} for {ref_tier['tier_name']}. Safe to raise resolution.",
        }
    else:
        res_tl = {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": ref_tier["target_resolution"],
            "advice": f"Matches optimal resolution target ({ref_tier['target_resolution']}) for {ref_tier['tier_name']}.",
        }

    eff_scale = 100.0 if (res_scale_num == 0 or res_scale_num >= 99) else res_scale_num
    rec_scale = int(ref_tier.get("recommended_res_scale", 100))
    min_safe_scale = int(ref_tier.get("min_safe_res_scale", 67))
    if res_y >= 2160 and eval_vram_gb <= 10.0 and eff_scale >= 90:
        scale_tl = {
            "traffic_light": "red",
            "light_icon": "",
            "light_badge": "TOO HIGH FOR 8GB AT 4K",
            "recommended_value": f"{rec_scale}% (TSR / DLSS Quality)",
            "advice": f"100% 3D Resolution Scale at 4K overflows {ref_tier['tier_name']} VRAM/fillrate. Lower sg.ResolutionQuality to {rec_scale}%.",
        }
    elif eff_scale < min_safe_scale:
        scale_tl = {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "LOW SCALE",
            "recommended_value": f"{rec_scale}%",
            "advice": f"Internal 3D scale ({eff_scale:.0f}%) is below recommended {rec_scale}% for {ref_tier['tier_name']}.",
        }
    else:
        scale_tl = {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": f"{rec_scale}%",
            "advice": f"3D Resolution Scale ({res_scale_label}) is well balanced for {ref_tier['tier_name']}.",
        }

    fps_tl = (
        {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "CAPPED BELOW REFRESH",
            "recommended_value": f"0 (Unlimited) or {native_hz} FPS",
            "advice": f"FPS cap ({int(fps_limit_val)}) is below your {native_hz}Hz monitor refresh rate.",
        }
        if (fps_limit_val > 0.1 and fps_limit_val < native_hz)
        else {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": f"0 (Unlimited) or {native_hz} FPS",
            "advice": f"Frame rate limit ({fps_limit_label}) allows full {native_hz}Hz display utilization.",
        }
    )

    vsync_tl = (
        {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "ADDS INPUT LATENCY",
            "recommended_value": "False (with G-SYNC / VRR) or True (60Hz)",
            "advice": "VSync is ON; if using a G-SYNC/FreeSync high-refresh display, disabling VSync reduces input lag.",
        }
        if (vsync_bool and native_hz >= 100)
        else {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": "Match Display VRR",
            "advice": "VSync setting is appropriate for current display refresh configuration.",
        }
    )

    rec_drs = bool(ref_tier.get("dynamic_resolution", False))
    drs_tl = (
        {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "ADVISED FOR 6-8GB GPUs",
            "recommended_value": "True" if rec_drs else "False",
            "advice": f"Enabling Dynamic Resolution is recommended on {ref_tier['tier_name']} to prevent heavy Lumen frame drops.",
        }
        if (rec_drs and not dyn_res_bool and res_y >= 1440)
        else {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": "True" if rec_drs else "False",
            "advice": f"Dynamic Resolution ({dyn_res_bool}) matches recommendation for {ref_tier['tier_name']}.",
        }
    )

    fs_tl = (
        {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": "0 (Exclusive) or 1 (Borderless DX12)",
            "advice": "DX12 Fullscreen / Borderless Independent Flip active for lowest presentation latency.",
        }
        if fs_mode_raw in ("0", "1")
        else {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "WINDOWED OVERHEAD",
            "recommended_value": "1 (Windowed Fullscreen)",
            "advice": "Windowed mode (2) disables DX12 Independent Flip; switch to FullscreenMode=1.",
        }
    )

    vd_tl = evaluate_scalability_traffic_light("sg.ViewDistanceQuality", view_dist_lvl, ref_tier)
    fol_tl = evaluate_scalability_traffic_light("sg.FoliageQuality", fol_lvl, ref_tier)
    gi_tl = evaluate_scalability_traffic_light("sg.GlobalIlluminationQuality", gi_lvl, ref_tier)
    shd_tl = evaluate_scalability_traffic_light("sg.ShadowQuality", shadow_lvl, ref_tier)
    refl_tl = evaluate_scalability_traffic_light("sg.ReflectionQuality", refl_lvl, ref_tier)
    tex_tl = evaluate_scalability_traffic_light("sg.TextureQuality", tex_lvl, ref_tier)
    aa_tl = evaluate_scalability_traffic_light("sg.AntiAliasingQuality", aa_lvl, ref_tier)
    shd_post_tl = evaluate_scalability_traffic_light("sg.ShadingQuality", shade_lvl, ref_tier)
    land_tl = evaluate_scalability_traffic_light("sg.LandscapeQuality", land_lvl, ref_tier)
    hdr_tl = {
        "traffic_light": "green",
        "light_icon": "",
        "light_badge": "OPTIMAL",
        "recommended_value": "Match Display HDR Capability",
        "advice": f"HDR output is {'Enabled' if hdr_bool else 'Disabled (SDR)'} ({hdr_nits} nits).",
    }

    # 3. Key Performance Settings List (with Traffic-Light evaluations)
    performance_settings = [
        {
            "key": "ResolutionSizeX / ResolutionSizeY",
            "name": "Output Render Resolution",
            "value": f"{res_x}x{res_y}" if has_ini else f"Not Saved Yet (Monitor Native: {native_w}x{native_h})",
            "impact": "High GPU Fillrate & VRAM Impact",
            "category": "Performance",
            "note": "Primary pixel count rendered or targeted by TSR/DLSS upscaler.",
            **res_tl,
        },
        {
            "key": "sg.ResolutionQuality",
            "name": "3D Internal Resolution Scale",
            "value": res_scale_label if has_ini else "Default (100%)",
            "impact": "High GPU Frame-Time Impact",
            "category": "Performance",
            "note": "Controls internal 3D rendering percentage before Temporal Super Resolution (TSR).",
            **scale_tl,
        },
        {
            "key": "FrameRateLimit",
            "name": "Frame Rate Limit (FPS Cap)",
            "value": fps_limit_label if has_ini else "Default (Unlimited)",
            "impact": "Frame Pacing & GPU Power",
            "category": "Performance",
            "note": "0.000000 = uncapped frame rate; set to monitor refresh rate for consistent frame pacing.",
            **fps_tl,
        },
        {
            "key": "bUseVSync",
            "name": "Vertical Sync (VSync)",
            "value": ("Enabled (True)" if vsync_bool else "Disabled (False)") if has_ini else "Default",
            "impact": "Input Latency & Tear Prevention",
            "category": "Performance",
            "note": "Locks presentation to display refresh rate; disable if using G-SYNC/FreeSync + reflex.",
            **vsync_tl,
        },
        {
            "key": "bUseDynamicResolution",
            "name": "Dynamic Resolution Scaling (DRS)",
            "value": ("Enabled (True)" if dyn_res_bool else "Disabled (False)") if has_ini else "Disabled (False)",
            "impact": "Frame Drop Mitigation",
            "category": "Performance",
            "note": "Automatically lowers internal 3D resolution during heavy GPU spikes.",
            **drs_tl,
        },
        {
            "key": "FullscreenMode",
            "name": "Window / Fullscreen Presentation",
            "value": fs_mode_label if has_ini else "Borderless Fullscreen (1)",
            "impact": "Swapchain Latency",
            "category": "Performance",
            "note": "0 = Exclusive Fullscreen, 1 = Windowed Fullscreen (DX12 Independent Flip), 2 = Windowed.",
            **fs_tl,
        },
        {
            "key": "sg.ViewDistanceQuality",
            "name": "View Distance & LOD Scale",
            "value": view_dist_label,
            "impact": "High CPU Draw-Call & Streaming Impact",
            "category": "Performance",
            "note": "Controls `r.ViewDistanceScale` and static mesh/Nanite culling distance.",
            **vd_tl,
        },
        {
            "key": "sg.FoliageQuality & sg.EffectsQuality",
            "name": "Foliage Density & Niagara Particle Effects",
            "value": f"Foliage: {fol_label} | Effects: {fx_label}",
            "impact": "Medium-High GPU Compute & Overdraw",
            "category": "Performance",
            "note": "Controls `foliage.DensityScale` and translucent Niagara GPU particle counts.",
            **fol_tl,
        },
    ]

    # 4. Key Visual Fidelity Settings List (with Traffic-Light evaluations)
    fidelity_settings = [
        {
            "key": "sg.GlobalIlluminationQuality",
            "name": "Global Illumination (Lumen GI / SSGI)",
            "value": gi_label,
            "impact": "Primary Lighting Fidelity (Heavy GPU)",
            "category": "Fidelity",
            "note": "Epic (3) / Cinematic (4) enables full-rate Lumen Dynamic GI & final gather quality.",
            **gi_tl,
        },
        {
            "key": "sg.ShadowQuality",
            "name": "Shadow Quality (Virtual Shadow Maps / CSM)",
            "value": shadow_label,
            "impact": "High Visual Depth & VRAM Impact",
            "category": "Fidelity",
            "note": "Controls Virtual Shadow Map (VSM) physical page pool resolution and shadow cascades.",
            **shd_tl,
        },
        {
            "key": "sg.ReflectionQuality",
            "name": "Reflection Quality (Lumen Reflections / SSR)",
            "value": refl_label,
            "impact": "Surface Realism (Heavy GPU Ray Tracing)",
            "category": "Fidelity",
            "note": "Controls Lumen reflection rays, roughness cutoffs, and screen-space reflection precision.",
            **refl_tl,
        },
        {
            "key": "sg.TextureQuality",
            "name": "Texture Quality & Anisotropic Filtering",
            "value": tex_label,
            "impact": "Surface Sharpness & VRAM Pool Size",
            "category": "Fidelity",
            "note": "Controls `r.Streaming.PoolSize` (VRAM budget) and max anisotropic filtering (16x at Epic).",
            **tex_tl,
        },
        {
            "key": "sg.AntiAliasingQuality",
            "name": "Anti-Aliasing & TSR Upscaling Precision",
            "value": aa_label,
            "impact": "Edge Stability & Sub-Pixel Detail",
            "category": "Fidelity",
            "note": "Controls Temporal Super Resolution (TSR) history sample count and anti-ghosting.",
            **aa_tl,
        },
        {
            "key": "sg.ShadingQuality & sg.PostProcessQuality",
            "name": "Material Shading & Post-Processing",
            "value": f"Shading: {shade_label} | PostFX: {post_label}",
            "impact": "Subsurface Scattering, Bloom, DoF & Motion Blur",
            "category": "Fidelity",
            "note": "Controls complex material shader permutations, ambient occlusion, and cinematic depth of field.",
            **shd_post_tl,
        },
        {
            "key": "sg.LandscapeQuality",
            "name": "Landscape & Terrain Tessellation Detail",
            "value": land_label,
            "impact": "Terrain Geometry Fidelity",
            "category": "Fidelity",
            "note": "Controls landscape LOD distribution and virtual heightfield mesh precision.",
            **land_tl,
        },
        {
            "key": "bUseHDRDisplayOutput / HDRDisplayOutputNits",
            "name": "HDR10 Display Output & Peak Luminance",
            "value": f"{'Enabled' if hdr_bool else 'Disabled (SDR)'} ({hdr_nits} nits)" if has_ini else "SDR Default (1000 nits)",
            "impact": "Dynamic Range & Color Gamut (Rec.2020)",
            "category": "Fidelity",
            "note": "Controls ACES HDR tonemapper output curve and peak nit clipping.",
            **hdr_tl,
        },
    ]

    # 5. Traffic-Light Comparison of PC Specs (or Simulated Reference Tier) vs. Current Unreal Settings
    quality_levels = [
        lvl for lvl in (view_dist_lvl, aa_lvl, shadow_lvl, gi_lvl, refl_lvl, post_lvl, tex_lvl, fx_lvl, fol_lvl, shade_lvl)
        if lvl is not None
    ]
    avg_quality = round(sum(quality_levels) / len(quality_levels), 2) if quality_levels else 3.0
    pixel_ratio = (res_x * res_y) / max(1, (1920 * 1080))
    est_vram_gb = round(3.8 + (pixel_ratio * 1.35) + (avg_quality * 1.15), 1)

    comparisons: list[dict[str, str]] = []

    # Comparison 1: GPU & VRAM vs Resolution & Textures
    if eval_vram_gb >= est_vram_gb + 2.5 and tex_tl["traffic_light"] == "green" and res_tl["traffic_light"] == "green":
        vram_light = "green"
        vram_state = "ok"
        vram_verdict = f"GREEN — OPTIMAL ({eval_vram_gb} GB vs ~{est_vram_gb} GB)"
        vram_advice = (
            f"Target profile [{ref_tier['tier_name']}] comfortably handles {res_x}x{res_y} with Textures at {tex_label} "
            f"(Recommended: {tex_tl['recommended_value']})."
        )
    elif eval_vram_gb < est_vram_gb or tex_tl["traffic_light"] == "red" or res_tl["traffic_light"] == "red":
        vram_light = "red"
        vram_state = "missing"
        vram_verdict = f"RED — VRAM OVER-BUDGET (~{est_vram_gb} GB vs {eval_vram_gb} GB)"
        vram_advice = (
            f"Current {res_x}x{res_y} + Textures at {tex_label} requires ~{est_vram_gb} GB VRAM, exceeding {ref_tier['tier_name']} ({eval_vram_gb} GB). "
            f"Recommended: Resolution {ref_tier['target_resolution']} and sg.TextureQuality = {tex_tl['recommended_value']}."
        )
    else:
        vram_light = "amber"
        vram_state = "pending"
        vram_verdict = f"AMBER — SUB-OPTIMAL ({eval_vram_gb} GB vs ~{est_vram_gb} GB)"
        vram_advice = f"{tex_tl['advice']} {res_tl['advice']}"

    comparisons.append(
        {
            "aspect": "1. GPU VRAM vs. Resolution & Texture Streaming Pool",
            "pc_spec": eval_hw_label,
            "game_setting": f"{res_x}x{res_y} | Textures: {tex_label} | Est. Pool: ~{est_vram_gb} GB",
            "recommended_setting": f"Res: {ref_tier['target_resolution']} | Textures: {tex_tl['recommended_value']}",
            "traffic_light": vram_light,
            "state": vram_state,
            "verdict": vram_verdict,
            "recommendation": vram_advice,
        }
    )

    # Comparison 2: GPU Compute Tier vs Lumen GI, Virtual Shadows & Reflections
    light_tls = [gi_tl["traffic_light"], shd_tl["traffic_light"], refl_tl["traffic_light"]]
    if "red" in light_tls:
        light_light = "red"
        light_state = "missing"
        light_verdict = "RED — LUMEN / VSM EXCEEDS GPU TIER"
        light_advice = f"{gi_tl['advice']} {shd_tl['advice']} {refl_tl['advice']}"
    elif "amber" in light_tls:
        light_light = "amber"
        light_state = "pending"
        light_verdict = "AMBER — SUB-OPTIMAL LIGHTING / SHADOWS"
        light_advice = f"{gi_tl['advice']} {shd_tl['advice']}"
    else:
        light_light = "green"
        light_state = "ok"
        light_verdict = "GREEN — OPTIMAL LUMEN GI & VSM SHADOWS"
        light_advice = f"GI ({gi_label}), Shadows ({shadow_label}), and Reflections ({refl_label}) match the reference array targets for {ref_tier['tier_name']}."

    comparisons.append(
        {
            "aspect": "2. GPU Architecture vs. Lumen GI, Virtual Shadows & Reflections",
            "pc_spec": f"{ref_tier['tier_name']} ({ref_tier['example_hardware']})",
            "game_setting": f"GI: {gi_label} | Shadows: {shadow_label} | Reflections: {refl_label}",
            "recommended_setting": (
                f"GI: {gi_tl['recommended_value']} | Shadows: {shd_tl['recommended_value']} | Reflections: {refl_tl['recommended_value']}"
            ),
            "traffic_light": light_light,
            "state": light_state,
            "verdict": light_verdict,
            "recommendation": light_advice,
        }
    )

    # Comparison 3: CPU Cores/Threads vs View Distance & Foliage Draw Calls
    cpu_tls = [vd_tl["traffic_light"], fol_tl["traffic_light"]]
    if "red" in cpu_tls or eval_cpu_cores < 6 and (view_dist_lvl or 3) >= 3:
        cpu_light = "red"
        cpu_state = "missing"
        cpu_verdict = "RED — DRAW-CALL / FOLIAGE OVERLOAD"
        cpu_advice = f"{vd_tl['advice']} {fol_tl['advice']}"
    elif "amber" in cpu_tls:
        cpu_light = "amber"
        cpu_state = "pending"
        cpu_verdict = "AMBER — SUB-OPTIMAL VIEW DISTANCE / FOLIAGE"
        cpu_advice = f"{vd_tl['advice']} {fol_tl['advice']}"
    else:
        cpu_light = "green"
        cpu_state = "ok"
        cpu_verdict = f"GREEN — OPTIMAL CPU DRAW-CALL BALANCE ({eval_cpu_cores}+ Cores)"
        cpu_advice = f"View Distance ({view_dist_label}) and Foliage ({fol_label}) match {ref_tier['tier_name']} targets."

    comparisons.append(
        {
            "aspect": "3. CPU Multi-Core Draw Calls vs. View Distance & Foliage",
            "pc_spec": f"{cpu_name} ({cpu_cores}C / {cpu_threads}T)" if not is_simulated_tier else f"{eval_cpu_cores}+ Core CPU ({ref_tier['tier_name']})",
            "game_setting": f"View Distance: {view_dist_label} | Foliage: {fol_label}",
            "recommended_setting": f"View Distance: {vd_tl['recommended_value']} | Foliage: {fol_tl['recommended_value']}",
            "traffic_light": cpu_light,
            "state": cpu_state,
            "verdict": cpu_verdict,
            "recommendation": cpu_advice,
        }
    )

    # Comparison 4: Display Native Resolution & Refresh Rate vs Output, Scale & VSync
    if scale_tl["traffic_light"] == "red" or res_tl["traffic_light"] == "red":
        disp_light = "red"
        disp_state = "missing"
        disp_verdict = "RED — RESOLUTION / 3D SCALE EXCEEDS TIER"
        disp_advice = f"{res_tl['advice']} {scale_tl['advice']}"
    elif scale_tl["traffic_light"] == "amber" or res_tl["traffic_light"] == "amber" or vsync_tl["traffic_light"] == "amber":
        disp_light = "amber"
        disp_state = "pending"
        disp_verdict = "AMBER — DISPLAY / VSYNC TUNING ADVISED"
        disp_advice = f"{res_tl['advice']} {vsync_tl['advice']}"
    else:
        disp_light = "green"
        disp_state = "ok"
        disp_verdict = f"GREEN — OPTIMAL DISPLAY & SCALING ({res_x}x{res_y})"
        disp_advice = f"Resolution ({res_x}x{res_y}), 3D Scale ({res_scale_label}), and Fullscreen mode match {ref_tier['tier_name']}."

    comparisons.append(
        {
            "aspect": "4. Display Resolution, 3D Render Scale & Sync",
            "pc_spec": f"{native_w}x{native_h} @ {native_hz} Hz Primary Display",
            "game_setting": f"{res_x}x{res_y} (Scale: {res_scale_label}) | VSync: {vsync_bool} | {fps_limit_label}",
            "recommended_setting": f"Res: {ref_tier['target_resolution']} | Scale: {ref_tier['recommended_res_scale']}% | DRS: {ref_tier['dynamic_resolution']}",
            "traffic_light": disp_light,
            "state": disp_state,
            "verdict": disp_verdict,
            "recommendation": disp_advice,
        }
    )

    # Comparison 5: System RAM vs World Partition & Shader Streaming
    if eval_ram_gb < 16.0 and avg_quality >= 2.5:
        ram_light = "red"
        ram_state = "missing"
        ram_verdict = f"RED — INSUFFICIENT RAM ({eval_ram_gb} GB)"
        ram_advice = "Unreal Engine 5 World Partition at High/Epic requires 16-32GB RAM to avoid severe pagefile hitches."
    elif eval_ram_gb < 24.0 and res_y >= 2160 and avg_quality >= 3.0:
        ram_light = "amber"
        ram_state = "pending"
        ram_verdict = f"AMBER — 16GB RAM TIGHT AT 4K EPIC"
        ram_advice = "16GB system RAM is borderline for 4K Epic World Partition streaming; close background browsers or use High (2) Textures."
    else:
        ram_light = "green"
        ram_state = "ok"
        ram_verdict = f"GREEN — OPTIMAL SYSTEM MEMORY ({eval_ram_gb} GB RAM)"
        ram_advice = f"{eval_ram_gb} GB RAM provides smooth World Partition asset and PSO shader cache streaming."

    comparisons.append(
        {
            "aspect": "5. System Memory (RAM) vs. World Partition & Shader Streaming",
            "pc_spec": f"{ram_gb} GB Total RAM ({pc_specs.get('ram', {}).get('available_gb', 0)} GB Free)" if not is_simulated_tier else f"{eval_ram_gb} GB System RAM ({ref_tier['tier_name']})",
            "game_setting": f"Avg Scalability Tier: {avg_quality:.1f} / 4.0 ({_UE_QUALITY_LABELS.get(round(avg_quality), 'Epic')})",
            "recommended_setting": f"Min {ref_tier['min_ram_gb']} GB RAM for {ref_tier['tier_name']}",
            "traffic_light": ram_light,
            "state": ram_state,
            "verdict": ram_verdict,
            "recommendation": ram_advice,
        }
    )

    all_evaluated = performance_settings + fidelity_settings
    green_cnt = sum(1 for item in all_evaluated if item.get("traffic_light") == "green")
    amber_cnt = sum(1 for item in all_evaluated if item.get("traffic_light") == "amber")
    red_cnt = sum(1 for item in all_evaluated if item.get("traffic_light") == "red")

    if not has_ini:
        overall_light = "amber"
        overall_state = "pending"
        overall_verdict = (
            f"AMBER — No Saved GameUserSettings.ini Found Yet for '{detected_project}' "
            f"(Showing Reference Targets for {ref_tier['tier_name']})"
        )
    elif red_cnt > 0 or any(c["traffic_light"] == "red" for c in comparisons):
        overall_light = "red"
        overall_state = "missing"
        overall_verdict = (
            f"RED (NOT OPTIMAL — {red_cnt} Critical Bottleneck(s), {amber_cnt} Warning(s)): "
            f"Current Settings Exceed Safe Limits for {ref_tier['tier_name']}"
        )
    elif amber_cnt > 2:
        overall_light = "amber"
        overall_state = "pending"
        overall_verdict = (
            f"AMBER (SUB-OPTIMAL — {green_cnt} Green, {amber_cnt} Amber): "
            f"Playable on {ref_tier['tier_name']}, with Tuning / Headroom Adjustments Advised"
        )
    else:
        overall_light = "green"
        overall_state = "ok"
        overall_verdict = (
            f"GREEN (OPTIMAL — {green_cnt}/{len(all_evaluated)} Settings Optimal"
            + (f", {amber_cnt} Minor Note" if amber_cnt else "")
            + f"): Current {res_x}x{res_y} {_UE_QUALITY_LABELS.get(round(avg_quality), 'Epic')} Config Ideally Matches {ref_tier['tier_name']}"
        )

    return {
        "detected_project_name": detected_project,
        "ini_found": has_ini,
        "ini_path": str(found_gus_path.resolve()) if found_gus_path else None,
        "engine_ini_path": str(found_engine_ini_path.resolve()) if found_engine_ini_path else None,
        "searched_locations": searched_locations[:6],
        "available_local_profiles": discover_local_unreal_ini_profiles(),
        "active_reference_tier_id": (reference_tier_id or "auto").strip().lower(),
        "active_reference_tier": ref_tier,
        "reference_array": UNREAL_HARDWARE_REFERENCE_ARRAY,
        "traffic_light_summary": {
            "overall_light": overall_light,
            "overall_state": overall_state,
            "overall_verdict": overall_verdict,
            "green_count": green_cnt,
            "amber_count": amber_cnt,
            "red_count": red_cnt,
            "total_settings": len(all_evaluated),
        },
        "summary_metrics": {
            "resolution": f"{res_x}x{res_y}",
            "fullscreen_mode": fs_mode_label,
            "vsync": vsync_bool,
            "frame_rate_limit": fps_limit_label,
            "average_scalability_level": avg_quality,
            "average_scalability_label": _UE_QUALITY_LABELS.get(round(avg_quality), f"Level {avg_quality}"),
            "estimated_vram_gb": est_vram_gb,
        },
        "performance_settings": performance_settings,
        "fidelity_settings": fidelity_settings,
        "spec_comparison": {
            "overall_light": overall_light,
            "overall_state": overall_state,
            "overall_verdict": overall_verdict,
            "estimated_vram_gb": est_vram_gb,
            "available_vram_gb": eval_vram_gb,
            "items": comparisons,
        },
        "raw_sections": sections,
    }


@router.post("/api/games/unreal-settings")
async def import_unreal_game_settings(req: ImportUnrealSettingsRequest) -> dict[str, Any]:
    """Imports or refreshes Unreal Engine GameUserSettings.ini / Engine.ini settings for a game and compares against PC Specs or a selected Reference Array tier."""
    store = load_managed_games()
    game_id = req.game_id.strip() or store.get("active_game_id", "battleground")
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Managed game '{game_id}' not found.")

    gdir = Path(str(target.get("game_dir", "")).strip())
    info = target.get("cached_modding_info") or inspect_game_modding_info(gdir, str(target.get("name", "")))
    tier_id = req.reference_tier_id.strip() or "auto"
    ue_settings = await asyncio.to_thread(
        inspect_unreal_game_settings,
        gdir,
        info,
        req.custom_ini_path.strip(),
        tier_id,
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
        "game": enrich_game_status(target),
    }


def _find_primary_game_exe(game_dir: Path, game_name: str = "") -> Path | None:
    if not game_dir.is_dir():
        return None
    skip_tokens = (
        "unins", "crash", "setup", "redist", "dxsetup", "vcredist",
        "unitycrashhandler", "cleanup", "touchup", "start_protected_game",
        "easyanticheat", "beservice",
    )
    candidates: list[Path] = []
    for pattern in ("*.exe", "*/*.exe", "*/*/*.exe", "*/*/*/*.exe"):
        for p in game_dir.glob(pattern):
            if p.is_file() and not any(tok in p.name.lower() for tok in skip_tokens):
                candidates.append(p)
        if candidates and pattern == "*.exe":
            break
    if not candidates:
        return None

    clean_name = "".join(ch for ch in (game_name or game_dir.name).lower() if ch.isalnum())

    def _score(p: Path) -> tuple[int, int, int]:
        stem = "".join(ch for ch in p.stem.lower() if ch.isalnum())
        name_match = 1 if (clean_name and (stem in clean_name or clean_name in stem)) else 0
        top_level = 1 if p.parent == game_dir else 0
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        return (name_match, top_level, size)

    candidates.sort(key=_score, reverse=True)
    return candidates[0]


@router.get("/api/games/{game_id}/icon")
async def get_game_icon(game_id: str):
    """Extracts and caches a 32x32 PNG icon from the game's primary executable (or .ico)."""
    safe_id = make_slug(game_id)
    cached_png = GAME_ICONS_DIR / f"{safe_id}.png"
    if cached_png.is_file() and cached_png.stat().st_size > 0:
        return FileResponse(cached_png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if not target:
        return Response(status_code=404)

    game_dir = Path(target.get("game_dir", ""))
    if not game_dir.is_dir():
        return Response(status_code=404)

    exe_path = _find_primary_game_exe(game_dir, target.get("name", ""))
    if exe_path is None:
        icos = list(game_dir.glob("*.ico")) + list(game_dir.glob("*/*.ico"))
        exe_path = icos[0] if icos else None
    if exe_path is None:
        return Response(status_code=404)

    ps_script = (
        "Add-Type -AssemblyName System.Drawing; "
        "$ico = [System.Drawing.Icon]::ExtractAssociatedIcon($env:MODTOOL_ICON_SRC); "
        "if ($ico -ne $null) { "
        "$bmp = $ico.ToBitmap(); "
        "$bmp.Save($env:MODTOOL_ICON_DST, [System.Drawing.Imaging.ImageFormat]::Png); "
        "$bmp.Dispose(); $ico.Dispose() "
        "}"
    )

    async with ICON_EXTRACT_SEMAPHORE:
        if not (cached_png.is_file() and cached_png.stat().st_size > 0):
            GAME_ICONS_DIR.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["MODTOOL_ICON_SRC"] = str(exe_path)
            env["MODTOOL_ICON_DST"] = str(cached_png)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-InputFormat", "None", "-Command", ps_script,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    if cached_png.is_file() and cached_png.stat().st_size > 0:
        return FileResponse(cached_png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)


def _find_steam_appid_and_assets(game_dir: Path) -> tuple[str | None, dict[str, Path]]:
    """Resolves Steam AppID from appmanifest_*.acf and locates local librarycache artwork (with disk+memory cache)."""
    import re

    if not game_dir or not game_dir.is_dir():
        return (None, {})

    cache_key = str(game_dir).lower()
    if not STEAM_ASSETS_CACHE and STEAM_ASSETS_CACHE_PATH.is_file():
        try:
            STEAM_ASSETS_CACHE.update(json.loads(STEAM_ASSETS_CACHE_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    if cache_key in STEAM_ASSETS_CACHE:
        entry = STEAM_ASSETS_CACHE[cache_key]
        cached_assets = {k: Path(v) for k, v in (entry.get("assets") or {}).items() if Path(v).is_file()}
        return (entry.get("appid"), cached_assets)

    steamapps_dir: Path | None = None
    for parent in [game_dir] + list(game_dir.parents):
        if parent.name.lower() == "steamapps" and parent.is_dir():
            steamapps_dir = parent
            break

    target_installdir = game_dir.name.lower()
    appid: str | None = None
    if steamapps_dir is not None:
        for acf in steamapps_dir.glob("appmanifest_*.acf"):
            try:
                txt = acf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            idir_m = re.search(r'"installdir"\s+"([^"]+)"', txt, re.IGNORECASE)
            if idir_m and idir_m.group(1).strip().lower() == target_installdir:
                aid_m = re.search(r'"appid"\s+"(\d+)"', txt, re.IGNORECASE)
                if aid_m:
                    appid = aid_m.group(1).strip()
                    break

    lc_dir: Path | None = None
    if appid and steamapps_dir is not None:
        steam_roots = [
            steamapps_dir.parent,
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        ]
        for root in steam_roots:
            cand = root / "appcache" / "librarycache" / appid
            if cand.is_dir():
                lc_dir = cand
                break

    assets: dict[str, Path] = {}
    if lc_dir is not None:
        def _pick(names: tuple[str, ...]) -> Path | None:
            for nm in names:
                direct = lc_dir / nm
                if direct.is_file():
                    return direct
                nested = next(lc_dir.rglob(nm), None)
                if nested and nested.is_file():
                    return nested
            return None

        hero = _pick(("library_hero.jpg", "library_hero_blur.jpg"))
        logo = _pick(("logo.png",))
        header = _pick(("header.jpg", "library_header.jpg"))
        capsule = _pick(("library_600x900.jpg", "library_capsule.jpg"))

        if hero:
            assets["hero"] = hero
        if logo:
            assets["logo"] = logo
        if header:
            assets["header"] = header
        if capsule:
            assets["capsule"] = capsule

    # Fallback / supplement: scan local game folder for splash, banner, logo, or cover art
    if len(assets) < 4:
        search_dirs = [
            game_dir,
            game_dir / "Assets",
            game_dir / "Content" / "Splash",
            game_dir / "Resources",
            game_dir / "Images",
            game_dir / "support",
        ]
        # Also check */Content/Splash for Unreal games
        for sub_splash in game_dir.glob("*/Content/Splash"):
            if sub_splash.is_dir():
                search_dirs.append(sub_splash)

        img_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        for sdir in search_dirs:
            if not sdir.is_dir():
                continue
            try:
                for p in sdir.iterdir():
                    if not p.is_file() or p.suffix.lower() not in img_exts:
                        continue
                    low = p.name.lower()
                    if "hero" not in assets and any(tok in low for tok in ("splash", "banner", "wide310x150", "hero")):
                        assets["hero"] = p
                    elif "header" not in assets and any(tok in low for tok in ("header", "wide310x150", "splash")):
                        assets["header"] = p
                    elif "logo" not in assets and any(tok in low for tok in ("logo", "storelogo")):
                        assets["logo"] = p
                    elif "capsule" not in assets and any(tok in low for tok in ("cover", "boxart", "square150x150", "capsule")):
                        assets["capsule"] = p
            except OSError:
                continue

    STEAM_ASSETS_CACHE[cache_key] = {
        "appid": appid,
        "assets": {k: str(v) for k, v in assets.items()},
    }
    try:
        STEAM_ASSETS_CACHE_PATH.write_text(json.dumps(STEAM_ASSETS_CACHE, indent=2), encoding="utf-8")
    except Exception:
        pass
    return (appid, assets)


@router.get("/api/games/{game_id}/steam-assets")
async def get_game_steam_assets(game_id: str) -> dict[str, Any]:
    """Returns available local Steam or game-folder artwork URLs (hero banner, logo, header, capsule, exe icon)."""
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
    """Serves a local Steam librarycache artwork image (hero, logo, header, capsule)."""
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


class ApplyUnrealSettingsRequest(BaseModel):
    game_id: str
    ini_path: str
    updates: dict[str, str] = Field(default_factory=dict)


@router.post("/api/unreal-settings/apply")
async def apply_unreal_ini_settings(req: ApplyUnrealSettingsRequest) -> dict[str, Any]:
    """Backs up the target Unreal GameUserSettings.ini and writes updated settings."""
    ini_file = Path(req.ini_path.strip())
    if not ini_file.is_file():
        raise HTTPException(status_code=404, detail=f"Unreal INI file not found: {ini_file}")

    bak_name = f"{make_slug(req.game_id)}_{ini_file.name}.bak"
    bak_path = UNREAL_INI_BACKUP_DIR / bak_name
    if not bak_path.is_file():
        shutil.copy2(ini_file, bak_path)

    lines = ini_file.read_text(encoding="utf-8", errors="replace").splitlines()
    remaining = {k.strip(): str(v).strip() for k, v in req.updates.items() if k.strip()}
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith(";"):
            k, _ = stripped.split("=", 1)
            k_clean = k.strip()
            if k_clean in remaining:
                new_lines.append(f"{k_clean}={remaining.pop(k_clean)}")
                continue
        new_lines.append(line)

    if remaining:
        new_lines.append("")
        new_lines.append("[ScalabilityGroups]")
        for k, v in remaining.items():
            new_lines.append(f"{k}={v}")

    ini_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    summary = f"Applied {len(req.updates)} Unreal INI setting(s) to {ini_file.name} (backup: {bak_path.name})"
    append_audit_log(action="unreal_settings.apply", status="ok", summary=summary, details=req.updates)
    return {"status": "ok", "summary": summary, "backup_path": str(bak_path), "ini_path": str(ini_file)}


@router.post("/api/unreal-settings/restore")
async def restore_unreal_ini_settings(req: ApplyUnrealSettingsRequest) -> dict[str, Any]:
    """Restores the backed-up Unreal GameUserSettings.ini."""
    ini_file = Path(req.ini_path.strip())
    bak_path = UNREAL_INI_BACKUP_DIR / f"{make_slug(req.game_id)}_{ini_file.name}.bak"
    if not bak_path.is_file():
        raise HTTPException(status_code=404, detail=f"No INI backup found at {bak_path}")
    shutil.copy2(bak_path, ini_file)
    summary = f"Restored {ini_file.name} from backup {bak_path.name}"
    append_audit_log(action="unreal_settings.restore", status="ok", summary=summary)
    return {"status": "ok", "summary": summary}


@router.get("/api/games/{game_id}/conflicts")
async def check_game_mod_conflicts(game_id: str) -> dict[str, Any]:
    """Scans BepInEx/plugins and Mods/ for duplicate DLLs/mods and stale interop hashes."""
    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Game '{game_id}' not found.")

    gdir = Path(str(target.get("game_dir", "")).strip())
    if not gdir.is_dir():
        return {"status": "ok", "issues": ["Game directory not found on disk."], "plugins": []}

    issues: list[str] = []
    installed_plugins: list[dict[str, Any]] = []
    seen_names: dict[str, list[str]] = {}

    plugins_dir = gdir / "BepInEx" / "plugins"
    if plugins_dir.is_dir():
        for dll in plugins_dir.rglob("*.dll"):
            rel = dll.relative_to(plugins_dir).as_posix()
            installed_plugins.append({"name": dll.name, "path": rel, "size_kb": round(dll.stat().st_size / 1024, 1)})
            seen_names.setdefault(dll.name.lower(), []).append(rel)

    for nm, paths in seen_names.items():
        if len(paths) > 1:
            issues.append(f"Duplicate plugin DLL '{nm}' found in: {', '.join(paths)}")

    # Check interop staleness
    interop_hash_file = gdir / "BepInEx" / "interop" / "assembly-hash.txt"
    ga_file = gdir / "GameAssembly.dll"
    if ga_file.is_file() and (gdir / "BepInEx" / "core").is_dir() and not interop_hash_file.is_file():
        issues.append("BepInEx\\interop\\assembly-hash.txt is missing — launch the game once to generate interop assemblies.")

    # Check Unreal Mods/
    for mods_dir in gdir.glob("*/Binaries/Win64/Mods"):
        if mods_dir.is_dir():
            for mod_sub in mods_dir.iterdir():
                if mod_sub.is_dir() and mod_sub.name.lower() != "shared":
                    enabled = (mod_sub / "enabled.txt").is_file()
                    installed_plugins.append({"name": mod_sub.name, "path": f"UE4SS/Mods/{mod_sub.name}", "enabled": enabled})

    # Check ReShade addons (*.addon64 / *.addon32 / *.addon)
    rs_dirs = [gdir] + [p for p in gdir.glob("*/Binaries/Win64") if p.is_dir()]
    for rdir in rs_dirs:
        for ext in ("*.addon64", "*.addon32", "*.addon"):
            for addon in rdir.glob(ext):
                if addon.is_file():
                    installed_plugins.append({"name": addon.name, "path": f"ReShade/{addon.name}", "size_kb": round(addon.stat().st_size / 1024, 1)})

    return {
        "status": "ok",
        "game_id": game_id,
        "plugin_count": len(installed_plugins),
        "issue_count": len(issues),
        "issues": issues,
        "plugins": installed_plugins,
    }


@router.post("/api/games/{game_id}/launch")
async def launch_game_process(game_id: str) -> dict[str, Any]:
    """Launches the game via Steam URI (if Steam AppID known) or its primary executable."""
    import subprocess

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


class GameNotesRequest(BaseModel):
    favorite: bool | None = None
    tags: list[str] | None = None
    user_notes: str | None = None


@router.post("/api/games/{game_id}/notes")
async def save_game_notes(game_id: str, req: GameNotesRequest) -> dict[str, Any]:
    """Saves per-game favorite flag, custom tags, and notes."""
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
    return {"status": "ok", "game": enrich_game_status(target)}


@router.post("/api/games/prewarm-icons")
async def prewarm_game_icons() -> dict[str, Any]:
    """Extracts and caches all missing game .exe icons in a single batched PowerShell call."""
    import subprocess

    store = load_managed_games()
    pairs: list[tuple[str, str]] = []
    for g in store.get("games", []):
        safe_id = make_slug(g.get("id", ""))
        cached_png = GAME_ICONS_DIR / f"{safe_id}.png"
        if cached_png.is_file() and cached_png.stat().st_size > 0:
            continue
        exe = _find_primary_game_exe(Path(str(g.get("game_dir", ""))), str(g.get("name", "")))
        if exe and exe.is_file():
            pairs.append((str(exe), str(cached_png)))

    if not pairs:
        return {"status": "ok", "extracted": 0, "summary": "All game icons already cached."}

    manifest_tmp = GAME_ICONS_DIR / "_batch_icons.json"
    manifest_tmp.write_text(json.dumps([{"src": s, "dst": d} for s, d in pairs]), encoding="utf-8")
    ps = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$items = Get-Content -Raw -Path '{str(manifest_tmp)}' | ConvertFrom-Json; "
        "foreach ($it in $items) { try { "
        "$ico = [System.Drawing.Icon]::ExtractAssociatedIcon($it.src); "
        "if ($ico -ne $null) { $b = $ico.ToBitmap(); $b.Save($it.dst, [System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose(); $ico.Dispose() } "
        "} catch {} }"
    )
    await asyncio.to_thread(
        subprocess.run,
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-InputFormat", "None", "-Command", ps],
        capture_output=True,
        timeout=60,
    )
    manifest_tmp.unlink(missing_ok=True)
    extracted = sum(1 for _, dst in pairs if Path(dst).is_file())
    return {"status": "ok", "extracted": extracted, "total_queued": len(pairs), "summary": f"Pre-warmed {extracted} game icon(s)."}


def load_scan_exclusions() -> set[str]:
    if SCAN_EXCLUSIONS_PATH.is_file():
        try:
            data = json.loads(SCAN_EXCLUSIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x).strip().lower() for x in data if str(x).strip()}
        except Exception:
            pass
    return set()


def save_scan_exclusions(paths: set[str]) -> None:
    SCAN_EXCLUSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCAN_EXCLUSIONS_PATH.write_text(json.dumps(sorted(paths), indent=2), encoding="utf-8")


@router.post("/api/games/prune-non-games")
async def prune_non_games() -> dict[str, Any]:
    """Removes duplicate Xbox Content/root entries and non-game DLC/utility entries from managed_games.json."""
    store = load_managed_games()
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    seen_dirs: set[str] = set()

    for g in store.get("games", []):
        if g.get("id") == "battleground":
            kept.append(g)
            continue
        gdir = Path(str(g.get("game_dir", "")).strip())
        norm = str(gdir).lower()
        if norm in seen_dirs:
            removed.append(g.get("name", g.get("id")))
            continue
        ok, _ = is_game_candidate(gdir, discovery_scope="all")
        if not ok:
            removed.append(g.get("name", g.get("id")))
            continue
        seen_dirs.add(norm)
        kept.append(g)

    store["games"] = kept
    save_managed_games(store)
    summary = f"Pruned {len(removed)} non-game or duplicate entry(s)."
    append_audit_log(action="game_manager.prune_non_games", status="ok", summary=summary, details={"removed": removed})
    return {"status": "ok", "summary": summary, "removed": removed, "remaining_count": len(kept)}




ENGINE_ADAPTER_REGISTRY: dict[str, str] = {
    "Unity_IL2CPP": "bepinex-mcp",
    "Unity_Mono": "bepinex-mcp",
    "Unreal_4_5": "ue4ss-mcp",
    "Creation_Engine": "creation-kit-mcp",
    "Godot": "godot-mcp",
    "Data_Driven": "filesystem-mcp",
    "Unknown": "unknown",
}

LIBRARY_ANTI_CHEAT_MARKERS: tuple[str, ...] = (
    "easyanticheat.exe",
    "easyanticheat_eos.exe",
    "beservice.exe",
    "vgk.sys",
    "gameguard.des",
)


def _normalize_library_title(title: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "", str(title or "").lower())


def _canonical_entry_id(source: str, external_id: str = "", root_path: str = "", title: str = "") -> str:
    src = (source or "manual").strip().lower()
    if external_id:
        return f"{src}:{external_id.strip()}"
    seed = (root_path or title or "entry").strip().lower()
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"{src}:{digest}"


def load_library_cache() -> dict[str, Any]:
    default_data: dict[str, Any] = {
        "entries": [],
        "custom_roots": [],
        "pcgw_cache": {},
        "last_scan_at": None,
        "last_import_at": None,
    }
    if LIBRARY_CACHE_PATH.is_file():
        try:
            raw = json.loads(LIBRARY_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                default_data["entries"] = raw.get("entries", []) if isinstance(raw.get("entries"), list) else []
                default_data["custom_roots"] = raw.get("custom_roots", []) if isinstance(raw.get("custom_roots"), list) else []
                default_data["pcgw_cache"] = raw.get("pcgw_cache", {}) if isinstance(raw.get("pcgw_cache"), dict) else {}
                default_data["last_scan_at"] = raw.get("last_scan_at")
                default_data["last_import_at"] = raw.get("last_import_at")
        except Exception:
            pass
    return default_data


def save_library_cache(cache: dict[str, Any]) -> None:
    LIBRARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def fingerprint_library_entry(root_path: Path) -> dict[str, Any]:
    """Evaluates anti-cheat gate first, then multi-engine disk signatures, returning normalized fields."""
    result: dict[str, Any] = {
        "engine": "Unknown",
        "detection_confidence": "verified_disk",
        "anti_cheat_status": "Clean",
        "recommended_adapter": "unknown",
    }
    if not root_path.is_dir():
        return result

    # 1. Anti-Cheat Gate (First)
    ac_found = False
    try:
        for item in root_path.rglob("*"):
            name_low = item.name.lower()
            if name_low in LIBRARY_ANTI_CHEAT_MARKERS or "nprotect" in name_low or name_low == "gamemon.des":
                ac_found = True
                break
    except Exception:
        pass
    if ac_found:
        result["anti_cheat_status"] = "Restricted"
        result["recommended_adapter"] = "blocked"

    # 2. Engine Signatures (in exact priority order)
    engine = "Unknown"
    has_game_assembly = (root_path / "GameAssembly.dll").is_file()
    has_global_metadata = any(root_path.glob("*_Data/il2cpp_data/Metadata/global-metadata.dat")) or any(
        root_path.rglob("global-metadata.dat")
    )
    if (root_path / "Engine" / "Binaries" / "Win64").is_dir() or any(root_path.rglob("*.pak")) or any(root_path.rglob("*.utoc")) or any(root_path.rglob("*.ucas")) or any(root_path.rglob("*-Win64-Shipping.exe")):
        engine = "Unreal_4_5"
    elif has_game_assembly and has_global_metadata:
        engine = "Unity_IL2CPP"
    elif any(root_path.glob("*_Data/Managed/Assembly-CSharp.dll")):
        engine = "Unity_Mono"
    elif any(root_path.glob("Data/*.esm")) or any(root_path.glob("Data/*.esp")):
        engine = "Creation_Engine"
    elif any(root_path.glob("*.pck")) and any(root_path.glob("*.exe")):
        engine = "Godot"
    else:
        has_data_dir = (root_path / "config").is_dir() or any(
            p.suffix.lower() in (".json", ".xml", ".lua") for p in root_path.iterdir() if p.is_file()
        )
        if has_data_dir:
            engine = "Data_Driven"

    result["engine"] = engine
    if result["anti_cheat_status"] != "Restricted":
        result["recommended_adapter"] = ENGINE_ADAPTER_REGISTRY.get(engine, "unknown")
    return result


def _parse_vdf_keyvalues(text: str) -> list[tuple[str, str]]:
    import re
    return re.findall(r'"([^"]+)"\s+"([^"]*)"', text)


def _locate_steam_root() -> Path | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            val, _ = winreg.QueryValueEx(k, "SteamPath")
            if val and Path(val).is_dir():
                return Path(val)
    except Exception:
        pass
    fallback = Path(r"C:\Program Files (x86)\Steam")
    return fallback if fallback.is_dir() else None


def scan_steam_installed() -> list[dict[str, Any]]:
    """Steam Connector: reads libraryfolders.vdf and appmanifest_*.acf where StateFlags == 4."""
    import re
    entries: list[dict[str, Any]] = []
    steam_root = _locate_steam_root()
    if not steam_root:
        return entries

    lib_paths: list[Path] = [steam_root]
    vdf_path = steam_root / "steamapps" / "libraryfolders.vdf"
    if vdf_path.is_file():
        try:
            vdf_text = vdf_path.read_text(encoding="utf-8", errors="replace")
            for k, v in _parse_vdf_keyvalues(vdf_text):
                if k.lower() == "path" and v:
                    p = Path(v.replace("\\\\", "\\"))
                    if p.is_dir() and p not in lib_paths:
                        lib_paths.append(p)
        except Exception:
            pass

    for lib in lib_paths:
        steamapps = lib / "steamapps"
        if not steamapps.is_dir():
            continue
        for acf in sorted(steamapps.glob("appmanifest_*.acf")):
            try:
                txt = acf.read_text(encoding="utf-8", errors="replace")
                kv = {k.lower(): v for k, v in _parse_vdf_keyvalues(txt)}
                if kv.get("stateflags") != "4":
                    continue
                appid = kv.get("appid", "").strip()
                name = kv.get("name", "").strip()
                installdir = kv.get("installdir", "").strip()
                if not installdir:
                    continue
                gdir = steamapps / "common" / installdir
                if not gdir.is_dir():
                    continue
                if re.search(r"\b(steamworks|redistributable|proton|dedicated server|soundtrack)\b", name, re.IGNORECASE):
                    continue
                entries.append({
                    "id": _canonical_entry_id("steam", appid, str(gdir), name),
                    "title": name or installdir,
                    "source_platform": "steam",
                    "external_ids": {"steam_appid": appid} if appid else {},
                    "install_state": "installed",
                    "root_path": str(gdir),
                })
            except Exception:
                continue
    return entries


def scan_epic_installed() -> list[dict[str, Any]]:
    """Epic Games Connector: reads *.item JSON manifests where bIsIncompleteInstall is false."""
    entries: list[dict[str, Any]] = []
    manifest_dir = Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests")
    if not manifest_dir.is_dir():
        return entries
    for item_file in sorted(manifest_dir.glob("*.item")):
        try:
            data = json.loads(item_file.read_text(encoding="utf-8", errors="replace"))
            if data.get("bIsIncompleteInstall"):
                continue
            install_loc = str(data.get("InstallLocation") or "").strip()
            display_name = str(data.get("DisplayName") or "").strip()
            cat_id = str(data.get("CatalogItemId") or "").strip()
            if not install_loc or not Path(install_loc).is_dir():
                continue
            entries.append({
                "id": _canonical_entry_id("epic", cat_id, install_loc, display_name),
                "title": display_name or Path(install_loc).name,
                "source_platform": "epic",
                "external_ids": {"epic_id": cat_id} if cat_id else {},
                "install_state": "installed",
                "root_path": install_loc,
            })
        except Exception:
            continue
    return entries


def scan_gog_installed() -> list[dict[str, Any]]:
    """GOG Galaxy Connector: reads installed entries from galaxy-2.0.db (read-only uri=True) and WOW6432Node registry."""
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    db_path = Path(r"C:\ProgramData\GOG.com\Galaxy\storage\galaxy-2.0.db")
    if db_path.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT ib.productId, ib.installationPath, gp.title "
                    "FROM InstalledBaseProducts ib "
                    "LEFT JOIN GamePieces gp ON gp.releaseKey = ('gog_' || ib.productId) "
                    "WHERE ib.installationPath IS NOT NULL"
                )
                for prod_id, inst_path, title in cur.fetchall():
                    p = Path(str(inst_path or "").strip())
                    if not p.is_dir():
                        continue
                    norm = str(p).lower()
                    if norm in seen_paths:
                        continue
                    seen_paths.add(norm)
                    pid = str(prod_id or "").strip()
                    t = str(title or "").strip()
                    if t.startswith("{"):
                        try:
                            t = json.loads(t).get("title") or p.name
                        except Exception:
                            t = p.name
                    entries.append({
                        "id": _canonical_entry_id("gog", pid, str(p), t),
                        "title": t or p.name,
                        "source_platform": "gog",
                        "external_ids": {"gog_id": pid} if pid else {},
                        "install_state": "installed",
                        "root_path": str(p),
                    })
            finally:
                conn.close()
        except Exception:
            pass

    # Fallback / supplement: Windows Registry HKLM\SOFTWARE\WOW6432Node\GOG.com\Games\*
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games") as root_k:
            idx = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root_k, idx)
                    idx += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(root_k, sub_name) as gk:
                        path_val, _ = winreg.QueryValueEx(gk, "path")
                        name_val = ""
                        try:
                            name_val, _ = winreg.QueryValueEx(gk, "gameName")
                        except OSError:
                            pass
                        p = Path(str(path_val or "").strip())
                        if p.is_dir() and str(p).lower() not in seen_paths:
                            seen_paths.add(str(p).lower())
                            entries.append({
                                "id": _canonical_entry_id("gog", sub_name, str(p), str(name_val)),
                                "title": str(name_val or p.name).strip(),
                                "source_platform": "gog",
                                "external_ids": {"gog_id": sub_name},
                                "install_state": "installed",
                                "root_path": str(p),
                            })
                except OSError:
                    continue
    except OSError:
        pass
    return entries


def scan_manual_folders(custom_roots: list[str], single_folder: str = "") -> list[dict[str, Any]]:
    """Custom Root / Manual Folder Connector for standalone/itch.io/DRM-free installs."""
    entries: list[dict[str, Any]] = []
    if single_folder:
        p = Path(single_folder.strip())
        if p.is_dir():
            entries.append({
                "id": _canonical_entry_id("manual", "", str(p), p.name),
                "title": p.name,
                "source_platform": "manual",
                "external_ids": {},
                "install_state": "installed",
                "root_path": str(p),
            })

    for raw_root in custom_roots:
        r = Path(str(raw_root or "").strip())
        if not r.is_dir():
            continue
        # If the root itself looks like a single game folder (has executables or engine markers), add it directly; otherwise scan immediate children
        has_exe = any(r.glob("*.exe"))
        if has_exe:
            entries.append({
                "id": _canonical_entry_id("manual", "", str(r), r.name),
                "title": r.name,
                "source_platform": "manual",
                "external_ids": {},
                "install_state": "installed",
                "root_path": str(r),
            })
        else:
            for child in sorted(r.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    if any(child.glob("*.exe")) or any(child.glob("*_Data")) or (child / "Engine").is_dir():
                        entries.append({
                            "id": _canonical_entry_id("manual", "", str(child), child.name),
                            "title": child.name,
                            "source_platform": "manual",
                            "external_ids": {},
                            "install_state": "installed",
                            "root_path": str(child),
                        })
    return entries


def _map_pcgw_to_engine_and_ac(engine_text: str, ac_text: str) -> tuple[str, str, str]:
    el = (engine_text or "").lower()
    acl = (ac_text or "").lower()
    if any(k in acl for k in ("easy anti-cheat", "battleye", "vanguard", "gameguard", "nprotect", "ricochet")):
        ac_status = "Restricted"
    else:
        ac_status = "Clean"

    if "unreal" in el:
        eng = "Unreal_4_5"
    elif "unity" in el:
        eng = "Unity_Mono"
    elif "creation" in el or "gamebryo" in el:
        eng = "Creation_Engine"
    elif "godot" in el:
        eng = "Godot"
    else:
        eng = "Unknown"

    adapter = "blocked" if ac_status == "Restricted" else ENGINE_ADAPTER_REGISTRY.get(eng, "unknown")
    return eng, ac_status, adapter


def enrich_uninstalled_with_pcgw(
    entries: list[dict[str, Any]],
    pcgw_cache: dict[str, Any],
    max_live_queries: int = 12,
) -> list[dict[str, Any]]:
    """Queries PCGamingWiki Cargo API for owned_uninstalled entries with polite rate limiting & offline cache."""
    import time
    import urllib.parse
    import urllib.request

    live_count = 0
    for entry in entries:
        if entry.get("install_state") != "owned_uninstalled":
            continue
        ext_ids = entry.get("external_ids") or {}
        steam_appid = str(ext_ids.get("steam_appid") or "").strip()
        title = str(entry.get("title") or "").strip()
        cache_key = f"steam:{steam_appid}" if steam_appid else f"title:{_normalize_library_title(title)}"

        cached = pcgw_cache.get(cache_key)
        if not cached and live_count < max_live_queries and (steam_appid or title):
            try:
                if live_count > 0:
                    time.sleep(0.25)
                if steam_appid:
                    where = f'Infobox_game.Steam_AppID HOLDS "{steam_appid}"'
                else:
                    safe_title = title.replace('"', '\\"')
                    where = f'Infobox_game._pageName="{safe_title}"'
                params = urllib.parse.urlencode({
                    "action": "cargoquery",
                    "tables": "Infobox_game",
                    "fields": "Infobox_game._pageName=Page,Infobox_game.Engines=Engines,Infobox_game.Anti_cheat=AntiCheat",
                    "where": where,
                    "limit": "1",
                    "format": "json",
                })
                url = f"https://www.pcgamingwiki.com/w/api.php?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "ModToolPoC/1.0 (Local Game Library)"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                rows = payload.get("cargoquery", [])
                if rows and isinstance(rows[0], dict):
                    t = rows[0].get("title", {})
                    cached = {
                        "engines": str(t.get("Engines") or ""),
                        "anti_cheat": str(t.get("AntiCheat") or ""),
                        "page": str(t.get("Page") or title),
                    }
                else:
                    cached = {"engines": "", "anti_cheat": "", "page": title}
                pcgw_cache[cache_key] = cached
                live_count += 1
            except Exception:
                cached = {"engines": "", "anti_cheat": "", "page": title}

        eng, ac_status, adapter = _map_pcgw_to_engine_and_ac(
            (cached or {}).get("engines", ""),
            (cached or {}).get("anti_cheat", ""),
        )
        entry["engine"] = eng
        entry["anti_cheat_status"] = ac_status
        entry["recommended_adapter"] = adapter
        entry["detection_confidence"] = "estimated_metadata"
    return entries


def _fetch_steam_owned_titles(steam_id_override: str = "", api_key_override: str = "") -> list[dict[str, Any]]:
    import re
    import urllib.request
    import xml.etree.ElementTree as ET

    steam_id = steam_id_override.strip()
    if not steam_id:
        steam_root = _locate_steam_root()
        if steam_root:
            login_vdf = steam_root / "config" / "loginusers.vdf"
            if login_vdf.is_file():
                try:
                    txt = login_vdf.read_text(encoding="utf-8", errors="replace")
                    blocks = re.findall(r'"(\d{17})"\s*\{([^}]*)\}', txt)
                    for sid, body in blocks:
                        kv = {k.lower(): v for k, v in _parse_vdf_keyvalues(body)}
                        if kv.get("mostrecent") == "1":
                            steam_id = sid
                            break
                    if not steam_id and blocks:
                        steam_id = blocks[0][0]
                except Exception:
                    pass
    if not steam_id:
        return []

    results: list[dict[str, Any]] = []
    if api_key_override.strip():
        try:
            url = (
                "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
                f"?key={api_key_override.strip()}&steamid={steam_id}&include_appinfo=1&format=json"
            )
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for g in data.get("response", {}).get("games", []):
                appid = str(g.get("appid") or "").strip()
                name = str(g.get("name") or "").strip()
                if appid and name:
                    results.append({
                        "id": _canonical_entry_id("steam", appid, "", name),
                        "title": name,
                        "source_platform": "steam",
                        "external_ids": {"steam_appid": appid},
                        "install_state": "owned_uninstalled",
                        "root_path": None,
                    })
            return results
        except Exception:
            pass

    try:
        xml_url = f"https://steamcommunity.com/profiles/{steam_id}/games/?tab=all&xml=1"
        req = urllib.request.Request(xml_url, headers={"User-Agent": "ModToolPoC/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            xml_bytes = resp.read()
        root = ET.fromstring(xml_bytes)
        for game_el in root.findall(".//game"):
            appid = (game_el.findtext("appID") or "").strip()
            name = (game_el.findtext("name") or "").strip()
            if appid and name:
                results.append({
                    "id": _canonical_entry_id("steam", appid, "", name),
                    "title": name,
                    "source_platform": "steam",
                    "external_ids": {"steam_appid": appid},
                    "install_state": "owned_uninstalled",
                    "root_path": None,
                })
    except Exception:
        pass
    return results


def _fetch_gog_owned_titles() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    db_path = Path(r"C:\ProgramData\GOG.com\Galaxy\storage\galaxy-2.0.db")
    if not db_path.is_file():
        return entries
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT releaseKey, value FROM GamePieces WHERE releaseKey LIKE 'gog_%' LIMIT 500")
            for rel_key, val in cur.fetchall():
                pid = str(rel_key).replace("gog_", "", 1)
                title = ""
                if isinstance(val, str) and val.startswith("{"):
                    try:
                        parsed = json.loads(val)
                        title = str(parsed.get("title") or "").strip()
                    except Exception:
                        pass
                if title:
                    entries.append({
                        "id": _canonical_entry_id("gog", pid, "", title),
                        "title": title,
                        "source_platform": "gog",
                        "external_ids": {"gog_id": pid},
                        "install_state": "owned_uninstalled",
                        "root_path": None,
                    })
        finally:
            conn.close()
    except Exception:
        pass
    return entries


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

    # Deduplicate by normalized root_path and fingerprint on disk
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

    # Preserve existing owned_uninstalled entries not already installed
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
        details={
            "installed_count": len(deduped_installed),
            "custom_roots": custom_roots,
        },
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

    # Merge with existing owned_uninstalled
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
    import csv
    import io

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


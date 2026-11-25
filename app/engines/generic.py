"""Generic / non-Unity / non-Unreal engine detection, anti-cheat gate, overlay & mod scanning."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.core.constants import (
    ANTI_CHEAT_BINARIES,
    ENGINE_ADAPTER_REGISTRY,
    LIBRARY_ANTI_CHEAT_MARKERS,
)
from app.engines.base import EngineAdapter
from app.engines.unity import scan_unity_mods
from app.engines.unreal import scan_unreal_mods


def clean_nexus_package_name(raw_name: str) -> str:
    """Normalize Vortex/Nexus subfolder names like 'AAA-Better Inventory-467-1-1-1674452260'."""
    name = raw_name.strip()
    if name.startswith("AAA-"):
        name = name[4:]
    cleaned = re.sub(r"(?:\.zip|\.7z|\.rar)?-\d+(?:-\d+){1,4}-\d{8,}$", "", name, flags=re.IGNORECASE)
    return cleaned.strip(" -_") or raw_name


def check_anticheat_binaries(game_dir: Path) -> list[str]:
    """Return any known anti-cheat binary filenames detected under *game_dir*."""
    detected: list[str] = []
    if not game_dir.is_dir():
        return detected
    try:
        for _root, _dirs, files in os.walk(game_dir):
            for fname in files:
                if fname.lower() in ANTI_CHEAT_BINARIES:
                    detected.append(fname)
    except Exception:
        pass
    return detected


def detect_normalized_engine(root_path: Path, fp_engine: str = "") -> str:
    """Detect engine key prioritizing Unreal markers before generic DLL/Mono checks."""
    if root_path.is_dir():
        has_ue_dir = (root_path / "Engine" / "Binaries" / "Win64").is_dir()
        has_ue_pak = (
            any(root_path.rglob("*.pak"))
            or any(root_path.rglob("*.utoc"))
            or any(root_path.rglob("*.ucas"))
        )
        has_ue_shipping = any(root_path.rglob("*-Win64-Shipping.exe"))
        if has_ue_dir or has_ue_pak or has_ue_shipping:
            return "unreal"
        if (root_path / "GameAssembly.dll").is_file() or any(root_path.rglob("global-metadata.dat")):
            return "unity_il2cpp"
        if any(root_path.glob("*_Data/Managed/Assembly-CSharp.dll")):
            return "unity_mono"
        if any(root_path.glob("*.pck")) and any(root_path.glob("*.exe")):
            return "godot"
        if any(root_path.glob("Data/*.esm")) or any(root_path.glob("Data/*.esp")):
            return "creation_engine"

    low = (fp_engine or "").strip().lower()
    if "unreal" in low:
        return "unreal"
    if "il2cpp" in low:
        return "unity_il2cpp"
    if "mono" in low:
        return "unity_mono"
    if "godot" in low:
        return "godot"
    if "creation" in low:
        return "creation_engine"
    return "generic"


def fingerprint_library_entry(root_path: Path) -> dict[str, Any]:
    """Evaluate anti-cheat gate first, then multi-engine disk signatures."""
    result: dict[str, Any] = {
        "engine": "Unknown",
        "detection_confidence": "verified_disk",
        "anti_cheat_status": "Clean",
        "recommended_adapter": "unknown",
    }
    if not root_path.is_dir():
        return result

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

    engine = "Unknown"
    has_game_assembly = (root_path / "GameAssembly.dll").is_file()
    has_global_metadata = any(root_path.glob("*_Data/il2cpp_data/Metadata/global-metadata.dat")) or any(
        root_path.rglob("global-metadata.dat")
    )
    if (
        (root_path / "Engine" / "Binaries" / "Win64").is_dir()
        or any(root_path.rglob("*.pak"))
        or any(root_path.rglob("*.utoc"))
        or any(root_path.rglob("*.ucas"))
        or any(root_path.rglob("*-Win64-Shipping.exe"))
    ):
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


def detect_reshade_info(game_dir: Path, primary_exe: Path | None = None) -> dict[str, Any]:
    """Detect ReShade, addons (*.addon64/*.addon32/*.addon), and shader directories."""
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
    return rs_info


def scan_installed_mods(
    game_dir: Path,
    primary_exe: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Scan engine- and overlay-specific folders (~mods, LogicMods, UE4SS, BepInEx, MelonLoader, ReShade, Mods/)."""
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

    # 1. Unreal & UE4SS mods
    scan_unreal_mods(game_dir, _add_mod, folders_scanned, managed_by, _rel, clean_nexus_package_name)

    # 2. Unity BepInEx & MelonLoader mods
    scan_unity_mods(game_dir, _add_mod, folders_scanned, _rel)

    # 3. Graphics, Upscaler & Overlay Mods
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

    # 4. Generic top-level Mods / Plugins / Addons folders
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
            elif item.is_file() and item.suffix.lower() in (
                ".pak", ".dll", ".asi", ".jar", ".zip", ".lua", ".esp", ".esm", ".esl"
            ):
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


def build_generic_crucial_steps(
    info: dict[str, Any],
    *,
    ac_clear: bool,
    binaries_ok: bool,
) -> list[dict[str, str]]:
    """Build crucial modding checklist items for Godot, Creation, FromSoftware, RE Engine, or Native C++."""
    return [
        {
            "step": "1. Anti-Cheat & Safety Check",
            "state": "ok" if ac_clear else "blocked",
            "label": "Clear (Safe to Mod)" if ac_clear else "Blocked (Anti-Cheat / Live-Service)",
            "detail": "Zero anti-cheat drivers detected" if ac_clear else str(info.get("discovery_mcp_tools", "")),
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
            "label": str(info.get("required_modding_tool") or "External Tooling"),
            "detail": str(info.get("recommended_mcp_stack") or "See Recommended Modding Apps below"),
        },
        {
            "step": "4. Recommended MCP / Tooling",
            "state": "na",
            "label": str(info.get("required_bridge_plugin") or "External Tooling"),
            "detail": str(info.get("modding_mcp_tools") or "Config / Data Overrides"),
        },
    ]


class GenericEngineAdapter(EngineAdapter):
    """Fallback adapter for Godot, Creation Engine, Data-Driven, and Native C++ titles."""

    engine_id = "generic"
    display_name = "Generic / Data-Driven / Native C++"

    def detect(self, game_dir: Path) -> bool:
        return game_dir.is_dir()

    def fingerprint(self, game_dir: Path) -> dict[str, Any]:
        return fingerprint_library_entry(game_dir)

    def discover_configs(self, game_dir: Path, **kwargs: Any) -> dict[str, Any]:
        if not game_dir.is_dir():
            return {"engine": "generic", "config_files": []}
        cfg_files = [
            str(p)
            for p in sorted(game_dir.iterdir())
            if p.is_file() and p.suffix.lower() in (".ini", ".json", ".xml", ".cfg", ".conf", ".lua")
        ]
        return {"engine": "generic", "config_files": cfg_files[:30]}

    def check_readiness(self, game_dir: Path, info: dict[str, Any] | None = None) -> dict[str, Any]:
        fp = fingerprint_library_entry(game_dir)
        return {
            "ready": fp.get("anti_cheat_status") != "Restricted",
            "modding_readiness": (
                "Blocked (Anti-Cheat)" if fp.get("anti_cheat_status") == "Restricted" else "External Tooling"
            ),
            **fp,
        }

    def get_log_paths(self, game_dir: Path) -> list[Path]:
        if not game_dir.is_dir():
            return []
        return [p for p in sorted(game_dir.glob("*.log")) if p.is_file()][:10]

"""Engine-agnostic traffic-light asset analyzer and moddability guide."""

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
from app.routers.library import fingerprint_library_entry
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

ENGINE_TOOL_MATRIX: dict[str, dict[str, list[str]]] = {
    "unity_il2cpp": {
        "allowed_mcp_bridges": ["bepinex-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["BepInEx (IL2CPP)", "Il2CppDumper", "AssetRipper"],
    },
    "unity_mono": {
        "allowed_mcp_bridges": ["bepinex-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["BepInEx (Mono)", "ILSpy", "AssetRipper"],
    },
    "unreal": {
        "allowed_mcp_bridges": ["ue4ss-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["UE4SS", "FModel", "UnrealPak"],
    },
    "godot": {
        "allowed_mcp_bridges": ["godot-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["GDRE Tools"],
    },
    "creation_engine": {
        "allowed_mcp_bridges": ["creation-kit-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["xEdit", "BAE"],
    },
    "generic": {
        "allowed_mcp_bridges": ["filesystem-mcp"],
        "allowed_loaders_and_cli": [],
    },
}

ASSET_AUDITS_DIR = get_app_data_root() / "asset_audits"
ASSET_AUDITS_DIR.mkdir(parents=True, exist_ok=True)

MODDABILITY_GUIDE: dict[str, dict[str, str]] = {
    "anti_cheat": {
        "key": "anti_cheat",
        "title": "Kernel / User-Mode Anti-Cheat Gate",
        "severity": "red",
        "summary": "Games protected by EasyAntiCheat, BattlEye, Vanguard, GameGuard, nProtect, or Denuvo actively block DLL injection, memory hooks, and modified archives.",
        "why_it_blocks": "Loading unsigned DLLs or altered game files triggers tamper detection, blocks launch, or risks online account bans.",
        "what_you_can_do_instead": "Do not inject runtime mods or alter signed game files on protected titles. Stick to official in-game settings or offline-only single-player titles.",
    },
    "encrypted_archives": {
        "key": "encrypted_archives",
        "title": "AES-Encrypted or Proprietary Archives",
        "severity": "red",
        "summary": "Game assets are stored in encrypted `.pak` / `.utoc` / `.archive` containers whose index or payload cannot be read without a decryption key.",
        "why_it_blocks": "Without the AES key, extraction and repacking tools cannot read the file table or rebuild a valid archive.",
        "what_you_can_do_instead": "Check if loose plaintext configs (`.ini`, `.json`) or runtime memory/UE4SS hooks are still accessible outside the encrypted archive.",
    },
    "signed_archives": {
        "key": "signed_archives",
        "title": "Signed Archives (*.sig Verification)",
        "severity": "amber",
        "summary": "Unreal Engine `.pak` / `.utoc` archives are paired with `.sig` cryptographic signature files.",
        "why_it_blocks": "The game verifies `.sig` signatures on startup and ignores or rejects unsigned custom mod paks.",
        "what_you_can_do_instead": "Install a signature-check bypass (such as RE-UE4SS or a community sig-bypass loader) before dropping custom `~mods/*.pak` files.",
    },
    "aot_compiled_code": {
        "key": "aot_compiled_code",
        "title": "AOT-Compiled Native / IL2CPP Machine Code",
        "severity": "red",
        "summary": "Compiled C++ binaries or Unity IL2CPP (`GameAssembly.dll`) are Ahead-of-Time native machine code rather than editable C#/script source.",
        "why_it_blocks": "Original source code cannot be reconstructed and recompiled directly in-place like a source project.",
        "what_you_can_do_instead": "Use a runtime bridge (`bepinex-mcp` / Harmony hooks) or UE4SS Lua hooks to inspect objects and override fields/methods at runtime.",
    },
    "compiled_shaders": {
        "key": "compiled_shaders",
        "title": "Compiled GPU Shader Bytecode (DXBC / SPIR-V)",
        "severity": "red",
        "summary": "Shaders inside engine archives are pre-compiled GPU bytecode with no editable `.shader` or `.hlsl` source files.",
        "why_it_blocks": "Engine shader variants cannot be recompiled from disk without the original game project and shader compiler.",
        "what_you_can_do_instead": "Use ReShade shaders or addon hooks (e.g., RenoDX) for post-processing, tone-mapping, and visual tweaks.",
    },
    "packed_binary_containers": {
        "key": "packed_binary_containers",
        "title": "Packed Binary Engine Containers",
        "severity": "amber",
        "summary": "Textures, meshes, audio banks, and prefabs are bundled inside binary containers (`*.assets`, `*.bundle`, `*.pak`, `*.ba2`, `*.pck`, `*.bank`).",
        "why_it_blocks": "Files inside binary containers cannot be edited directly with a text editor or `filesystem-mcp`.",
        "what_you_can_do_instead": "Use offline extraction/repacking tools (such as AssetRipper, FModel, UnrealReZen, or BAE) or override values at runtime via an MCP bridge.",
    },
    "server_authoritative": {
        "key": "server_authoritative",
        "title": "Online / Server-Authoritative Game State",
        "severity": "red",
        "summary": "Multiplayer matchmaking, inventory, currency, and progression are validated on remote game servers.",
        "why_it_blocks": "Local file or memory edits only affect client visuals and are rejected or flagged by the server.",
        "what_you_can_do_instead": "Limit modding strictly to offline, local, single-player gameplay.",
    },
}


def _detect_normalized_engine(root_path: Path, fp_engine: str = "") -> str:
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


def _check_per_game_loaders(root_path: Path, detected_engine: str) -> dict[str, bool]:
    """Check whether engine-specific loaders are actually installed inside the game root."""
    if not root_path.is_dir():
        return {"unity_bepinex": False, "unity_mcp_plugin": False, "unreal_ue4ss": False}

    if detected_engine in ("unity_il2cpp", "unity_mono"):
        core_ok = (root_path / "BepInEx" / "core").is_dir()
        proxy_ok = (root_path / "winhttp.dll").is_file() or (root_path / "version.dll").is_file()
        plugin_ok = (
            (root_path / "BepInEx" / "plugins" / "BepInExMCP.IL2CPP.dll").is_file()
            or any((root_path / "BepInEx" / "plugins").glob("*.dll"))
            if (root_path / "BepInEx" / "plugins").is_dir()
            else False
        )
        return {
            "unity_bepinex": core_ok and proxy_ok,
            "unity_mcp_plugin": core_ok and proxy_ok and plugin_ok,
            "unreal_ue4ss": False,
        }

    if detected_engine == "unreal":
        shipping_dirs = {p.parent for p in root_path.rglob("*-Win64-Shipping.exe")}
        win64_dirs = shipping_dirs or {
            p for p in root_path.rglob("Win64") if p.is_dir() and p.parent.name.lower() == "binaries"
        }
        ue4ss_found = False
        for wdir in win64_dirs:
            has_proxy = any((wdir / px).is_file() for px in ("dwmapi.dll", "dxgi.dll"))
            if not has_proxy:
                has_proxy = any(
                    f.is_file() and f.name.lower() in ("dwmapi.dll", "dxgi.dll")
                    for f in wdir.iterdir()
                )
            has_ue4ss = any(
                c.is_file() and c.name.lower() == "ue4ss.dll"
                for c in (wdir / "ue4ss" / "UE4SS.dll", wdir / "UE4SS.dll")
            ) or any(f.is_file() and f.name.lower() == "ue4ss.dll" for f in wdir.rglob("*.dll"))
            if has_proxy and has_ue4ss:
                ue4ss_found = True
                break
        return {
            "unity_bepinex": False,
            "unity_mcp_plugin": False,
            "unreal_ue4ss": ue4ss_found,
        }

    return {"unity_bepinex": False, "unity_mcp_plugin": False, "unreal_ue4ss": False}


def _find_host_exe(folder_hint: str, exe_names: tuple[str, ...]) -> str:
    for ex in exe_names:
        w = shutil.which(ex)
        if w:
            return w
    if (TOOLS_ROOT / folder_hint).is_dir():
        for ex in exe_names:
            cand = next((TOOLS_ROOT / folder_hint).rglob(ex), None)
            if cand and cand.is_file():
                return str(cand)
    return ""


def _check_local_modding_tools(
    root_path: Path, detected_engine: str
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate only ENGINE_TOOL_MATRIX[detected_engine] tools, separating host vs per-game status."""
    matrix_entry = ENGINE_TOOL_MATRIX.get(detected_engine, ENGINE_TOOL_MATRIX["generic"])
    allowed_bridges = matrix_entry["allowed_mcp_bridges"]
    allowed_cli = matrix_entry["allowed_loaders_and_cli"]
    game_loaders = _check_per_game_loaders(root_path, detected_engine)

    bep_host_ok = Path(DEFAULT_MCP_APP).is_file() and (
        (WORKSPACE_DIR / "bepinex-mcp").is_dir() or (RESOURCE_ROOT / "bepinex-mcp").is_dir()
    )
    ue4ss_host_ok = (TOOLS_ROOT / "re_ue4ss").is_dir() or (TOOLS_ROOT / "UE4SS").is_dir()
    godot_host_ok = bool(shutil.which("godot"))

    cli_probes = {
        "Il2CppDumper": ("il2cppdumper", ("Il2CppDumper.exe",), f"Download Il2CppDumper into {TOOLS_ROOT / 'Il2CppDumper'}"),
        "AssetRipper": ("assetripper", ("AssetRipper.exe", "AssetRipper.GUI.Free.exe"), f"Download AssetRipper into {TOOLS_ROOT / 'AssetRipper'}"),
        "ILSpy": ("dnspyex", ("ILSpy.exe", "dnSpy.exe"), f"Download ILSpy / dnSpyEx into {TOOLS_ROOT / 'dnSpyEx'}"),
        "FModel": ("fmodel", ("FModel.exe",), f"Download FModel into {TOOLS_ROOT / 'FModel'}"),
        "UnrealPak": ("unrealpak", ("UnrealPak.exe",), f"Place UnrealPak.exe in PATH or {TOOLS_ROOT / 'unrealpak'}"),
        "GDRE Tools": ("gdretools", ("gdre_tools.exe", "GDRE_tools.exe"), f"Download GDRE Tools into {TOOLS_ROOT / 'GDRETools'}"),
        "xEdit": ("xedit", ("xEdit.exe", "SSEEdit.exe", "FO4Edit.exe"), f"Download xEdit into {TOOLS_ROOT / 'xEdit'}"),
        "BAE": ("bae", ("bae.exe", "BAE.exe"), f"Download Bethesda Archive Extractor into {TOOLS_ROOT / 'BAE'}"),
    }

    tools: dict[str, dict[str, Any]] = {}
    available_tools: list[dict[str, Any]] = []

    for name in allowed_bridges:
        if name == "filesystem-mcp":
            entry = {
                "name": name,
                "kind": "mcp_bridge",
                "engine_compatible": True,
                "host_available": True,
                "installed_in_game": True,
                "available": True,
                "install_hint": "Built-in local filesystem access",
                "detail": "Built-in local filesystem access",
            }
        elif name == "bepinex-mcp":
            in_game = game_loaders["unity_mcp_plugin"]
            entry = {
                "name": name,
                "kind": "mcp_bridge",
                "engine_compatible": True,
                "host_available": bep_host_ok,
                "installed_in_game": in_game,
                "available": bep_host_ok and in_game,
                "install_hint": f"Run Provision in Active Game to deploy BepInEx + bridge into {root_path}",
                "detail": (
                    "Ready in Game"
                    if (bep_host_ok and in_game)
                    else ("Host bridge ready — provision into game" if bep_host_ok else "bepinex-mcp bridge not found on host")
                ),
            }
        elif name == "ue4ss-mcp":
            in_game = game_loaders["unreal_ue4ss"]
            entry = {
                "name": name,
                "kind": "mcp_bridge",
                "engine_compatible": True,
                "host_available": ue4ss_host_ok,
                "installed_in_game": in_game,
                "available": in_game,
                "install_hint": f"Extract RE-UE4SS into {root_path}\\Binaries\\Win64\\",
                "detail": (
                    "UE4SS installed in Binaries/Win64"
                    if in_game
                    else "Install UE4SS into Binaries/Win64 to enable Lua/Blueprint runtime hooks"
                ),
            }
        elif name == "godot-mcp":
            entry = {
                "name": name,
                "kind": "mcp_bridge",
                "engine_compatible": True,
                "host_available": godot_host_ok,
                "installed_in_game": False,
                "available": godot_host_ok,
                "install_hint": "Install Godot CLI on PATH or GDRE Tools",
                "detail": "Godot CLI on PATH" if godot_host_ok else "Godot CLI not found on PATH",
            }
        else:  # creation-kit-mcp
            entry = {
                "name": name,
                "kind": "mcp_bridge",
                "engine_compatible": True,
                "host_available": False,
                "installed_in_game": False,
                "available": False,
                "install_hint": "Use Creation Kit / xEdit for .esm/.esp editing",
                "detail": "External Creation Kit / xEdit",
            }
        tools[name] = entry
        available_tools.append(entry)

    for name in allowed_cli:
        if name in ("BepInEx (IL2CPP)", "BepInEx (Mono)"):
            in_game = game_loaders["unity_bepinex"]
            host_ok = Path(DEFAULT_BEPINEX_ZIP).is_file() or (TOOLS_ROOT / "BepInEx5_Mono").is_dir()
            entry = {
                "name": name,
                "kind": "loader_cli",
                "engine_compatible": True,
                "host_available": host_ok,
                "installed_in_game": in_game,
                "available": in_game,
                "install_hint": f"Extract {name} into {root_path}",
                "detail": (
                    "Installed in Game Folder"
                    if in_game
                    else ("Host cache ready — not installed in game" if host_ok else f"Install {name} into game folder")
                ),
            }
        elif name == "UE4SS":
            in_game = game_loaders["unreal_ue4ss"]
            entry = {
                "name": name,
                "kind": "loader_cli",
                "engine_compatible": True,
                "host_available": ue4ss_host_ok,
                "installed_in_game": in_game,
                "available": in_game,
                "install_hint": f"Extract RE-UE4SS into {root_path}\\Binaries\\Win64\\",
                "detail": "Installed in Binaries/Win64" if in_game else f"Extract RE-UE4SS into {root_path}\\Binaries\\Win64\\",
            }
        else:
            folder_hint, exe_names, hint = cli_probes[name]
            found_path = _find_host_exe(folder_hint, exe_names)
            entry = {
                "name": name,
                "kind": "loader_cli",
                "engine_compatible": True,
                "host_available": bool(found_path),
                "installed_in_game": False,
                "available": bool(found_path),
                "install_hint": hint,
                "detail": found_path if found_path else f"{name} not found locally",
            }
        tools[name] = entry
        available_tools.append(entry)

    return tools, available_tools


def _check_pak_encrypted_hint(pak_path: Path) -> bool:
    """Fast non-destructive footer check on Unreal .pak (reads only last 256 bytes)."""
    try:
        size = pak_path.stat().st_size
        if size < 64:
            return False
        with open(pak_path, "rb") as f:
            f.seek(max(0, size - 256))
            tail = f.read(256)
        # Check for Unreal Pak magic 0x5A6F12E1; if index_encrypted byte near footer is 1, flag encrypted
        magic = b"\xe1\x12\x6f\x5a"
        idx = tail.rfind(magic)
        if idx >= 1:
            # In UE4/5 FPakInfo, bEncryptedIndex sits immediately before version/magic in older/newer formats
            # Conservative check: only flag if explicit bEncryptedIndex == 1 at magic - 17 or magic - 1
            if idx >= 17 and tail[idx - 17] == 1:
                return True
    except Exception:
        pass
    return False


class AnalyzeAssetsRequest(BaseModel):
    game_id: str = ""
    root_path: str = ""


@router.get("/api/game/moddability-guide")
async def get_moddability_guide() -> dict[str, Any]:
    return {
        "status": "ok",
        "guide": list(MODDABILITY_GUIDE.values()),
        "guide_by_key": MODDABILITY_GUIDE,
    }


@router.post("/api/game/analyze-assets")
async def analyze_game_assets(req: AnalyzeAssetsRequest) -> dict[str, Any]:
    game_id = req.game_id.strip()
    root_str = req.root_path.strip()
    game_title = ""

    if game_id and not root_str:
        store = load_managed_games()
        target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
        if target:
            root_str = str(target.get("game_dir") or "").strip()
            game_title = str(target.get("name") or game_id)

    if not root_str:
        raise HTTPException(status_code=400, detail="Provide 'root_path' or a valid 'game_id'.")

    root_path = Path(root_str)
    if not root_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Game directory not found on disk: {root_path}")

    if not game_id:
        game_id = f"path_{hashlib.sha256(str(root_path).lower().encode('utf-8')).hexdigest()[:10]}"
    if not game_title:
        game_title = root_path.name

    fp = fingerprint_library_entry(root_path)
    engine = fp["engine"]
    detected_engine = _detect_normalized_engine(root_path, engine)

    skip_dirs = {".git", "backups", "app_backups", "__pycache__", ".venv", "temp", "tmp", "crashdumps"}
    open_exts = {".json", ".xml", ".yaml", ".yml", ".lua", ".ini", ".cfg", ".csv", ".tsv", ".sqlite", ".db"}
    media_exts = {".png", ".jpg", ".jpeg", ".dds", ".tga", ".wav", ".ogg", ".mp3", ".wem", ".fbx", ".obj", ".gltf"}
    packed_exts = {".assets", ".bundle", ".pak", ".utoc", ".ucas", ".ba2", ".bsa", ".pck", ".bank", ".archive", ".vpk"}
    ac_tokens = ("easyanticheat", "beservice", "battleye", "vgk.sys", "vanguard", "gameguard.des", "nprotect", "denuvo")

    counts: dict[str, int] = {
        "anti_cheat": 0,
        "open_data": 0,
        "loose_media": 0,
        "runtime_code": 0,
        "packed_containers": 0,
        "signed_archives": 0,
        "encrypted_paks": 0,
    }
    samples: dict[str, list[str]] = {k: [] for k in counts}

    def _add_sample(cat: str, rel_p: str) -> None:
        counts[cat] += 1
        if len(samples[cat]) < 12:
            samples[cat].append(rel_p)

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]
        d_path = Path(dirpath)
        for fname in filenames:
            fpath = d_path / fname
            try:
                rel_p = str(fpath.relative_to(root_path)).replace("\\", "/")
            except Exception:
                rel_p = fname
            flow = fname.lower()
            ext = fpath.suffix.lower()

            if any(tok in flow for tok in ac_tokens):
                _add_sample("anti_cheat", rel_p)
            elif ext == ".sig":
                _add_sample("signed_archives", rel_p)
            elif ext in packed_exts or flow in ("resources.assets", "globalgamemanagers"):
                _add_sample("packed_containers", rel_p)
                if ext == ".pak" and counts["encrypted_paks"] < 3 and _check_pak_encrypted_hint(fpath):
                    _add_sample("encrypted_paks", rel_p)
            elif ext in open_exts or "streamingassets" in rel_p.lower() or flow == "catalog.json":
                _add_sample("open_data", rel_p)
            elif ext in media_exts:
                _add_sample("loose_media", rel_p)
            elif flow in ("gameassembly.dll", "global-metadata.dat", "assembly-csharp.dll") or ext in (".gd", ".gdc", ".esm", ".esp"):
                _add_sample("runtime_code", rel_p)
            elif ext in (".exe", ".dll") and len(samples["runtime_code"]) < 6 and "binaries/win64" in rel_p.lower():
                _add_sample("runtime_code", rel_p)

    tools, available_tools = _check_local_modding_tools(root_path, detected_engine)
    layers: list[dict[str, Any]] = []

    # 1. Anti-Cheat Gate (RED if present)
    if counts["anti_cheat"] > 0 or fp["anti_cheat_status"] == "Restricted":
        layers.append({
            "status": "red",
            "layer_name": "Anti-Cheat & Tamper Protection",
            "detected_items": samples["anti_cheat"] or ["Anti-Cheat Marker"],
            "file_count": counts["anti_cheat"],
            "required_tools": ["None (Blocked)"],
            "tool_status": "Blocked by kernel/user-mode anti-cheat",
            "what_can_change": "Nothing — DLL injection, runtime hooks, and archive edits are blocked.",
            "blocker_key": "anti_cheat",
        })

    # 2. Open Data, Scripts & Manifests (GREEN)
    if counts["open_data"] > 0:
        layers.append({
            "status": "green",
            "layer_name": "Open Data, Configs & Scripts",
            "detected_items": samples["open_data"],
            "file_count": counts["open_data"],
            "required_tools": ["filesystem-mcp"],
            "tool_status": "Available (filesystem-mcp)",
            "what_can_change": "Plaintext .ini/.json/.xml/.lua configs, balance tables, and Addressables catalog.json.",
            "blocker_key": "",
        })

    # 3. Loose Media Assets (GREEN)
    if counts["loose_media"] > 0:
        layers.append({
            "status": "green",
            "layer_name": "Loose Media Assets (Textures / Audio / Models)",
            "detected_items": samples["loose_media"],
            "file_count": counts["loose_media"],
            "required_tools": ["filesystem-mcp"],
            "tool_status": "Available (filesystem-mcp)",
            "what_can_change": "Direct loose image, audio, and mesh replacements.",
            "blocker_key": "",
        })

    # 4. Runtime Code & Scripting Surfaces (strictly engine-filtered via ENGINE_TOOL_MATRIX)
    if counts["runtime_code"] > 0 or detected_engine in ("unity_il2cpp", "unity_mono", "unreal", "creation_engine", "godot"):
        if detected_engine in ("unity_il2cpp", "unity_mono"):
            bep_ok = tools.get("bepinex-mcp", {}).get("installed_in_game", False)
            layers.append({
                "status": "green" if bep_ok and counts["anti_cheat"] == 0 else "amber",
                "layer_name": f"Runtime Hooks & Object Inspection ({engine})",
                "detected_items": samples["runtime_code"] or [engine],
                "file_count": counts["runtime_code"],
                "required_tools": ["bepinex-mcp"],
                "tool_status": tools["bepinex-mcp"]["detail"],
                "what_can_change": "Live object properties, component values, and runtime method hooks.",
                "blocker_key": "",
            })
        elif detected_engine == "unreal":
            ue_ok = tools.get("ue4ss-mcp", {}).get("installed_in_game", False)
            layers.append({
                "status": "green" if ue_ok and counts["anti_cheat"] == 0 else "amber",
                "layer_name": "Unreal Blueprint & UObject Runtime Hooks (UE4SS)",
                "detected_items": samples["runtime_code"] or ["Unreal Engine Binaries"],
                "file_count": counts["runtime_code"],
                "required_tools": ["ue4ss-mcp", "UE4SS"],
                "tool_status": (
                    "UE4SS installed in Binaries/Win64"
                    if ue_ok
                    else "Install UE4SS into Binaries/Win64 to enable Lua/Blueprint runtime hooks"
                ),
                "what_can_change": "Blueprint hooks, Lua mods, and live UObject property overrides.",
                "blocker_key": "" if ue_ok else "packed_binary_containers",
            })
        elif detected_engine == "creation_engine":
            layers.append({
                "status": "green",
                "layer_name": "Creation Engine Plugins (.esm / .esp)",
                "detected_items": samples["runtime_code"],
                "file_count": counts["runtime_code"],
                "required_tools": ["creation-kit-mcp", "filesystem-mcp"],
                "tool_status": "Plugin load order & record overrides supported",
                "what_can_change": "Game records, quests, NPCs, items, and worldspaces via .esp/.esm plugins.",
                "blocker_key": "",
            })
        elif detected_engine == "godot":
            layers.append({
                "status": "amber",
                "layer_name": "Godot Scripts & Packed Scenes",
                "detected_items": samples["runtime_code"] or ["Godot Project"],
                "file_count": counts["runtime_code"],
                "required_tools": ["godot-mcp", "GDRE Tools"],
                "tool_status": tools.get("GDRE Tools", {}).get("detail", "GDRE Tools"),
                "what_can_change": "GDScript logic and packed .pck scenes via GDRE Tools.",
                "blocker_key": "",
            })

        if detected_engine == "unity_il2cpp":
            layers.append({
                "status": "red",
                "layer_name": "AOT-Compiled IL2CPP Native Code",
                "detected_items": samples["runtime_code"][:4] or ["GameAssembly.dll"],
                "file_count": counts["runtime_code"],
                "required_tools": ["Il2CppDumper"],
                "tool_status": tools.get("Il2CppDumper", {}).get("detail", "Il2CppDumper"),
                "what_can_change": "Cannot edit/recompile original C# source code directly (use runtime hooks instead).",
                "blocker_key": "aot_compiled_code",
            })
        elif detected_engine == "unreal":
            layers.append({
                "status": "red",
                "layer_name": "AOT-Compiled Native C++ Shipping Binary",
                "detected_items": samples["runtime_code"][:4] or ["*-Win64-Shipping.exe"],
                "file_count": counts["runtime_code"],
                "required_tools": ["UE4SS"],
                "tool_status": tools.get("UE4SS", {}).get("detail", "UE4SS"),
                "what_can_change": "Native C++ shipping code cannot be recompiled; use UE4SS Lua/Blueprint hooks instead.",
                "blocker_key": "aot_compiled_code",
            })

    # 5. Packed Containers & Signed / Encrypted Archives (AMBER / RED)
    if counts["packed_containers"] > 0:
        if detected_engine == "unreal":
            helper_list = ["FModel", "UnrealPak"]
        elif detected_engine == "godot":
            helper_list = ["GDRE Tools"]
        elif detected_engine == "creation_engine":
            helper_list = ["BAE"]
        elif detected_engine in ("unity_il2cpp", "unity_mono"):
            helper_list = ["AssetRipper"]
        else:
            helper_list = []
        helper_status = (
            ", ".join(f"{h}: {'Found' if tools.get(h, {}).get('host_available') else 'Missing'}" for h in helper_list)
            if helper_list
            else "No engine-specific archive unpacker configured"
        )
        sig_note = " (check *.sig bypass before loading custom ~mods/*.pak)" if detected_engine == "unreal" else ""
        layers.append({
            "status": "amber",
            "layer_name": "Packed Binary Engine Containers",
            "detected_items": samples["packed_containers"],
            "file_count": counts["packed_containers"],
            "required_tools": helper_list or ["Manual Inspection"],
            "tool_status": helper_status,
            "what_can_change": f"Requires offline unpacking/repacking tools before replacing bundled textures, meshes, or audio{sig_note}.",
            "blocker_key": "packed_binary_containers",
        })
        layers.append({
            "status": "red",
            "layer_name": "Compiled GPU Shader Bytecode",
            "detected_items": samples["packed_containers"][:3],
            "file_count": counts["packed_containers"],
            "required_tools": ["ReShade / Addons (visual post-processing)"],
            "tool_status": "Shader source not shipped",
            "what_can_change": "Embedded DXBC/SPIR-V shaders cannot be recompiled; use ReShade for post-processing.",
            "blocker_key": "compiled_shaders",
        })

    if counts["signed_archives"] > 0:
        layers.append({
            "status": "amber",
            "layer_name": "Signed Unreal Archives (*.sig Verification)",
            "detected_items": samples["signed_archives"],
            "file_count": counts["signed_archives"],
            "required_tools": ["UE4SS"],
            "tool_status": tools.get("UE4SS", {}).get("detail", "Extract RE-UE4SS into Binaries/Win64"),
            "what_can_change": "Custom ~mods/*.pak files require a signature-check bypass to load.",
            "blocker_key": "signed_archives",
        })

    if counts["encrypted_paks"] > 0:
        layers.append({
            "status": "red",
            "layer_name": "Encrypted Game Archives",
            "detected_items": samples["encrypted_paks"],
            "file_count": counts["encrypted_paks"],
            "required_tools": ["Decryption Key (Unavailable)"],
            "tool_status": "AES index encryption detected",
            "what_can_change": "Archive contents cannot be unpacked or repacked without the game's AES key.",
            "blocker_key": "encrypted_archives",
        })

    summary_counts = {
        "green": sum(1 for l in layers if l["status"] == "green"),
        "amber": sum(1 for l in layers if l["status"] == "amber"),
        "red": sum(1 for l in layers if l["status"] == "red"),
    }
    triggered_blockers = sorted({l["blocker_key"] for l in layers if l.get("blocker_key")})

    report = {
        "status": "ok",
        "game_id": game_id,
        "title": game_title,
        "root_path": str(root_path),
        "engine": engine,
        "detected_engine": detected_engine,
        "anti_cheat_status": fp["anti_cheat_status"],
        "recommended_adapter": fp["recommended_adapter"],
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary_counts,
        "counts": counts,
        "tools": tools,
        "available_tools": available_tools,
        "layers": layers,
        "triggered_blockers": triggered_blockers,
        "guide": MODDABILITY_GUIDE,
    }

    audit_file = ASSET_AUDITS_DIR / f"{Path(game_id).name}.json"
    audit_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

    append_audit_log(
        action="game.analyze_assets",
        status="ok",
        summary=f"Analyzed assets for '{game_title}' ({engine}): {summary_counts['green']} Green, {summary_counts['amber']} Amber, {summary_counts['red']} Red",
        details={"game_id": game_id, "engine": engine, "summary": summary_counts, "triggered_blockers": triggered_blockers},
    )
    return report


"""Unity (IL2CPP and Mono) engine adapter, metadata inspection, and loader readiness checks."""

from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path
from typing import Any, Callable

from app.core.config import manifest_path_for_game, sha256_file
from app.core.constants import ANTI_CHEAT_BINARIES
from app.engines.base import EngineAdapter


def _unity_data_dirs(game_dir: Path) -> list[Path]:
    if not game_dir.is_dir():
        return []
    return [p for p in game_dir.glob("*_Data") if p.is_dir()]


def check_unity_loaders(root_path: Path) -> dict[str, bool]:
    """Return whether Doorstop/BepInEx and the live MCP bridge plugin are installed in *root_path*."""
    if not root_path.is_dir():
        return {"unity_bepinex": False, "unity_mcp_plugin": False}

    core_ok = (root_path / "BepInEx" / "core").is_dir()
    proxy_ok = (root_path / "winhttp.dll").is_file() or (root_path / "version.dll").is_file()
    plugins_dir = root_path / "BepInEx" / "plugins"
    plugin_ok = (
        (plugins_dir / "BepInExMCP.IL2CPP.dll").is_file()
        or (plugins_dir / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.dll").is_file()
        or (plugins_dir / "BepInExMCP.dll").is_file()
        or any(plugins_dir.glob("*.dll"))
        if plugins_dir.is_dir()
        else False
    )
    return {
        "unity_bepinex": core_ok and proxy_ok,
        "unity_mcp_plugin": core_ok and proxy_ok and plugin_ok,
    }


def fingerprint_unity_directory(game_dir: Path) -> dict[str, Any]:
    """Compute Unity IL2CPP runtime/anti-cheat/interop fingerprint for `/api/fingerprint`."""
    game_assembly = game_dir / "GameAssembly.dll"
    has_game_assembly = game_assembly.is_file()

    metadata_files = list(game_dir.glob("*_Data/il2cpp_data/Metadata/global-metadata.dat"))
    if not metadata_files:
        metadata_files = list(game_dir.rglob("global-metadata.dat"))
    has_global_metadata = len(metadata_files) > 0
    metadata_path = str(metadata_files[0]) if has_global_metadata else None

    interop_dir = game_dir / "BepInEx" / "interop"
    has_interop = interop_dir.is_dir() and any(interop_dir.iterdir())
    assembly_hash_exists = (interop_dir / "assembly-hash.txt").is_file()

    detected_anticheat: list[str] = []
    for root, _dirs, files in os.walk(game_dir):
        for fname in files:
            if fname.lower() in ANTI_CHEAT_BINARIES:
                rel = os.path.relpath(os.path.join(root, fname), game_dir)
                detected_anticheat.append(rel)

    is_unity_il2cpp = has_game_assembly and has_global_metadata
    blocked = len(detected_anticheat) > 0
    runtime_kind = "Unity_IL2CPP" if is_unity_il2cpp else "Unknown"
    ga_sha256 = sha256_file(game_assembly) if has_game_assembly else None

    return {
        "game_dir": str(game_dir),
        "runtime": runtime_kind,
        "is_unity_il2cpp": is_unity_il2cpp,
        "has_game_assembly": has_game_assembly,
        "game_assembly_sha256": ga_sha256,
        "has_global_metadata": has_global_metadata,
        "global_metadata_path": metadata_path,
        "bepinex_interop_exists": has_interop,
        "bepinex_interop_assembly_hash": assembly_hash_exists,
        "anti_cheat_detected": detected_anticheat,
        "blocked": blocked,
        "status": "blocked" if blocked else ("ready" if is_unity_il2cpp else "invalid"),
    }


def inspect_unity_metadata(
    game_dir: Path,
    info: dict[str, Any],
    *,
    data_dirs: list[Path] | None = None,
) -> tuple[bool, bool]:
    """Populate Unity *_Data, Doorstop, BepInEx, interop, and Roslyn bridge fields on *info*."""
    resolved_data_dirs = data_dirs if data_dirs is not None else _unity_data_dirs(game_dir)

    if resolved_data_dirs:
        ddir = resolved_data_dirs[0]
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

    has_ga = (game_dir / "GameAssembly.dll").is_file()
    has_mono_managed = any((d / "Managed" / "Assembly-CSharp.dll").is_file() for d in resolved_data_dirs)
    return has_ga, has_mono_managed


def build_unity_crucial_steps(
    info: dict[str, Any],
    *,
    has_ga: bool,
    has_mono_managed: bool,
    ac_clear: bool,
    binaries_ok: bool,
    interop_ok: bool,
    bridge_ok: bool,
) -> list[dict[str, str]]:
    """Build the 6 crucial Unity modding checklist items for the Game Info panel."""
    mod_files_ok = bool(info.get("doorstop_installed") and info.get("bepinex_installed"))
    return [
        {
            "step": "1. Anti-Cheat & Safety Check",
            "state": "ok" if ac_clear else "blocked",
            "label": "Clear (Safe to Mod)" if ac_clear else "Blocked (Anti-Cheat / Live-Service)",
            "detail": "Zero anti-cheat drivers detected" if ac_clear else str(info.get("discovery_mcp_tools", "")),
        },
        {
            "step": "2. Engine & Metadata Files",
            "state": "ok" if binaries_ok else "missing",
            "label": "Present" if binaries_ok else "Missing",
            "detail": (
                f"GameAssembly.dll + global-metadata.dat (v{info.get('metadata_version')})"
                if has_ga
                else "Managed/Assembly-CSharp.dll"
            ),
        },
        {
            "step": "3. Mod Loader Files (Doorstop & BepInEx)",
            "state": "ok" if mod_files_ok else "missing",
            "label": (
                f"Present (BepInEx {info.get('bepinex_version') or '6.x'} + Doorstop {info.get('doorstop_version') or 'OK'})"
                if mod_files_ok
                else "Not Present — Run Provision & Backup"
            ),
            "detail": "winhttp.dll + doorstop_config.ini + BepInEx/core",
        },
        {
            "step": "4. First-Run Bootstrap & Interop DLLs",
            "state": "ok" if interop_ok else "pending",
            "label": (
                f"Generated ({info.get('interop_assemblies_count', 0)} Interop DLLs)"
                if (has_ga and interop_ok)
                else ("Ready (Mono Runtime)" if (has_mono_managed and interop_ok) else "Pending — Launch Game Once After Provisioning")
            ),
            "detail": f"BepInEx/interop/assembly-hash.txt ({(info.get('assembly_hash') or 'not generated')[:12]})",
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
            "state": "ok" if info.get("manifest_tracked") else "pending",
            "label": "Tracked (Zero-Residual Restore Ready)" if info.get("manifest_tracked") else "No Active Provision Manifest",
            "detail": "Tracks added & backed-up colliding files in %LOCALAPPDATA%\\TheModdingTool\\manifests",
        },
    ]


def scan_unity_mods(
    game_dir: Path,
    add_mod_fn: Callable[..., None],
    folders_scanned: list[str],
    rel_fn: Callable[[Path], str],
) -> None:
    """Scan Unity `BepInEx/plugins` and `MelonLoader/Mods` folders for installed plugins."""
    bep_plugins = game_dir / "BepInEx" / "plugins"
    if bep_plugins.is_dir():
        folders_scanned.append(rel_fn(bep_plugins))
        for dll in sorted(bep_plugins.rglob("*.dll")):
            if dll.is_file():
                add_mod_fn(
                    name=dll.stem,
                    file_name=dll.name,
                    category="BepInEx Plugin",
                    path_obj=dll,
                    enabled=True,
                )

    melon_mods = game_dir / "MelonLoader" / "Mods"
    if melon_mods.is_dir():
        folders_scanned.append(rel_fn(melon_mods))
        for dll in sorted(melon_mods.glob("*.dll")):
            if dll.is_file():
                add_mod_fn(
                    name=dll.stem,
                    file_name=dll.name,
                    category="MelonLoader Mod",
                    path_obj=dll,
                    enabled=True,
                )


class UnityEngineAdapter(EngineAdapter):
    """Engine adapter for Unity IL2CPP and Mono titles."""

    engine_id = "unity"
    display_name = "Unity (IL2CPP / Mono)"

    def detect(self, game_dir: Path) -> bool:
        if not game_dir.is_dir():
            return False
        if (game_dir / "GameAssembly.dll").is_file():
            return True
        if any(game_dir.rglob("global-metadata.dat")):
            return True
        data_dirs = _unity_data_dirs(game_dir)
        return any(
            (d / "Managed" / "Assembly-CSharp.dll").is_file()
            or (d / "data.unity3d").is_file()
            or (d / "globalgamemanagers").is_file()
            for d in data_dirs
        )

    def fingerprint(self, game_dir: Path) -> dict[str, Any]:
        return fingerprint_unity_directory(game_dir)

    def discover_configs(self, game_dir: Path, **kwargs: Any) -> dict[str, Any]:
        configs: list[str] = []
        for pattern in ("doorstop_config.ini", "BepInEx/config/*.cfg", "*_Data/boot.config", "*_Data/ScriptingAssemblies.json"):
            configs.extend(str(p) for p in sorted(game_dir.glob(pattern)) if p.is_file())
        return {
            "engine": "unity",
            "config_files": configs,
        }

    def check_readiness(self, game_dir: Path, info: dict[str, Any] | None = None) -> dict[str, Any]:
        loaders = check_unity_loaders(game_dir)
        has_ga = (game_dir / "GameAssembly.dll").is_file() if game_dir.is_dir() else False
        interop_ok = ((game_dir / "BepInEx" / "interop" / "assembly-hash.txt").is_file() if has_ga else loaders["unity_bepinex"])
        if loaders["unity_mcp_plugin"] and interop_ok:
            readiness = "Ready"
        elif loaders["unity_bepinex"]:
            readiness = "Partially Provisioned"
        elif self.detect(game_dir):
            readiness = "Unprovisioned"
        else:
            readiness = "External Tooling"
        return {
            "ready": readiness == "Ready",
            "modding_readiness": readiness,
            **loaders,
        }

    def get_log_paths(self, game_dir: Path) -> list[Path]:
        if not game_dir.is_dir():
            return []
        candidates = [
            game_dir / "BepInEx" / "LogOutput.log",
            game_dir / "MelonLoader" / "Latest.log",
        ]
        return [p for p in candidates if p.is_file()]

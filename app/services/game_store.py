"""Managed games store enrichment, inspection, icon extraction, conflict check, and pruning."""

from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from app.core.audit import append_audit_log
from app.core.config import (
    GAME_ICONS_DIR,
    ICON_EXTRACT_SEMAPHORE,
    TOOLS_ROOT,
    load_managed_games,
    manifest_path_for_game,
    save_managed_games,
    sha256_text,
)
from app.engines.generic import (
    build_generic_crucial_steps,
    check_anticheat_binaries,
    detect_reshade_info,
    scan_installed_mods,
)
from app.engines.unity import build_unity_crucial_steps, inspect_unity_metadata
from app.engines.unreal import build_unreal_crucial_steps, inspect_unreal_game_settings
from app.services.discovery import (
    _find_primary_game_exe,
    is_game_candidate,
    lookup_sheet_reference,
    make_slug,
)


def evaluate_modding_apps_for_game(game_dir: Path, info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return modding apps/tools with live installation status and game compatibility flags."""
    dir_exists = game_dir.is_dir()
    has_ga = (game_dir / "GameAssembly.dll").is_file() if dir_exists else ("IL2CPP" in str(info.get("scripting_backend", "")))
    backend = str(info.get("scripting_backend", ""))
    cat = str(info.get("mcp_category", ""))
    tech = str(info.get("sheet_engine_tech", ""))
    is_mono = ("Mono" in backend) and not has_ga
    is_unity = (cat == "Unity") or has_ga or is_mono
    is_ue4_5 = bool(info.get("is_unreal_engine")) or (cat == "Unreal Engine 4/5") or ("Unreal Engine 4/5" in tech)
    is_ue3 = ("Unreal Engine 3" in tech) or ("Unreal Engine 3" in backend)
    is_creation = cat == "Creation Engine"
    is_godot = cat == "Godot"
    is_re_engine = ("RE Engine" in tech) or ("RE Engine" in backend)
    is_fromsoft = ("FromSoftware" in tech) or ("FromSoftware" in backend)
    is_custom_cpp = (cat == "Custom / Legacy C++") or is_ue3 or is_fromsoft or is_re_engine
    if cat == "Online / Anti-Cheat":
        is_unity = False
        is_ue4_5 = False
        is_ue3 = False
        is_creation = False
        is_godot = False
        is_re_engine = False
        is_fromsoft = False
        is_custom_cpp = False

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


def inspect_game_modding_info(
    game_dir: Path,
    display_name: str = "",
    *,
    pc_specs: dict[str, Any] | None = None,
    pc_specs_fn: Callable[[bool], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Determine platform, engine, backend, metadata version, and modding requirements from disk."""
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

    detected_anticheat = check_anticheat_binaries(game_dir)

    primary_exe = _find_primary_game_exe(game_dir, display_name or raw_folder_name)
    exes = [primary_exe] if primary_exe else [
        p for p in game_dir.glob("*.exe") if "unitycrashhandler" not in p.name.lower()
    ]
    target_pe = exes[0] if exes else (game_dir / "GameAssembly.dll")
    if exes and exes[0]:
        info["executable"] = exes[0].name
        info["executable_rel"] = exes[0].relative_to(game_dir).as_posix() if exes[0].is_relative_to(game_dir) else exes[0].name
        gstr = str(game_dir).lower()
        if "steam" in gstr:
            info["platform_store"] = "Steam"
        elif "gog" in gstr:
            info["platform_store"] = "GOG Galaxy"
        elif "xboxgames" in gstr:
            info["platform_store"] = "Xbox / PC Game Pass"
        elif "epic" in gstr:
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

    info["reshade"] = detect_reshade_info(game_dir, primary_exe=primary_exe)
    installed_mods, installed_mods_summary = scan_installed_mods(game_dir, primary_exe=primary_exe)
    info["installed_mods"] = installed_mods
    info["installed_mods_summary"] = installed_mods_summary

    pak_files = list(game_dir.glob("*/Content/Paks/*.pak")) + list(game_dir.glob("*/Content/Paks/*.utoc"))
    info["pak_count"] = len(pak_files)
    ue4ss_dll = next(game_dir.glob("*/Binaries/Win64/UE4SS.dll"), None) or next(game_dir.glob("*/Binaries/Win64/dwmapi.dll"), None)
    info["ue4ss_installed"] = bool(ue4ss_dll and ue4ss_dll.is_file())
    ue_mods = [m for m in game_dir.glob("*/Binaries/Win64/Mods/*") if m.is_dir() and m.name.lower() != "shared"]
    info["ue_mods_count"] = len(ue_mods)

    if (game_dir / "D3D12").is_dir():
        info["graphics_api"] = "Direct3D 12 (D3D12)"

    data_dirs = [p for p in game_dir.glob("*_Data") if p.is_dir()]
    has_ga, has_mono_managed = inspect_unity_metadata(game_dir, info, data_dirs=data_dirs)

    has_ue4_pak = any(game_dir.rglob("*.pak")) and any(
        (game_dir / "Engine").is_dir() or "shipping.exe" in p.name.lower() for p in game_dir.rglob("*.exe")
    )
    has_godot_pck = any(game_dir.glob("*.pck"))
    has_creation_esm = any(game_dir.rglob("*.esm"))

    sheet_row = lookup_sheet_reference(info["product_name"], display_name or game_dir.name)
    if sheet_row:
        info["sheet_reference_match"] = f"Matched Sheet: '{sheet_row['Game Title']}' ({sheet_row['MCP Category']})"
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

    ac_clear = (len(detected_anticheat) == 0) and (info["mcp_category"] != "Online / Anti-Cheat")
    binaries_ok = has_ga or has_mono_managed or (info["executable"] != "Not found")
    interop_ok = (info["interop_assemblies_count"] > 0 and bool(info["assembly_hash"])) if has_ga else info["bepinex_installed"]
    roslyn_ok = bool(info.get("roslyn_Compiler_installed"))
    bridge_ok = (bool(info.get("mcp_bridge_installed")) and roslyn_ok) if has_ga else bool(info.get("mcp_bridge_installed"))

    is_unreal = bool(
        has_ue4_pak
        or info.get("mcp_category") == "Unreal Engine 4/5"
        or "Unreal" in str(info.get("sheet_engine_tech", ""))
        or "Unreal" in str(info.get("scripting_backend", ""))
        or ((game_dir / "Engine" / "Config").is_dir() and not has_ga and not has_mono_managed)
    )
    info["is_unreal_engine"] = is_unreal

    if has_ga or has_mono_managed:
        info["crucial_steps"] = build_unity_crucial_steps(
            info,
            has_ga=has_ga,
            has_mono_managed=has_mono_managed,
            ac_clear=ac_clear,
            binaries_ok=binaries_ok,
            interop_ok=interop_ok,
            bridge_ok=bridge_ok,
        )
    elif is_unreal:
        info["crucial_steps"] = build_unreal_crucial_steps(info, ac_clear=ac_clear, binaries_ok=binaries_ok)
    else:
        info["crucial_steps"] = build_generic_crucial_steps(info, ac_clear=ac_clear, binaries_ok=binaries_ok)

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
        inspect_unreal_game_settings(game_dir, info, pc_specs=pc_specs, pc_specs_fn=pc_specs_fn)
        if is_unreal
        else None
    )
    return info


def enrich_game_status(
    game: dict[str, Any],
    refresh_live: bool = False,
    *,
    pc_specs: dict[str, Any] | None = None,
    pc_specs_fn: Callable[[bool], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attach computed modding_info, detected_engine, and disk flags to a managed game record."""
    enriched = dict(game)
    enriched["name"] = str(enriched.get("name") or enriched.get("title") or enriched.get("id") or "Unknown")
    gdir = Path(str(game.get("game_dir", "")).strip())
    dir_exists = gdir.is_dir()
    missing_on_disk = bool(game.get("missing_on_disk", False)) or (not dir_exists)
    enriched["dir_exists"] = dir_exists
    enriched["missing_on_disk"] = missing_on_disk
    enriched["metadata_scraped"] = bool(game.get("metadata_scraped", False))
    enriched["last_scraped_at"] = game.get("last_scraped_at")
    enriched["has_game_assembly"] = (gdir / "GameAssembly.dll").is_file() if dir_exists else False
    enriched["interop_ready"] = (gdir / "BepInEx" / "interop" / "assembly-hash.txt").is_file() if dir_exists else False
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
            live_info = inspect_game_modding_info(
                gdir, str(game.get("name", "")), pc_specs=pc_specs, pc_specs_fn=pc_specs_fn
            )
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
                    pc_specs=pc_specs,
                    pc_specs_fn=pc_specs_fn,
                )
        elif missing_on_disk:
            mod_info["modding_readiness"] = "Cannot Be Found on Disk (Showing Previously Downloaded Metadata)"
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


async def select_and_enrich_game(
    game_id: str,
    *,
    pc_specs_fn: Callable[[bool], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    store = load_managed_games()
    target = next((g for g in store["games"] if g["id"] == game_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Managed game '{game_id}' not found.")

    display_name = str(target.get("name") or target.get("title") or game_id)
    target["name"] = display_name
    gdir = Path(str(target.get("game_dir", "")).strip())
    dir_exists = gdir.is_dir()
    target["missing_on_disk"] = not dir_exists
    scraped_now = False

    has_cached = isinstance(target.get("cached_modding_info"), dict) and bool(target.get("cached_modding_info"))
    if (not target.get("metadata_scraped") or not has_cached) and dir_exists:
        scraped_info = await asyncio.to_thread(
            inspect_game_modding_info, gdir, display_name, pc_specs_fn=pc_specs_fn
        )
        scraped_info["metadata_scraped"] = True
        target["cached_modding_info"] = scraped_info
        target["metadata_scraped"] = True
        target["last_scraped_at"] = datetime.now(timezone.utc).isoformat()
        scraped_now = True

    store["active_game_id"] = game_id
    store["selected_id"] = game_id
    save_managed_games(store)

    status_note = (
        "scraped metadata on first load"
        if scraped_now
        else ("game folder missing on disk — loaded cached metadata" if not dir_exists else "loaded cached metadata")
    )
    info_hash = sha256_text(
        json.dumps(target.get("cached_modding_info") or {"game_dir": str(gdir)}, sort_keys=True, default=str)
    )
    append_audit_log(
        action="game_manager.load_and_scan" if scraped_now else "game_manager.load",
        status="warning" if not dir_exists else "ok",
        summary=f"Loaded game '{display_name}' ({game_id}) [{status_note}]",
        sha256=info_hash,
        details={
            "game_id": game_id,
            "game_dir": target.get("game_dir"),
            "scraped_now": scraped_now,
            "missing_on_disk": not dir_exists,
        },
    )
    return {
        "status": "ok",
        "active_game_id": game_id,
        "scraped_now": scraped_now,
        "missing_on_disk": not dir_exists,
        "game": enrich_game_status(target, pc_specs_fn=pc_specs_fn),
    }


def rescan_game_sync(
    game_id: str,
    *,
    pc_specs_fn: Callable[[bool], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    store = load_managed_games()
    target = next((g for g in store["games"] if g["id"] == game_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Managed game '{game_id}' not found.")

    gdir = Path(str(target.get("game_dir", "")).strip())
    if not gdir.is_dir():
        target["missing_on_disk"] = True
        save_managed_games(store)
        append_audit_log(
            action="game_manager.rescan",
            status="warning",
            summary=f"Rescan failed for '{target['name']}': folder cannot be found on disk ({gdir}); retaining cached info",
            sha256=sha256_text(str(gdir).lower()),
            details={"game_id": game_id, "game_dir": str(gdir)},
        )
        return {
            "status": "missing_on_disk",
            "message": f"Game folder cannot be found on disk: {gdir}. Showing previously downloaded information.",
            "active_game_id": game_id,
            "game": enrich_game_status(target, pc_specs_fn=pc_specs_fn),
        }

    scraped_info = inspect_game_modding_info(gdir, str(target.get("name", "")), pc_specs_fn=pc_specs_fn)
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
            f"Rescanned game folder metadata for '{target['name']}' ({game_id}) "
            f"[{scraped_info.get('mcp_category')} | {scraped_info.get('modding_readiness')}]"
        ),
        sha256=rescan_hash,
        details={"game_id": game_id, "game_dir": str(gdir)},
    )
    return {
        "status": "ok",
        "message": f"Successfully rescanned metadata from {gdir}",
        "active_game_id": game_id,
        "game": enrich_game_status(target, pc_specs_fn=pc_specs_fn),
    }


def prune_non_games_from_store() -> dict[str, Any]:
    """Remove duplicate Xbox Content/root entries and non-game entries from managed_games.json."""
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


def scan_game_conflicts(game_id: str) -> dict[str, Any]:
    """Scan BepInEx/plugins and Mods/ for duplicate DLLs/mods and stale interop hashes."""
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

    interop_hash_file = gdir / "BepInEx" / "interop" / "assembly-hash.txt"
    ga_file = gdir / "GameAssembly.dll"
    if ga_file.is_file() and (gdir / "BepInEx" / "core").is_dir() and not interop_hash_file.is_file():
        issues.append("BepInEx\\interop\\assembly-hash.txt is missing — launch the game once to generate interop assemblies.")

    for mods_dir in gdir.glob("*/Binaries/Win64/Mods"):
        if mods_dir.is_dir():
            for mod_sub in mods_dir.iterdir():
                if mod_sub.is_dir() and mod_sub.name.lower() != "shared":
                    enabled = (mod_sub / "enabled.txt").is_file()
                    installed_plugins.append({"name": mod_sub.name, "path": f"UE4SS/Mods/{mod_sub.name}", "enabled": enabled})

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


async def extract_game_icon_png(game_id: str) -> Path | None:
    """Extract and cache a 32x32 PNG icon from the game's primary executable (or .ico)."""
    safe_id = make_slug(game_id)
    cached_png = GAME_ICONS_DIR / f"{safe_id}.png"
    if cached_png.is_file() and cached_png.stat().st_size > 0:
        return cached_png

    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if not target:
        return None

    game_dir = Path(target.get("game_dir", ""))
    if not game_dir.is_dir():
        return None

    exe_path = _find_primary_game_exe(game_dir, target.get("name", ""))
    if exe_path is None:
        icos = list(game_dir.glob("*.ico")) + list(game_dir.glob("*/*.ico"))
        exe_path = icos[0] if icos else None
    if exe_path is None:
        return None

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

    return cached_png if (cached_png.is_file() and cached_png.stat().st_size > 0) else None


async def prewarm_game_icons_batch() -> dict[str, Any]:
    """Extract and cache all missing game .exe icons in a single batched PowerShell call."""
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

    GAME_ICONS_DIR.mkdir(parents=True, exist_ok=True)
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

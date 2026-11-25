"""Coexistence and conflict guard for proxy DLLs, Vortex/MO2 markers, and Unreal .pak overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

PROXY_DLL_NAMES: tuple[str, ...] = (
    "winhttp.dll",
    "dwmapi.dll",
    "dxgi.dll",
    "d3d11.dll",
    "dinput8.dll",
    "version.dll",
    "winmm.dll",
    "xinput1_3.dll",
    "bink2w64.dll",
)

MOD_MANAGER_MARKERS: dict[str, str] = {
    "vortex.deployment.json": "Vortex",
    "vortex.deployment.dinput.json": "Vortex",
    "__folder_managed_by_vortex": "Vortex",
    "modlist.txt": "MO2",
    "modorganizer.ini": "MO2",
}


def _candidate_search_dirs(game_dir: Path) -> list[Path]:
    dirs: list[Path] = [game_dir]
    patterns = (
        "Binaries/Win64",
        "*/Binaries/Win64",
        "Content/Paks",
        "*/Content/Paks",
        "Content/Paks/~mods",
        "*/Content/Paks/~mods",
        "BepInEx",
        "BepInEx/plugins",
        "Mods",
        "profiles",
        "profiles/*",
    )
    seen: set[Path] = {game_dir.resolve()}
    for pat in patterns:
        try:
            for sub in sorted(game_dir.glob(pat)):
                if sub.is_dir():
                    res = sub.resolve()
                    if res not in seen:
                        seen.add(res)
                        dirs.append(sub)
        except OSError:
            continue
    return dirs


def _infer_proxy_owner(dll_path: Path, game_dir: Path) -> str:
    name = dll_path.name.lower()
    parent = dll_path.parent
    if name == "winhttp.dll" and ((game_dir / "BepInEx").is_dir() or (parent / "doorstop_config.ini").is_file()):
        return "BepInEx / Doorstop"
    if name == "dwmapi.dll" and ((parent / "UE4SS.dll").is_file() or (parent / "ue4ss" / "UE4SS.dll").is_file()):
        return "RE-UE4SS"
    if name in ("dxgi.dll", "d3d11.dll", "d3d9.dll") and (
        (parent / "ReShade.ini").is_file() or (game_dir / "ReShade.ini").is_file() or (parent / "reshade-shaders").is_dir()
    ):
        return "ReShade"
    return "Existing external proxy DLL"


def inspect_conflict_guard(
    game_dir: Path | str,
    *,
    intended_action: str = "",
) -> dict[str, Any]:
    """Scan *game_dir* for Vortex/MO2 deployment markers, existing proxy DLLs, and ``~mods`` .pak overrides."""
    gdir = Path(str(game_dir or "").strip())
    if not str(game_dir or "").strip() or not gdir.is_dir():
        return {
            "status": "ok",
            "has_conflicts": False,
            "has_mod_manager": False,
            "detected_managers": [],
            "mod_manager_markers": [],
            "proxy_dlls": [],
            "pak_overrides": [],
            "warnings": [],
            "summary": "Game directory not found on disk; no local conflicts inspected.",
        }

    search_dirs = _candidate_search_dirs(gdir)
    manager_markers: list[dict[str, str]] = []
    detected_managers: list[str] = []
    seen_marker_paths: set[Path] = set()

    proxy_dlls: list[dict[str, Any]] = []
    seen_proxy_paths: set[Path] = set()

    for d in search_dirs:
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        for item in sorted(children, key=lambda p: p.name.lower()):
            low = item.name.lower()
            res = item.resolve()
            if low in MOD_MANAGER_MARKERS and res not in seen_marker_paths:
                seen_marker_paths.add(res)
                mgr = MOD_MANAGER_MARKERS[low]
                if mgr not in detected_managers:
                    detected_managers.append(mgr)
                try:
                    rel_str = res.relative_to(gdir.resolve()).as_posix()
                except ValueError:
                    rel_str = item.name
                manager_markers.append({
                    "manager": mgr,
                    "marker": item.name,
                    "rel_path": rel_str,
                    "path": str(item),
                })

            if item.is_file() and low in PROXY_DLL_NAMES and res not in seen_proxy_paths:
                seen_proxy_paths.add(res)
                try:
                    rel_str = res.relative_to(gdir.resolve()).as_posix()
                except ValueError:
                    rel_str = item.name
                proxy_dlls.append({
                    "dll": item.name,
                    "rel_path": rel_str,
                    "path": str(item),
                    "owner_hint": _infer_proxy_owner(item, gdir),
                    "size_bytes": item.stat().st_size,
                })

    pak_overrides: list[str] = []
    for pdir in search_dirs:
        if pdir.name.lower() == "~mods":
            for pf in sorted(pdir.rglob("*.pak")):
                if pf.is_file():
                    try:
                        pak_overrides.append(pf.resolve().relative_to(gdir.resolve()).as_posix())
                    except ValueError:
                        pak_overrides.append(pf.name)

    action_low = str(intended_action or "").strip().lower()
    warnings: list[dict[str, str]] = []

    if manager_markers:
        mgr_label = " / ".join(detected_managers)
        first = manager_markers[0]
        warnings.append({
            "code": "mod_manager_detected",
            "severity": "high",
            "title": f"{mgr_label} Mod Manager Setup Detected",
            "message": (
                f"Detected {mgr_label} deployment marker(s): "
                + ", ".join(m["rel_path"] for m in manager_markers[:4])
                + ". Installing loaders or overrides directly may overwrite or conflict with managed links."
            ),
            "path": first["path"],
        })

    proxy_names_found = {p["dll"].lower(): p for p in proxy_dlls}
    check_winhttp = action_low in ("", "bepinex", "bepinex_il2cpp", "bepinex_mono", "provision", "mod_install")
    check_dwmapi = action_low in ("", "ue4ss", "mod_install")

    if check_winhttp and "winhttp.dll" in proxy_names_found:
        pinfo = proxy_names_found["winhttp.dll"]
        warnings.append({
            "code": "proxy_dll_winhttp",
            "severity": "high" if action_low in ("bepinex", "bepinex_il2cpp", "bepinex_mono", "provision") else "warning",
            "title": "Existing winhttp.dll Proxy Detected",
            "message": (
                f"'{pinfo['rel_path']}' ({pinfo['owner_hint']}) already exists. "
                "Installing BepInEx will replace winhttp.dll unless backed up first."
            ),
            "path": pinfo["path"],
        })

    if check_dwmapi and "dwmapi.dll" in proxy_names_found:
        pinfo = proxy_names_found["dwmapi.dll"]
        warnings.append({
            "code": "proxy_dll_dwmapi",
            "severity": "high" if action_low == "ue4ss" else "warning",
            "title": "Existing dwmapi.dll Proxy Detected",
            "message": (
                f"'{pinfo['rel_path']}' ({pinfo['owner_hint']}) already exists. "
                "Installing RE-UE4SS may overwrite dwmapi.dll."
            ),
            "path": pinfo["path"],
        })

    other_proxies = [p for p in proxy_dlls if p["dll"].lower() not in ("winhttp.dll", "dwmapi.dll")]
    if other_proxies:
        warnings.append({
            "code": "other_proxy_dlls",
            "severity": "warning",
            "title": "Existing Proxy DLLs Present",
            "message": (
                "Found additional proxy DLL(s): "
                + ", ".join(f"{p['dll']} ({p['owner_hint']})" for p in other_proxies[:4])
                + ". Verify loader chaining order."
            ),
            "path": other_proxies[0]["path"],
        })

    if pak_overrides and action_low in ("pak_override", "ue4ss", "mod_install"):
        warnings.append({
            "code": "pak_overrides_present",
            "severity": "warning",
            "title": "Existing ~mods .pak Overrides Detected",
            "message": f"Found {len(pak_overrides)} existing .pak override(s) under ~mods/. Verify load priority before adding new overrides.",
            "path": str(gdir),
        })

    has_conflicts = bool(warnings)
    summary = (
        f"{len(warnings)} conflict warning(s): " + "; ".join(w["title"] for w in warnings)
        if warnings
        else "No Vortex/MO2 markers or conflicting proxy DLLs detected."
    )
    return {
        "status": "ok",
        "has_conflicts": has_conflicts,
        "has_mod_manager": bool(manager_markers),
        "detected_managers": detected_managers,
        "mod_manager_markers": manager_markers,
        "proxy_dlls": proxy_dlls,
        "pak_overrides": pak_overrides,
        "warnings": warnings,
        "summary": summary,
    }

"""Game discovery across Steam/Epic/GOG/Xbox/manual paths, CSV reference lookup, and Steam asset helpers."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from app.core.config import (
    DATA_DIR,
    DEFAULT_BEPINEX_SHA256,
    DEFAULT_BEPINEX_ZIP,
    DEFAULT_MCP_APP,
    DEFAULT_PLUGIN_BUILD_DIR,
    DEFAULT_VENV_PYTHON,
    LIBRARY_CACHE_PATH,
    SCAN_EXCLUSIONS_PATH,
    STEAM_ASSETS_CACHE,
    STEAM_ASSETS_CACHE_PATH,
    atomic_write_json,
)
from app.core.constants import (
    ANTI_CHEAT_BINARIES,
    ENGINE_ADAPTER_REGISTRY,
    NON_GAME_NAME_PATTERNS,
    NON_GAME_STEAM_APPIDS,
)
from app.engines.unreal import has_unreal_editor_without_shipping

STEAM_REF_CSV = DATA_DIR / "steam_games_reference.csv"
_STEAM_REF_CACHE: list[dict[str, str]] | None = None


def load_steam_reference_sheet() -> list[dict[str, str]]:
    global _STEAM_REF_CACHE
    if _STEAM_REF_CACHE is not None:
        return _STEAM_REF_CACHE

    rows: list[dict[str, str]] = []
    if STEAM_REF_CSV.is_file():
        with STEAM_REF_CSV.open("r", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                rows.append(row)
    _STEAM_REF_CACHE = rows
    return rows


def lookup_sheet_reference(product_name: str, folder_name: str) -> dict[str, str] | None:
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    rows = load_steam_reference_sheet()
    targets = [t for t in (norm(product_name), norm(folder_name)) if t]

    for t in targets:
        for r in rows:
            if norm(r.get("Game Title", "")) == t:
                return r

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
    atomic_write_json(SCAN_EXCLUSIONS_PATH, sorted(paths))


def make_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "game"


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


def _find_steam_appid_and_assets(game_dir: Path) -> tuple[str | None, dict[str, Path]]:
    """Resolve Steam AppID from appmanifest_*.acf and locate local librarycache artwork."""
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

    if len(assets) < 4:
        search_dirs = [
            game_dir,
            game_dir / "Assets",
            game_dir / "Content" / "Splash",
            game_dir / "Resources",
            game_dir / "Images",
            game_dir / "support",
        ]
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
        atomic_write_json(STEAM_ASSETS_CACHE_PATH, STEAM_ASSETS_CACHE)
    except Exception:
        pass
    return (appid, assets)


def discover_platform_roots() -> list[tuple[str, Path]]:
    """Find installed PC game platform libraries across local drives."""
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

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\GalaxyClient\paths"
        ) as key:
            val, _ = winreg.QueryValueEx(key, "client")
            if val:
                add_root("GOG Galaxy", Path(str(val)).parent / "Games")
    except OSError:
        pass

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


def is_non_game_entry(
    app_id: str | int | None = None,
    name: str = "",
    install_dir: Path | str | None = None,
) -> bool:
    """Return True if a Steam AppID, title, or install directory represents a non-game tool/runtime/editor."""
    aid = str(app_id or "").strip()
    if aid and aid in NON_GAME_STEAM_APPIDS:
        return True

    dir_path: Path | None = None
    if install_dir:
        raw_dir = str(install_dir).strip()
        if raw_dir:
            dir_path = Path(raw_dir)

    dir_name = ""
    if dir_path is not None:
        dir_name = (
            dir_path.parent.name
            if dir_path.name.lower() == "content" and dir_path.parent.name
            else dir_path.name
        )

    for text in (str(name or "").strip().lower(), dir_name.strip().lower()):
        if text and any(pat in text for pat in NON_GAME_NAME_PATTERNS):
            return True

    if dir_path is not None and has_unreal_editor_without_shipping(dir_path):
        return True

    return False


def is_game_candidate(
    folder: Path,
    discovery_scope: str = "all",
    platform_label: str = "",
) -> tuple[bool, str]:
    """Check whether a directory matches the requested discovery_scope and return (matched, runtime_kind)."""
    if not folder.is_dir():
        return (False, "")

    raw_name = folder.parent.name if folder.name.lower() == "content" and folder.parent.name else folder.name
    if is_non_game_entry(None, raw_name, folder):
        return (False, "")

    scope = (discovery_scope or "all").strip().lower()
    plat_lower = (platform_label or "").lower()

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


def build_managed_game_from_folder(folder: Path, platform_label: str, runtime_kind: str) -> dict[str, Any]:
    """Create a lightweight managed game entry without scraping metadata yet."""
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


def _normalize_library_title(title: str) -> str:
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
    atomic_write_json(LIBRARY_CACHE_PATH, cache)


def _parse_vdf_keyvalues(text: str) -> list[tuple[str, str]]:
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


def scan_steam_installed(steam_root: Path | None = None) -> list[dict[str, Any]]:
    """Steam Connector: read libraryfolders.vdf and appmanifest_*.acf where StateFlags == 4."""
    entries: list[dict[str, Any]] = []
    resolved_root = steam_root if steam_root is not None else _locate_steam_root()
    if not resolved_root:
        return entries

    lib_paths: list[Path] = [resolved_root]
    vdf_path = resolved_root / "steamapps" / "libraryfolders.vdf"
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
                if is_non_game_entry(appid, name, gdir):
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
    """Epic Games Connector: read *.item JSON manifests where bIsIncompleteInstall is false."""
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
            if is_non_game_entry(cat_id, display_name, install_loc):
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
    """GOG Galaxy Connector: read installed entries from galaxy-2.0.db and registry."""
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
        if p.is_dir() and not is_non_game_entry(None, p.name, p):
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
        has_exe = any(r.glob("*.exe"))
        if has_exe:
            if not is_non_game_entry(None, r.name, r):
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
                    if is_non_game_entry(None, child.name, child):
                        continue
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
    """Query PCGamingWiki Cargo API for owned_uninstalled entries with rate limiting & cache."""
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
                req = urllib.request.Request(url, headers={"User-Agent": "TheModdingTool/1.0 (Local Game Library)"})
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
        req = urllib.request.Request(xml_url, headers={"User-Agent": "TheModdingTool/1.0"})
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

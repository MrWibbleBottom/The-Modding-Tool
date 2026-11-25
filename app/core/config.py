"""Shared configuration, `%LOCALAPPDATA%\\TheModdingTool\\` paths, and in-memory state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.constants import ANTI_CHEAT_BINARIES, PLUGIN_DEPLOY_FILES


def get_resource_root() -> Path:
    """Return the root directory for bundled read-only resources (PyInstaller-safe)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent.parent


RESOURCE_ROOT = get_resource_root()
WORKSPACE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent.parent
)
APP_DIR = RESOURCE_ROOT / "app"
STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"
DATA_DIR = APP_DIR / "data"


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _resolve_default_bepinex_zip() -> str:
    zip_name = "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.793+dbc82f0.zip"
    candidates = [
        WORKSPACE_DIR / "assets" / zip_name,
        Path.home() / "Downloads" / zip_name,
        Path(r"D:\Downloads") / zip_name,
    ]
    for c in candidates:
        if _safe_is_file(c):
            return str(c)
    return str(WORKSPACE_DIR / "assets" / zip_name)


DEFAULT_GAME_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Battlemarked"
DEFAULT_BEPINEX_ZIP = _resolve_default_bepinex_zip()
DEFAULT_BEPINEX_SHA256 = "5b63d7d0e92a2448ccd13b738ff95ff1cc1026c6d718555d4df8e6c824953ac4"


_LEGACY_UNPREFIXED_MCP_RE = re.compile(r"The-Modding-Tool([\\/])bepinex-mcp(?![\\/]?bridges)")
_LEGACY_MCP_VENV_RE = re.compile(r"bridges[\\/]bepinex-mcp[\\/]\.venv")


def _sanitize_legacy_string(value: str) -> str:
    cleaned = value.replace("Battleground", "The-Modding-Tool")
    cleaned = cleaned.replace("ModToolPoC", "TheModdingTool")
    cleaned = _LEGACY_UNPREFIXED_MCP_RE.sub(r"The-Modding-Tool\1bridges\1bepinex-mcp", cleaned)
    cleaned = _LEGACY_MCP_VENV_RE.sub(".venv", cleaned)
    return cleaned


def sanitize_legacy_paths(obj: Any) -> Any:
    """Recursively normalize legacy workspace/AppData/bridge paths inside loaded JSON structures."""
    if isinstance(obj, str):
        return _sanitize_legacy_string(obj)
    if isinstance(obj, list):
        return [sanitize_legacy_paths(item) for item in obj]
    if isinstance(obj, dict):
        return {k: sanitize_legacy_paths(v) for k, v in obj.items()}
    return obj


def load_json_sanitized(path: Path, default: Any = None, *, persist: bool = True) -> Any:
    """Load JSON from *path*, normalize legacy paths via `sanitize_legacy_paths`, and save atomically if changed."""
    target = Path(path)
    if not target.is_file():
        return default
    raw = json.loads(target.read_text(encoding="utf-8"))
    cleaned = sanitize_legacy_paths(raw)
    if persist and cleaned != raw:
        try:
            atomic_write_json(target, cleaned)
        except OSError:
            pass
    return cleaned


def migrate_legacy_appdata(new_root: Path, legacy_root: Path | None = None) -> bool:
    """One-time migration copying `%LOCALAPPDATA%\\ModToolPoC\\` to `new_root` when `new_root` does not yet exist."""
    if legacy_root is None:
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        legacy_root = Path(local_app_data) / "ModToolPoC"
    if new_root.exists() or not legacy_root.is_dir():
        return False
    try:
        shutil.copytree(legacy_root, new_root, dirs_exist_ok=True)
        for json_file in new_root.rglob("*.json"):
            try:
                load_json_sanitized(json_file, persist=True)
            except Exception:
                continue
        return True
    except OSError:
        return False


def get_app_data_root() -> Path:
    override = os.environ.get("MODTOOL_DATA_DIR")
    if override:
        root = Path(override)
        root.mkdir(parents=True, exist_ok=True)
        return root
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    root = Path(local_app_data) / "TheModdingTool"
    try:
        migrate_legacy_appdata(root)
        root.mkdir(parents=True, exist_ok=True)
        # Verify directory is actually accessible/writable
        (root / "manifests").mkdir(parents=True, exist_ok=True)
        return root
    except OSError:
        import tempfile

        fallback = Path(tempfile.gettempdir()) / "TheModdingTool"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


APP_DATA_ROOT = get_app_data_root()
AUDIT_LOG_PATH = APP_DATA_ROOT / "audit.jsonl"
AUDIT_GAME_LOG_PATH = APP_DATA_ROOT / "audit_game.jsonl"
AUDIT_APP_LOG_PATH = APP_DATA_ROOT / "audit_app.jsonl"
MANIFEST_DIR = APP_DATA_ROOT / "manifests"
BACKUP_ROOT = APP_DATA_ROOT / "backups"
TOOLS_ROOT = APP_DATA_ROOT / "tools"
BUILT_MODS_DIR = APP_DATA_ROOT / "built_mods"
GAMES_STORE_PATH = APP_DATA_ROOT / "managed_games.json"
PC_SPECS_CACHE_PATH = APP_DATA_ROOT / "pc_specs.json"
STEAM_ASSETS_CACHE_PATH = APP_DATA_ROOT / "steam_assets_cache.json"
APP_UI_CONFIG_PATH = APP_DATA_ROOT / "app_ui_config.json"
BRANDING_DIR = APP_DATA_ROOT / "branding"
BRANDING_BANNERS_DIR = BRANDING_DIR / "banners"
BRANDING_FAVICONS_DIR = BRANDING_DIR / "favicons"
TOOL_VERSIONS_PATH = APP_DATA_ROOT / "tool_versions.json"
GAME_ICONS_DIR = APP_DATA_ROOT / "game_icons"
UNREAL_INI_BACKUP_DIR = APP_DATA_ROOT / "unreal_ini_backups"
SCAN_EXCLUSIONS_PATH = APP_DATA_ROOT / "scan_exclusions.json"
LIBRARY_CACHE_PATH = APP_DATA_ROOT / "library_cache.json"
UI_SETTINGS_PATH = APP_DATA_ROOT / "ui_settings.json"
REPO_CONFIG_PATH = APP_DATA_ROOT / "repo_config.json"
APP_SNAPSHOTS_DIR = APP_DATA_ROOT / "app_snapshots"
ASSET_AUDITS_DIR = APP_DATA_ROOT / "asset_audits"
DEV_MODE_PATH = APP_DATA_ROOT / "developer_mode.json"


def get_bepinex_bridge_roots() -> list[Path]:
    """Return candidate bepinex-mcp bridge roots, preferring `bridges/bepinex-mcp` over `%LOCALAPPDATA%\\TheModdingTool\\tools\\bepinex-mcp`."""
    candidates = [
        WORKSPACE_DIR / "bridges" / "bepinex-mcp",
        RESOURCE_ROOT / "bridges" / "bepinex-mcp",
        TOOLS_ROOT / "bepinex-mcp",
    ]
    roots: list[Path] = []
    for c in candidates:
        if c not in roots:
            roots.append(c)
    return roots


def _resolve_default_mcp_app() -> str:
    for root in get_bepinex_bridge_roots():
        candidate = root / "mcp" / "ModdersHelperApp.py"
        if _safe_is_file(candidate):
            return str(candidate)
    return str(WORKSPACE_DIR / "bridges" / "bepinex-mcp" / "mcp" / "ModdersHelperApp.py")


def _resolve_default_venv_python() -> str:
    candidates = [
        WORKSPACE_DIR / ".venv" / "Scripts" / "python.exe",
        RESOURCE_ROOT / ".venv" / "Scripts" / "python.exe",
    ]
    for c in candidates:
        if _safe_is_file(c):
            return str(c)
    return sys.executable


def _resolve_default_plugin_build_dir() -> str:
    for root in get_bepinex_bridge_roots():
        candidate = root / "plugins" / "BepInExMCP.IL2CPP" / "bin" / "Release" / "net6.0"
        if _safe_is_dir(candidate):
            return str(candidate)
    return str(
        WORKSPACE_DIR / "bridges" / "bepinex-mcp" / "plugins" / "BepInExMCP.IL2CPP" / "bin" / "Release" / "net6.0"
    )


DEFAULT_MCP_APP = _resolve_default_mcp_app()
DEFAULT_VENV_PYTHON = _resolve_default_venv_python()
DEFAULT_PLUGIN_BUILD_DIR = _resolve_default_plugin_build_dir()

for _d in (MANIFEST_DIR, BACKUP_ROOT, TOOLS_ROOT, BUILT_MODS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


# Shared in-memory state & caches
GAMES_RESPONSE_CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
PC_SPECS_RESPONSE_CACHE: dict[str, Any] | None = None
STEAM_ASSETS_CACHE: dict[str, Any] = {}


def clear_pc_specs_cache() -> None:
    global PC_SPECS_RESPONSE_CACHE
    PC_SPECS_RESPONSE_CACHE = None
SCAN_PROGRESS: dict[str, Any] = {
    "active": False,
    "done": 0,
    "total": 0,
    "current": "",
    "started_at": 0.0,
}
ICON_EXTRACT_SEMAPHORE = asyncio.Semaphore(4)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def _load_workspace_module(module_name: str, filename: str):
    import importlib.util

    mod_path = WORKSPACE_DIR / filename
    if not mod_path.is_file():
        mod_path = RESOURCE_ROOT / filename
    if not mod_path.is_file():
        raise HTTPException(status_code=404, detail=f"Module not found: {filename}")
    spec = importlib.util.spec_from_file_location(module_name, str(mod_path))
    if not spec or not spec.loader:
        raise HTTPException(status_code=500, detail=f"Failed to load module: {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def invalidate_games_cache() -> None:
    GAMES_RESPONSE_CACHE["ts"] = 0.0
    GAMES_RESPONSE_CACHE["payload"] = None


def default_managed_games() -> dict[str, Any]:
    return {
        "selected_id": "battleground",
        "games": [
            {
                "id": "battleground",
                "title": "Demeo x Dungeons & Dragons: Battlemarked",
                "game_dir": DEFAULT_GAME_DIR,
                "executable": "Battlemarked.exe",
                "mcp_app": DEFAULT_MCP_APP,
                "venv_python": DEFAULT_VENV_PYTHON,
                "plugin_build_dir": DEFAULT_PLUGIN_BUILD_DIR,
                "bepinex_zip": DEFAULT_BEPINEX_ZIP,
                "game_ip": "127.0.0.1",
                "bepinex_port": 23455,
                "steam_appid": "",
                "engine": "Unity IL2CPP",
                "anti_cheat": "None",
                "modding_notes": (
                    "Full Unity IL2CPP live scene/component/Harmony inspection via "
                    "BepInEx 6 + BepInExMCP."
                ),
                "source": "default",
                "pinned": True,
                "hidden": False,
                "last_played": "",
                "playtime_minutes": 0,
                "user_notes": "",
            }
        ],
    }


def load_managed_games() -> dict[str, Any]:
    if not GAMES_STORE_PATH.is_file():
        data = default_managed_games()
        save_managed_games(data)
        return data
    try:
        raw = json.loads(GAMES_STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("games"), list):
            raise ValueError("Invalid managed_games.json structure")
        data = sanitize_legacy_paths(raw)
        if data != raw:
            atomic_write_json(GAMES_STORE_PATH, data)
            invalidate_games_cache()
        for g in data["games"]:
            g.setdefault("pinned", False)
            g.setdefault("hidden", False)
            g.setdefault("last_played", "")
            g.setdefault("playtime_minutes", 0)
            g.setdefault("user_notes", "")
        return data
    except Exception:
        data = default_managed_games()
        save_managed_games(data)
        return data


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Write JSON atomically via temp file in the same directory, flush + fsync, and os.replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.stem}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(target))
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save_managed_games(data: dict[str, Any]) -> None:
    atomic_write_json(GAMES_STORE_PATH, data)
    invalidate_games_cache()


def manifest_path_for_game(game_dir: Path) -> Path:
    key = sha256_text(str(game_dir.resolve()).lower())[:16]
    return MANIFEST_DIR / f"manifest_{key}.json"


def backup_dir_for_game(game_dir: Path) -> Path:
    key = sha256_text(str(game_dir.resolve()).lower())[:16]
    return BACKUP_ROOT / f"backup_{key}"

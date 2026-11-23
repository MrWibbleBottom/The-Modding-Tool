#!/usr/bin/env python3
"""
Minimal local FastAPI controller for Unity IL2CPP modding PoC via bepinex-mcp.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import zipfile
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

POC_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = POC_DIR.parent

def _resolve_default_venv_python() -> str:
    candidates = [
        WORKSPACE_DIR / ".venv" / "Scripts" / "python.exe",
        WORKSPACE_DIR / "bepinex-mcp" / ".venv" / "Scripts" / "python.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return sys.executable


def _resolve_default_bepinex_zip() -> str:
    zip_name = "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.793+dbc82f0.zip"
    candidates = [
        WORKSPACE_DIR / "assets" / zip_name,
        Path.home() / "Downloads" / zip_name,
        Path(r"D:\Downloads") / zip_name,
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return str(WORKSPACE_DIR / "assets" / zip_name)


DEFAULT_GAME_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Battlemarked"
DEFAULT_MCP_APP = str(WORKSPACE_DIR / "bepinex-mcp" / "mcp" / "ModdersHelperApp.py")
DEFAULT_VENV_PYTHON = _resolve_default_venv_python()
DEFAULT_PLUGIN_BUILD_DIR = str(
    WORKSPACE_DIR / "bepinex-mcp" / "plugins" / "BepInExMCP.IL2CPP" / "bin" / "Release" / "net6.0"
)
DEFAULT_BEPINEX_ZIP = _resolve_default_bepinex_zip()
DEFAULT_BEPINEX_SHA256 = "5b63d7d0e92a2448ccd13b738ff95ff1cc1026c6d718555d4df8e6c824953ac4"

ANTI_CHEAT_BINARIES = {
    "easyanticheat.exe": "EasyAntiCheat.exe",
    "beservice.exe": "BEService.exe",
    "vgk.sys": "vgk.sys",
    "gameguard.des": "GameGuard.des",
}

PLUGIN_DEPLOY_FILES = [
    "BepInExMCP.IL2CPP.dll",
    "BepInExMCP.IL2CPP.deps.json",
    "Microsoft.CodeAnalysis.dll",
    "Microsoft.CodeAnalysis.CSharp.dll",
    "BepInExMCP.IL2CPP.pdb",
]


def get_app_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    root = Path(local_app_data) / "ModToolPoC"
    root.mkdir(parents=True, exist_ok=True)
    return root


AUDIT_LOG_PATH = get_app_data_root() / "audit.jsonl"
MANIFEST_DIR = get_app_data_root() / "manifests"
BACKUP_ROOT = get_app_data_root() / "backups"
TOOLS_ROOT = get_app_data_root() / "tools"
BUILT_MODS_DIR = get_app_data_root() / "built_mods"
GAMES_STORE_PATH = get_app_data_root() / "managed_games.json"
PC_SPECS_CACHE_PATH = get_app_data_root() / "pc_specs.json"
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
TOOLS_ROOT.mkdir(parents=True, exist_ok=True)
BUILT_MODS_DIR.mkdir(parents=True, exist_ok=True)


def default_managed_games() -> dict[str, Any]:
    return {
        "active_game_id": "battleground",
        "games": [
            {
                "id": "battleground",
                "name": "Battleground (Battlemarked)",
                "tab_label": "Battleground",
                "has_dedicated_tab": True,
                "runtime": "Unity_IL2CPP",
                "game_dir": DEFAULT_GAME_DIR,
                "bepinex_zip_path": DEFAULT_BEPINEX_ZIP,
                "expected_sha256": DEFAULT_BEPINEX_SHA256,
                "plugin_build_dir": DEFAULT_PLUGIN_BUILD_DIR,
                "venv_python_path": DEFAULT_VENV_PYTHON,
                "mcp_app_path": DEFAULT_MCP_APP,
                "game_ip": "localhost",
                "default_entity": "paladin_piece",
                "default_variable": "stat_hp_max",
                "default_go_id": "-269924",
                "default_component": "Jade.GameView.CameraTargetCharacter",
                "default_member": "targetPiece.stats.Health.maxValue",
                "default_value": "23",
                "default_profile": "battlemarked_stat_hp_max",
                "default_selector": {
                    "scene": "27653627778046a48b230901ca6f384c",
                    "path": "/LevelSceneRoot[0]/Piece_DragonbornPaladin%28Clone%29[10]",
                    "name": "Piece_DragonbornPaladin(Clone)",
                    "component": "Jade.GameView.CameraTargetCharacter",
                },
                "notes": "Demeo Battlemarked — Unity 6000.3.7f1 IL2CPP (BepInEx 6.0.0-be.793)",
            }
        ],
    }


def load_managed_games() -> dict[str, Any]:
    if not GAMES_STORE_PATH.is_file():
        data = default_managed_games()
        save_managed_games(data)
        return data
    try:
        data = json.loads(GAMES_STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "games" not in data or not isinstance(data["games"], list):
            raise ValueError("Invalid managed_games.json structure")
        # Ensure battleground always exists
        if not any(g.get("id") == "battleground" for g in data["games"]):
            data["games"].insert(0, default_managed_games()["games"][0])
            save_managed_games(data)
        return data
    except Exception:
        data = default_managed_games()
        save_managed_games(data)
        return data


GAMES_RESPONSE_CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
PC_SPECS_RESPONSE_CACHE: dict[str, Any] | None = None
AUDIT_LOG_CACHE: list[dict[str, Any]] | None = None
STEAM_ASSETS_CACHE_PATH = get_app_data_root() / "steam_assets_cache.json"
STEAM_ASSETS_CACHE: dict[str, Any] = {}


def invalidate_games_cache() -> None:
    GAMES_RESPONSE_CACHE["ts"] = 0.0
    GAMES_RESPONSE_CACHE["payload"] = None


def save_managed_games(data: dict[str, Any]) -> None:
    GAMES_STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    invalidate_games_cache()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


AUDIT_GAME_LOG_PATH = get_app_data_root() / "audit_game.jsonl"
AUDIT_APP_LOG_PATH = get_app_data_root() / "audit_app.jsonl"


def _classify_log_action(action: str) -> str:
    low = (action or "").strip().lower()
    if low.startswith(("app.", "app_ui.", "pc_specs.")):
        return "app"
    return "game"


def append_audit_log(
    action: str,
    status: str,
    summary: str,
    sha256: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    category = _classify_log_action(action)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "action": action,
        "status": status,
        "summary": summary,
        "sha256": sha256 or sha256_text(f"{action}:{status}:{summary}"),
        "details": details or {},
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with AUDIT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    cat_path = AUDIT_APP_LOG_PATH if category == "app" else AUDIT_GAME_LOG_PATH
    with cat_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    global AUDIT_LOG_CACHE
    AUDIT_LOG_CACHE = None
    return entry


def read_audit_logs(limit: int = 50, category: str = "all") -> list[dict[str, Any]]:
    global AUDIT_LOG_CACHE
    if AUDIT_LOG_CACHE is None:
        if not AUDIT_LOG_PATH.is_file():
            AUDIT_LOG_CACHE = []
        else:
            lines = AUDIT_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            entries: list[dict[str, Any]] = []
            for raw in lines[-400:]:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                    if "category" not in item:
                        item["category"] = _classify_log_action(item.get("action", ""))
                    entries.append(item)
                except json.JSONDecodeError:
                    continue
            AUDIT_LOG_CACHE = list(reversed(entries))

    cat = (category or "all").strip().lower()
    if cat in ("game", "app"):
        return [e for e in AUDIT_LOG_CACHE if e.get("category") == cat][:limit]
    return AUDIT_LOG_CACHE[:limit]


def manifest_path_for_game(game_dir: Path) -> Path:
    key = sha256_text(str(game_dir.resolve()).lower())[:16]
    return MANIFEST_DIR / f"manifest_{key}.json"


def backup_dir_for_game(game_dir: Path) -> Path:
    key = sha256_text(str(game_dir.resolve()).lower())[:16]
    return BACKUP_ROOT / f"backup_{key}"


class PersistentMcpStdioManager:
    """Maintains a single persistent MCP stdio session to ModdersHelperApp.py."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._python_path: str | None = None
        self._script_path: str | None = None
        self._game_ip: str | None = None

    async def ensure_session(
        self,
        python_path: str,
        script_path: str,
        game_ip: str = "localhost",
    ) -> ClientSession:
        if (
            self._session is not None
            and self._python_path == python_path
            and self._script_path == script_path
            and self._game_ip == game_ip
        ):
            return self._session

        await self._close_unlocked()
        if not Path(python_path).is_file():
            raise FileNotFoundError(f"MCP venv Python not found: {python_path}")
        if not Path(script_path).is_file():
            raise FileNotFoundError(f"ModdersHelperApp.py not found: {script_path}")

        server_params = StdioServerParameters(
            command=python_path,
            args=[script_path, "--transport=stdio", "--headless", f"--game-ip={game_ip}"],
            cwd=str(Path(script_path).resolve().parent),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(session.initialize(), timeout=20.0)
        except Exception:
            await stack.aclose()
            raise

        self._stack = stack
        self._session = session
        self._python_path = python_path
        self._script_path = script_path
        self._game_ip = game_ip
        append_audit_log(
            action="mcp.session_start",
            status="ok",
            summary=f"Started persistent MCP stdio session for {Path(script_path).name}",
            sha256=sha256_file(Path(script_path)),
            details={"python": python_path, "script": script_path, "game_ip": game_ip},
        )
        return session

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        python_path: str = DEFAULT_VENV_PYTHON,
        script_path: str = DEFAULT_MCP_APP,
        game_ip: str = "localhost",
    ) -> dict[str, Any]:
        async with self._lock:
            try:
                session = await self.ensure_session(python_path, script_path, game_ip)
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments or {}),
                    timeout=25.0,
                )
            except Exception:
                # Reconnect once if the stdio pipe broke
                await self._close_unlocked()
                session = await self.ensure_session(python_path, script_path, game_ip)
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments or {}),
                    timeout=25.0,
                )

            texts: list[str] = []
            for item in getattr(result, "content", []) or []:
                text = getattr(item, "text", None)
                if text is not None:
                    texts.append(text)
            combined = "\n".join(texts).strip()
            parsed: Any = combined
            try:
                parsed = json.loads(combined)
            except (TypeError, json.JSONDecodeError):
                pass
            return {
                "tool": tool_name,
                "isError": bool(getattr(result, "isError", False)),
                "raw": combined,
                "data": parsed,
                "persistentSession": True,
            }

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack = None
        self._session = None
        self._python_path = None
        self._script_path = None
        self._game_ip = None


mcp_manager = PersistentMcpStdioManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    append_audit_log(
        action="server.startup",
        status="ok",
        summary="PoC FastAPI controller initialized on 127.0.0.1:8765",
    )
    try:
        yield
    finally:
        await mcp_manager.close()


app = FastAPI(title="Battlemarked IL2CPP Modding PoC", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)


class FingerprintRequest(BaseModel):
    game_dir: str = Field(default=DEFAULT_GAME_DIR)


class ProvisionRequest(BaseModel):
    game_dir: str = Field(default=DEFAULT_GAME_DIR)
    bepinex_zip_path: str = Field(default=DEFAULT_BEPINEX_ZIP)
    expected_sha256: str = Field(default=DEFAULT_BEPINEX_SHA256)
    plugin_build_dir: str = Field(default=DEFAULT_PLUGIN_BUILD_DIR)


class RestoreRequest(BaseModel):
    game_dir: str = Field(default=DEFAULT_GAME_DIR)


class McpCallRequest(BaseModel):
    action: str = Field(
        description="One of: test_bridge, search, inspect, edit, profile_save_apply, raw_tool"
    )
    tool_name: str = Field(default="")
    arguments: dict[str, Any] = Field(default_factory=dict)
    python_path: str = Field(default=DEFAULT_VENV_PYTHON)
    mcp_app_path: str = Field(default=DEFAULT_MCP_APP)
    game_ip: str = Field(default="localhost")


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


class InstallToolRequest(BaseModel):
    tool_id: str
    game_id: str = Field(default="battleground")
    game_dir: str = Field(default=DEFAULT_GAME_DIR)


class OpenToolFolderRequest(BaseModel):
    path: str


class ImportUnrealSettingsRequest(BaseModel):
    game_id: str = Field(default="")
    custom_ini_path: str = Field(default="")
    reference_tier_id: str = Field(default="auto")


class ModEditRule(BaseModel):
    label: str = Field(default="Stat Edit")
    rule_type: str = Field(default="member_set")
    entity_name: str = Field(default="Piece_DragonbornPaladin(Clone)")
    component_name: str = Field(default="Jade.GameView.CameraTargetCharacter")
    member_path: str = Field(default="targetPiece.stats.Health.maxValue")
    value: str = Field(default="23")
    hook_kind: str = Field(default="Prefix")
    target_method: str = Field(default="")
    skip_original: bool = Field(default=False)


class BuildModRequest(BaseModel):
    game_id: str = Field(default="battleground")
    game_dir: str = Field(default=DEFAULT_GAME_DIR)
    mod_name: str = Field(default="Battlemarked Custom Balance Mod")
    mod_id: str = Field(default="com.modtool.battlemarked.custombalance")
    mod_version: str = Field(default="1.0.0")
    author: str = Field(default="ModToolPoC")
    apply_interval_sec: float = Field(default=2.0)
    edits: list[ModEditRule] = Field(default_factory=list)


class InstallBuiltModRequest(BaseModel):
    mod_slug: str
    game_id: str = Field(default="battleground")
    game_dir: str = Field(default=DEFAULT_GAME_DIR)


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(POC_DIR / "index.html")


@app.get("/api/defaults")
async def get_defaults() -> dict[str, Any]:
    return {
        "game_dir": DEFAULT_GAME_DIR,
        "mcp_app_path": DEFAULT_MCP_APP,
        "venv_python_path": DEFAULT_VENV_PYTHON,
        "plugin_build_dir": DEFAULT_PLUGIN_BUILD_DIR,
        "bepinex_zip_path": DEFAULT_BEPINEX_ZIP,
        "expected_sha256": DEFAULT_BEPINEX_SHA256,
        "audit_log_path": str(AUDIT_LOG_PATH),
        "stage4": {
            "game_object_name": "Piece_DragonbornPaladin(Clone)",
            "game_object_id": "-269924",
            "scene": "27653627778046a48b230901ca6f384c",
            "path": "/LevelSceneRoot[0]/Piece_DragonbornPaladin%28Clone%29[10]",
            "component_name": "Jade.GameView.CameraTargetCharacter",
            "alt_component_name": "Jade.GameView.PieceView",
            "member_name": "PreferredDragLayerMask",
            "member_value": "1007361",
            "ui_health_object_id": "-64174",
            "ui_health_path": "/ScreenspaceHudViewNonVR%28Clone%29[0]/UICanvas[0]/LeftStretchAnchor[0]/LeftStretchOffset[0]/TopLeftAnchor[0]/PartyViewNonVR[0]/RootStack[0]/TurnViews[0]/PartyPieceView1[1]/Offset[0]/Portrait[1]/PieceHealthBar2D[0]/HealthText[3]",
            "ui_health_component": "TMPro.TextMeshProUGUI",
            "ui_health_member": "text",
            "ui_health_value": "23 / 23 ",
            "selector": {
                "scene": "DontDestroyOnLoad",
                "path": "/ScreenspaceHudViewNonVR%28Clone%29[0]/UICanvas[0]/LeftStretchAnchor[0]/LeftStretchOffset[0]/TopLeftAnchor[0]/PartyViewNonVR[0]/RootStack[0]/TurnViews[0]/PartyPieceView1[1]/Offset[0]/Portrait[1]/PieceHealthBar2D[0]/HealthText[3]",
                "name": "HealthText",
                "component": "TMPro.TextMeshProUGUI",
            },
            "hero_selector": {
                "scene": "27653627778046a48b230901ca6f384c",
                "path": "/LevelSceneRoot[0]/Piece_DragonbornPaladin%28Clone%29[10]",
                "name": "Piece_DragonbornPaladin(Clone)",
                "component": "Jade.GameView.PieceView",
            },
            "profile_name": "paladin_hp_23",
        },
    }


@app.post("/api/fingerprint")
async def fingerprint_game(req: FingerprintRequest) -> dict[str, Any]:
    game_dir = Path(req.game_dir.strip())
    if not game_dir.is_dir():
        append_audit_log(
            action="fingerprint",
            status="error",
            summary=f"Target directory not found: {game_dir}",
        )
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {game_dir}")

    game_assembly = game_dir / "GameAssembly.dll"
    has_game_assembly = game_assembly.is_file()

    metadata_files = list(
        game_dir.glob("*_Data/il2cpp_data/Metadata/global-metadata.dat")
    )
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
            low = fname.lower()
            if low in ANTI_CHEAT_BINARIES:
                rel = os.path.relpath(os.path.join(root, fname), game_dir)
                detected_anticheat.append(rel)

    is_unity_il2cpp = has_game_assembly and has_global_metadata
    blocked = len(detected_anticheat) > 0
    runtime_kind = "Unity_IL2CPP" if is_unity_il2cpp else "Unknown"

    ga_sha256 = sha256_file(game_assembly) if has_game_assembly else None
    summary = (
        f"Fingerprint {game_dir.name}: runtime={runtime_kind}, "
        f"interop={has_interop}, blocked={blocked}"
    )
    result = {
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
    append_audit_log(
        action="fingerprint",
        status="blocked" if blocked else ("ok" if is_unity_il2cpp else "warning"),
        summary=summary,
        sha256=ga_sha256,
        details=result,
    )
    return result


@app.post("/api/provision")
async def provision_game(req: ProvisionRequest) -> dict[str, Any]:
    game_dir = Path(req.game_dir.strip())
    zip_path = Path(req.bepinex_zip_path.strip())
    plugin_build_dir = Path(req.plugin_build_dir.strip())
    expected_hash = req.expected_sha256.strip().lower()

    if not game_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Game folder not found: {game_dir}")
    if not zip_path.is_file():
        raise HTTPException(status_code=400, detail=f"BepInEx zip not found: {zip_path}")
    if not plugin_build_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Plugin build dir not found: {plugin_build_dir}")

    # Re-run anti-cheat guard
    fp = await fingerprint_game(FingerprintRequest(game_dir=str(game_dir)))
    if fp["blocked"]:
        raise HTTPException(
            status_code=403,
            detail=f"Provisioning blocked due to anti-cheat binaries: {fp['anti_cheat_detected']}",
        )

    actual_hash = sha256_file(zip_path)
    if expected_hash and actual_hash != expected_hash:
        append_audit_log(
            action="provision",
            status="error",
            summary=f"SHA-256 mismatch for {zip_path.name}",
            sha256=actual_hash,
            details={"expected": expected_hash, "actual": actual_hash},
        )
        raise HTTPException(
            status_code=400,
            detail=f"SHA-256 mismatch! Expected {expected_hash}, got {actual_hash}",
        )

    manifest_file = manifest_path_for_game(game_dir)
    backup_dir = backup_dir_for_game(game_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Load existing manifest if re-provisioning so we don't overwrite original backups
    existing_manifest: dict[str, Any] = {}
    if manifest_file.is_file():
        try:
            existing_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            existing_manifest = {}

    backed_up_files: dict[str, dict[str, str]] = dict(existing_manifest.get("backed_up_files", {}))
    added_files: dict[str, str] = dict(existing_manifest.get("added_files", {}))

    def backup_if_colliding(rel_path_str: str) -> None:
        target = game_dir / rel_path_str
        norm_rel = Path(rel_path_str).as_posix()
        if target.is_file() and norm_rel not in backed_up_files and norm_rel not in added_files:
            b_dest = backup_dir / norm_rel
            b_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, b_dest)
            backed_up_files[norm_rel] = {
                "backup_path": str(b_dest),
                "sha256": sha256_file(b_dest),
            }

    # 1. Extract BepInEx zip
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            rel_member = Path(member.filename).as_posix()
            backup_if_colliding(rel_member)
            dest_path = game_dir / rel_member
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as src, dest_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if rel_member not in backed_up_files:
                added_files[rel_member] = sha256_file(dest_path)

    # 2. Deploy compiled BepInExMCP.IL2CPP.dll and dependencies
    plugin_target_dir = game_dir / "BepInEx" / "plugins" / "BepInExMCP.IL2CPP"
    plugin_target_dir.mkdir(parents=True, exist_ok=True)
    deployed_plugin_files: list[str] = []

    for fname in PLUGIN_DEPLOY_FILES:
        src_file = plugin_build_dir / fname
        if not src_file.is_file():
            if fname.endswith(".pdb"):
                continue
            raise HTTPException(status_code=400, detail=f"Missing compiled plugin file: {src_file}")
        rel_dest = (Path("BepInEx") / "plugins" / "BepInExMCP.IL2CPP" / fname).as_posix()
        backup_if_colliding(rel_dest)
        dest_file = game_dir / rel_dest
        shutil.copy2(src_file, dest_file)
        file_hash = sha256_file(dest_file)
        if rel_dest not in backed_up_files:
            added_files[rel_dest] = file_hash
        deployed_plugin_files.append(rel_dest)

    plugin_dll_hash = sha256_file(plugin_target_dir / "BepInExMCP.IL2CPP.dll")
    manifest_data = {
        "game_dir": str(game_dir),
        "bepinex_zip_path": str(zip_path),
        "bepinex_zip_sha256": actual_hash,
        "plugin_dll_sha256": plugin_dll_hash,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "added_files": added_files,
        "backed_up_files": backed_up_files,
        "deployed_plugin_files": deployed_plugin_files,
    }
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    summary = (
        f"Provisioned BepInEx 6 IL2CPP + BepInExMCP.IL2CPP.dll "
        f"({len(added_files)} tracked added files, {len(backed_up_files)} backed up)"
    )
    append_audit_log(
        action="provision",
        status="ok",
        summary=summary,
        sha256=actual_hash,
        details={
            "manifest_path": str(manifest_file),
            "zip_sha256": actual_hash,
            "plugin_dll_sha256": plugin_dll_hash,
            "added_count": len(added_files),
            "backed_up_count": len(backed_up_files),
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "manifest_path": str(manifest_file),
        "bepinex_zip_sha256": actual_hash,
        "plugin_dll_sha256": plugin_dll_hash,
        "added_files_count": len(added_files),
        "backed_up_files_count": len(backed_up_files),
        "deployed_plugin_files": deployed_plugin_files,
    }


@app.post("/api/restore")
async def restore_game(req: RestoreRequest) -> dict[str, Any]:
    game_dir = Path(req.game_dir.strip())
    if not game_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Game folder not found: {game_dir}")

    manifest_file = manifest_path_for_game(game_dir)
    backup_dir = backup_dir_for_game(game_dir)

    deleted_files: list[str] = []
    restored_files: list[str] = []

    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        for rel_path in manifest.get("added_files", {}):
            target = game_dir / rel_path
            if target.is_file() or target.is_symlink():
                target.unlink()
                deleted_files.append(rel_path)

        for rel_path, meta in manifest.get("backed_up_files", {}).items():
            b_src = Path(meta["backup_path"])
            target = game_dir / rel_path
            if b_src.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(b_src, target)
                restored_files.append(rel_path)

    # Also remove known BepInEx root artefacts and generated BepInEx directories if not in backup
    backed_up_set = set(restored_files)
    known_root_files = [
        "winhttp.dll",
        "doorstop_config.ini",
        ".doorstop_version",
        "changelog.txt",
        "libdoorstop.dylib",
        "libdoorstop.so",
        "run_bepinex.sh",
    ]
    for root_fname in known_root_files:
        if root_fname not in backed_up_set:
            candidate = game_dir / root_fname
            if candidate.is_file():
                candidate.unlink()
                if root_fname not in deleted_files:
                    deleted_files.append(root_fname)

    for gen_dir_name in ["BepInEx", "dotnet", "mono"]:
        gen_dir = game_dir / gen_dir_name
        if gen_dir.is_dir():
            # Only remove if no backed up file lives inside it
            if not any(r.startswith(f"{gen_dir_name}/") for r in backed_up_set):
                shutil.rmtree(gen_dir)
                deleted_files.append(f"{gen_dir_name}/")

    if manifest_file.is_file():
        manifest_file.unlink()
    if backup_dir.is_dir():
        shutil.rmtree(backup_dir)

    # Confirm zero residual files remain
    residuals: list[str] = []
    for check_item in ["BepInEx", "dotnet", "winhttp.dll", "doorstop_config.ini", ".doorstop_version"]:
        if (game_dir / check_item).exists() and check_item not in backed_up_set:
            residuals.append(check_item)

    zero_residual = len(residuals) == 0
    summary = (
        f"Clean restore completed: deleted {len(deleted_files)} item(s), "
        f"restored {len(restored_files)} backup(s), zero_residual={zero_residual}"
    )
    append_audit_log(
        action="restore",
        status="ok" if zero_residual else "warning",
        summary=summary,
        details={
            "deleted_count": len(deleted_files),
            "restored_count": len(restored_files),
            "zero_residual": zero_residual,
            "residuals": residuals,
        },
    )
    return {
        "status": "ok" if zero_residual else "residual_detected",
        "summary": summary,
        "deleted_items": deleted_files,
        "restored_files": restored_files,
        "zero_residual": zero_residual,
        "residuals": residuals,
    }


@app.post("/api/mcp/call")
async def mcp_call(req: McpCallRequest) -> dict[str, Any]:
    action = req.action.strip().lower()
    args = req.arguments or {}

    try:
        if action == "test_bridge":
            ping_res = await mcp_manager.call_tool(
                "ping_unity_bridge", {}, req.python_path, req.mcp_app_path, req.game_ip
            )
            cap_res = await mcp_manager.call_tool(
                "get_runtime_capabilities", {}, req.python_path, req.mcp_app_path, req.game_ip
            )
            summary = "Executed ping_unity_bridge + get_runtime_capabilities over persistent MCP stdio"
            payload = {"ping": ping_res, "capabilities": cap_res}
            append_audit_log("mcp.test_bridge", "ok", summary, details=payload)
            return {"status": "ok", "action": action, "result": payload}

        if action == "search":
            mode = args.get("mode", "search_gameobjects")
            if mode == "find_objects_with_component":
                res = await mcp_manager.call_tool(
                    "find_objects_with_component",
                    {"component_name": str(args.get("component_name", ""))},
                    req.python_path,
                    req.mcp_app_path,
                    req.game_ip,
                )
            else:
                res = await mcp_manager.call_tool(
                    "search_gameobjects",
                    {
                        "name": str(args.get("name", "")),
                        "component_name": str(args.get("component_name", "")),
                        "tag": str(args.get("tag", "")),
                        "scene": str(args.get("scene", "")),
                        "include_inactive": bool(args.get("include_inactive", False)),
                        "limit": int(args.get("limit", 50)),
                    },
                    req.python_path,
                    req.mcp_app_path,
                    req.game_ip,
                )
            append_audit_log(
                "mcp.search",
                "ok",
                f"MCP {mode} query executed",
                details={"arguments": args},
            )
            return {"status": "ok", "action": action, "result": res}

        if action == "inspect":
            game_object_id = str(args.get("game_object_id", "")).strip()
            selector = args.get("selector")
            resolved_info = None
            if (not game_object_id) and isinstance(selector, dict):
                resolve_res = await mcp_manager.call_tool(
                    "resolve_gameobject_selector",
                    {"selector_json": json.dumps(selector)},
                    req.python_path,
                    req.mcp_app_path,
                    req.game_ip,
                )
                resolved_info = resolve_res.get("data")
                if isinstance(resolved_info, dict) and "id" in resolved_info:
                    game_object_id = str(resolved_info["id"])

            components_res = await mcp_manager.call_tool(
                "inspect_components",
                {"game_object_id": game_object_id},
                req.python_path,
                req.mcp_app_path,
                req.game_ip,
            )
            member_res = None
            comp_name = str(args.get("component_name", "")).strip()
            member_name = str(args.get("member_name", "")).strip()
            if comp_name and member_name:
                member_res = await mcp_manager.call_tool(
                    "get_component_member",
                    {
                        "game_object_id": game_object_id,
                        "component_name": comp_name,
                        "member_name": member_name,
                    },
                    req.python_path,
                    req.mcp_app_path,
                    req.game_ip,
                )
            payload = {
                "resolved_game_object_id": game_object_id,
                "resolved": resolved_info,
                "components": components_res,
                "member": member_res,
            }
            append_audit_log(
                "mcp.inspect",
                "ok",
                f"Inspected GameObject {game_object_id} ({comp_name}.{member_name})",
                details={"game_object_id": game_object_id, "component": comp_name, "member": member_name},
            )
            return {"status": "ok", "action": action, "result": payload}

        if action == "edit":
            game_object_id = str(args.get("game_object_id", "")).strip()
            selector = args.get("selector")
            if (not game_object_id) and isinstance(selector, dict):
                resolve_res = await mcp_manager.call_tool(
                    "resolve_gameobject_selector",
                    {"selector_json": json.dumps(selector)},
                    req.python_path,
                    req.mcp_app_path,
                    req.game_ip,
                )
                resolved_data = resolve_res.get("data")
                if isinstance(resolved_data, dict) and "id" in resolved_data:
                    game_object_id = str(resolved_data["id"])

            comp_name = str(args.get("component_name", "")).strip()
            member_name = str(args.get("member_name", "")).strip()
            value = str(args.get("value", ""))
            set_res = await mcp_manager.call_tool(
                "set_component_value",
                {
                    "game_object_id": game_object_id,
                    "component_name": comp_name,
                    "member_name": member_name,
                    "value": value,
                },
                req.python_path,
                req.mcp_app_path,
                req.game_ip,
            )
            verify_res = await mcp_manager.call_tool(
                "get_component_member",
                {
                    "game_object_id": game_object_id,
                    "component_name": comp_name,
                    "member_name": member_name,
                },
                req.python_path,
                req.mcp_app_path,
                req.game_ip,
            )
            payload = {"set_value": set_res, "verified_member": verify_res}
            append_audit_log(
                "mcp.edit",
                "ok",
                f"Edited {comp_name}.{member_name} = {value} on {game_object_id}",
                details=payload,
            )
            return {"status": "ok", "action": action, "result": payload}

        if action == "profile_save_apply":
            profile_name = str(args.get("profile_name", "paladin_hp_23")).strip()
            selector = args.get("selector")
            if not isinstance(selector, dict):
                raise ValueError("A stable selector object is required to save a profile.")
            comp_name = str(args.get("component_name", "")).strip()
            member_name = str(args.get("member_name", "")).strip()
            value = str(args.get("value", ""))
            profile_obj = {
                "version": 1,
                "name": profile_name,
                "autoApply": False,
                "operations": [
                    {
                        "id": "op_set_1",
                        "command": "component/set_value",
                        "selector": selector,
                        "componentName": comp_name,
                        "memberName": member_name,
                        "value": value,
                    }
                ],
            }
            save_res = await mcp_manager.call_tool(
                "save_mod_profile",
                {"name": profile_name, "profile_json": json.dumps(profile_obj)},
                req.python_path,
                req.mcp_app_path,
                req.game_ip,
            )
            apply_res = await mcp_manager.call_tool(
                "apply_mod_profile",
                {"name": profile_name},
                req.python_path,
                req.mcp_app_path,
                req.game_ip,
            )
            payload = {"saved_profile": save_res, "applied_profile": apply_res}
            append_audit_log(
                "mcp.profile",
                "ok",
                f"Saved and applied profile '{profile_name}'",
                details=payload,
            )
            return {"status": "ok", "action": action, "result": payload}

        if action == "raw_tool":
            if not req.tool_name.strip():
                raise ValueError("tool_name is required for raw_tool action.")
            res = await mcp_manager.call_tool(
                req.tool_name.strip(),
                args,
                req.python_path,
                req.mcp_app_path,
                req.game_ip,
            )
            append_audit_log(
                f"mcp.{req.tool_name.strip()}",
                "ok",
                f"Called MCP tool '{req.tool_name.strip()}'",
                details={"arguments": args},
            )
            return {"status": "ok", "action": action, "result": res}

        raise ValueError(f"Unsupported MCP action: {action}")
    except Exception as exc:
        append_audit_log(
            action=f"mcp.{action}",
            status="error",
            summary=f"MCP call '{action}' failed: {exc}",
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/logs")
async def get_logs(request: Request, limit: int = 50, category: str = "all"):
    limit = max(1, min(limit, 200))
    cat = (category or "all").strip().lower()
    entries = read_audit_logs(limit=limit, category=cat)
    all_entries = AUDIT_LOG_CACHE or []
    game_count = sum(1 for e in all_entries if e.get("category") == "game")
    app_count = sum(1 for e in all_entries if e.get("category") == "app")
    active_path = (
        AUDIT_GAME_LOG_PATH if cat == "game" else (AUDIT_APP_LOG_PATH if cat == "app" else AUDIT_LOG_PATH)
    )
    payload = {
        "audit_log_path": str(active_path),
        "category": cat,
        "game_count": game_count,
        "app_count": app_count,
        "count": len(entries),
        "entries": entries,
    }
    etag = f'W/"{sha256_text(json.dumps(payload, sort_keys=True, default=str))[:16]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=payload, headers={"ETag": etag})


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


@app.get("/api/games")
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


@app.post("/api/games/select")
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


@app.post("/api/games/rescan")
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


@app.get("/api/games/scan-progress")
async def get_scan_progress() -> dict[str, Any]:
    """Returns live progress for an active Game scan or Game info scan."""
    return {"status": "ok", **SCAN_PROGRESS}


@app.post("/api/games/scan-info")
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


@app.post("/api/games")
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


@app.delete("/api/games/{game_id}")
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


@app.post("/api/games/scan")
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


@app.post("/api/games/browse")
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


async def _download_and_extract_zip(url: str, target_dir: Path, archive_name: str) -> tuple[Path, str, int]:
    """Downloads a zip archive over HTTPS, computes its SHA-256, and extracts it into target_dir."""
    import httpx

    target_dir.mkdir(parents=True, exist_ok=True)
    archive_path = target_dir / archive_name
    async with httpx.AsyncClient(follow_redirects=True, timeout=90.0) as client:
        resp = await client.get(url, headers={"User-Agent": "ModToolPoC/1.0"})
        resp.raise_for_status()
        archive_path.write_bytes(resp.content)

    file_hash = sha256_file(archive_path)
    extracted_count = 0
    if archive_name.lower().endswith(".zip") and zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(target_dir)
            extracted_count = len(zf.namelist())
    return (archive_path, file_hash, extracted_count)


async def _install_github_release_tool(
    repo: str,
    target_dir: Path,
    asset_keyword: str = ".zip",
) -> tuple[str, str, int]:
    """Queries GitHub latest release API for repo, downloads matching asset (or saves release bundle), and extracts if zip."""
    import httpx

    target_dir.mkdir(parents=True, exist_ok=True)
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        resp = await client.get(api_url, headers={"User-Agent": "ModToolPoC/1.0"})
        resp.raise_for_status()
        rel = resp.json()

    tag_name = rel.get("tag_name", "latest")
    assets = rel.get("assets", []) or []
    chosen_asset = None
    for a in assets:
        aname = str(a.get("name", "")).lower()
        if asset_keyword.lower() in aname and aname.endswith(".zip"):
            chosen_asset = a
            break
    if chosen_asset is None:
        for a in assets:
            aname = str(a.get("name", "")).lower()
            if aname.endswith(".zip"):
                chosen_asset = a
                break
    if chosen_asset is None and assets:
        chosen_asset = assets[0]

    if chosen_asset and chosen_asset.get("browser_download_url"):
        dl_url = chosen_asset["browser_download_url"]
        aname = chosen_asset.get("name", f"{repo.replace('/', '_')}_{tag_name}.zip")
        _, file_hash, count = await _download_and_extract_zip(dl_url, target_dir, aname)
        (target_dir / "release_info.json").write_text(
            json.dumps({"repo": repo, "tag": tag_name, "asset": aname, "download_url": dl_url}, indent=2),
            encoding="utf-8",
        )
        return (f"{aname} ({tag_name})", file_hash, count)

    info_file = target_dir / "release_info.json"
    info_text = json.dumps(
        {"repo": repo, "tag": tag_name, "html_url": rel.get("html_url", f"https://github.com/{repo}/releases")},
        indent=2,
    )
    info_file.write_text(info_text, encoding="utf-8")
    return (f"{repo} ({tag_name})", sha256_text(info_text), 1)


@app.post("/api/tools/install")
async def install_modding_tool(req: InstallToolRequest) -> dict[str, Any]:
    """Installs or deploys a modding tool/app for the active game or workstation tools directory."""
    tool_id = req.tool_id.strip().lower()
    game_dir = Path(req.game_dir.strip()) if req.game_dir.strip() else Path(DEFAULT_GAME_DIR)
    dir_exists = game_dir.is_dir()

    try:
        if tool_id == "bepinex_il2cpp":
            zip_path = Path(DEFAULT_BEPINEX_ZIP)
            if not zip_path.is_file():
                raise HTTPException(status_code=400, detail=f"Local BepInEx 6 IL2CPP zip not found at {zip_path}")
            dest_dir = game_dir if dir_exists else (TOOLS_ROOT / "BepInEx6_IL2CPP")
            dest_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(dest_dir)
                count = len(zf.namelist())
            h = sha256_file(zip_path)
            msg = f"Extracted BepInEx 6 IL2CPP ({count} files) into {dest_dir}"

        elif tool_id == "bepinex_mcp_il2cpp":
            build_dir = Path(DEFAULT_PLUGIN_BUILD_DIR)
            if not build_dir.is_dir():
                raise HTTPException(status_code=400, detail=f"Plugin build directory not found: {build_dir}")
            dest_dir = (
                (game_dir / "BepInEx" / "plugins" / "BepInExMCP.IL2CPP")
                if dir_exists
                else (TOOLS_ROOT / "BepInExMCP.IL2CPP")
            )
            dest_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            for fname in REQUIRED_PLUGIN_FILES:
                src = build_dir / fname
                if src.is_file():
                    shutil.copy2(src, dest_dir / fname)
                    copied += 1
            main_dll = dest_dir / "BepInExMCP.IL2CPP.dll"
            h = sha256_file(main_dll) if main_dll.is_file() else sha256_text(str(dest_dir))
            msg = f"Deployed BepInExMCP.IL2CPP + Roslyn ({copied} files) to {dest_dir}"

        elif tool_id == "il2cpp_dumper":
            dest_dir = TOOLS_ROOT / "Il2CppDumper"
            url = "https://github.com/Perfare/Il2CppDumper/releases/download/v6.7.46/Il2CppDumper-net6-v6.7.46.zip"
            _, h, count = await _download_and_extract_zip(url, dest_dir, "Il2CppDumper-net6-v6.7.46.zip")
            msg = f"Downloaded & extracted Il2CppDumper v6.7.46 ({count} files) to {dest_dir}"

        elif tool_id == "unity_explorer_il2cpp":
            dest_dir = (
                (game_dir / "BepInEx")
                if (dir_exists and (game_dir / "GameAssembly.dll").is_file())
                else (TOOLS_ROOT / "UnityExplorer_IL2CPP")
            )
            url = "https://github.com/sinai-dev/UnityExplorer/releases/download/4.9.0/UnityExplorer.BepInEx6.IL2CPP.zip"
            _, h, count = await _download_and_extract_zip(url, dest_dir, "UnityExplorer.BepInEx6.IL2CPP.zip")
            msg = f"Installed UnityExplorer (BepInEx 6 IL2CPP, {count} files) to {dest_dir}"

        elif tool_id == "bepinex_mono":
            is_mono_game = dir_exists and not (game_dir / "GameAssembly.dll").is_file() and bool(list(game_dir.glob("*_Data")))
            dest_dir = game_dir if is_mono_game else (TOOLS_ROOT / "BepInEx5_Mono")
            url = "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.2/BepInEx_win_x64_5.4.23.2.zip"
            _, h, count = await _download_and_extract_zip(url, dest_dir, "BepInEx_win_x64_5.4.23.2.zip")
            msg = f"Installed BepInEx 5.4.23.2 (Unity Mono x64, {count} files) to {dest_dir}"

        elif tool_id == "dnspy_ex":
            dest_dir = TOOLS_ROOT / "dnSpyEx"
            url = "https://github.com/dnSpyEx/dnSpy/releases/download/v6.5.1/dnSpy-net-win64.zip"
            _, h, count = await _download_and_extract_zip(url, dest_dir, "dnSpy-net-win64.zip")
            msg = f"Installed dnSpyEx v6.5.1 Win64 ({count} files) to {dest_dir}"

        elif tool_id == "ue4ss":
            dest_dir = TOOLS_ROOT / "UE4SS"
            label, h, count = await _install_github_release_tool("UE4SS-RE/RE-UE4SS", dest_dir, "UE4SS")
            msg = f"Installed RE-UE4SS [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "fmodel":
            dest_dir = TOOLS_ROOT / "FModel"
            label, h, count = await _install_github_release_tool("4sval/FModel", dest_dir, "FModel")
            msg = f"Installed FModel [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "ghidra_mcp":
            dest_dir = TOOLS_ROOT / "GhidraMCP"
            label, h, count = await _install_github_release_tool("LaurieWired/GhidraMCP", dest_dir, "GhidraMCP")
            msg = f"Installed GhidraMCP [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "x64dbg":
            dest_dir = TOOLS_ROOT / "x64dbg"
            label, h, count = await _install_github_release_tool("x64dbg/x64dbg", dest_dir, "snapshot")
            msg = f"Installed x64dbg [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "gdre_tools":
            dest_dir = TOOLS_ROOT / "GDRETools"
            label, h, count = await _install_github_release_tool("GDRETools/gdsdecomp", dest_dir, "windows")
            msg = f"Installed GDRE Tools [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "mod_engine_2":
            dest_dir = TOOLS_ROOT / "ModEngine2"
            label, h, count = await _install_github_release_tool("soulsmods/ModEngine2", dest_dir, "ModEngine")
            msg = f"Installed Mod Engine 2 [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "reframework":
            dest_dir = TOOLS_ROOT / "REFramework"
            label, h, count = await _install_github_release_tool("praydog/REFramework", dest_dir, ".zip")
            msg = f"Installed REFramework [{label}] ({count} files) to {dest_dir}"

        elif tool_id == "xedit_creation":
            dest_dir = TOOLS_ROOT / "xEdit"
            label, h, count = await _install_github_release_tool("TES5Edit/TES5Edit", dest_dir, "Edit")
            msg = f"Installed xEdit [{label}] ({count} files) to {dest_dir}"

        else:
            raise HTTPException(status_code=400, detail=f"Unknown modding tool_id: '{tool_id}'")

    except HTTPException:
        raise
    except Exception as exc:
        append_audit_log(
            action=f"tools.install.{tool_id}",
            status="error",
            summary=f"Failed to install modding tool '{tool_id}': {exc}",
            details={"tool_id": tool_id, "game_id": req.game_id, "game_dir": str(game_dir)},
        )
        raise HTTPException(status_code=500, detail=f"Failed to install '{tool_id}': {exc}") from exc

    append_audit_log(
        action=f"tools.install.{tool_id}",
        status="ok",
        summary=msg,
        sha256=h,
        details={"tool_id": tool_id, "install_dir": str(dest_dir), "game_id": req.game_id},
    )

    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == req.game_id), None)
    if target and dir_exists:
        scraped_info = inspect_game_modding_info(game_dir, str(target.get("name", "")))
        scraped_info["metadata_scraped"] = True
        target["cached_modding_info"] = scraped_info
        target["metadata_scraped"] = True
        save_managed_games(store)

    return {
        "status": "ok",
        "tool_id": tool_id,
        "install_dir": str(dest_dir),
        "sha256": h,
        "message": msg,
        "game": enrich_game_status(target) if target else None,
    }


@app.post("/api/tools/open-folder")
async def open_tool_folder(req: OpenToolFolderRequest) -> dict[str, Any]:
    """Opens the specified tool or game directory in Windows Explorer."""
    import subprocess

    target = Path(req.path.strip())
    if not target.exists():
        if target.parent.is_dir():
            target = target.parent
        else:
            raise HTTPException(status_code=404, detail=f"Directory does not exist yet: {target}")
    subprocess.Popen(["explorer.exe", str(target.resolve())])
    append_audit_log(
        action="tools.open_folder",
        status="ok",
        summary=f"Opened directory in Explorer: {target.resolve()}",
        sha256=sha256_text(str(target.resolve()).lower()),
        details={"path": str(target.resolve())},
    )
    return {"status": "ok", "path": str(target.resolve())}


_PC_SPECS_MEM_CACHE: dict[str, Any] | None = None


def scrape_pc_specs(force_refresh: bool = False) -> dict[str, Any]:
    """Scrapes comprehensive PC hardware & OS specifications (CPU, GPU, VRAM, RAM, Display, Storage, Tier)."""
    global _PC_SPECS_MEM_CACHE
    if not force_refresh and _PC_SPECS_MEM_CACHE is not None:
        return _PC_SPECS_MEM_CACHE
    if not force_refresh and PC_SPECS_CACHE_PATH.is_file():
        try:
            cached = json.loads(PC_SPECS_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("cpu") and cached.get("gpu"):
                _PC_SPECS_MEM_CACHE = cached
                return cached
        except Exception:
            pass

    import ctypes
    import platform
    import subprocess
    import winreg

    # 1. System Memory (RAM) via GlobalMemoryStatusEx
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    mem_stat = MEMORYSTATUSEX()
    mem_stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_stat))
    total_ram_gb = round(mem_stat.ullTotalPhys / (1024**3), 1)
    avail_ram_gb = round(mem_stat.ullAvailPhys / (1024**3), 1)
    used_ram_gb = round(total_ram_gb - avail_ram_gb, 1)
    ram_load_pct = int(mem_stat.dwMemoryLoad)

    # 2. CPU via Registry + os.cpu_count
    cpu_name = platform.processor() or "Unknown CPU"
    cpu_mhz = 0
    cpu_vendor = "Unknown"
    logical_threads = os.cpu_count() or 1
    physical_cores = max(1, logical_threads // 2)
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            cpu_name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
            cpu_mhz = int(winreg.QueryValueEx(key, "~MHz")[0])
            cpu_vendor = str(winreg.QueryValueEx(key, "VendorIdentifier")[0]).strip()
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor",
        ) as root_cpu:
            logical_threads = winreg.QueryInfoKey(root_cpu)[0] or logical_threads
            physical_cores = max(1, logical_threads // 2)
    except Exception:
        pass

    # 3. Motherboard & BIOS via Registry
    board_vendor = "Unknown"
    board_product = "Unknown"
    bios_version = "Unknown"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\BIOS",
        ) as bkey:
            for reg_val, target_name in [
                ("BaseBoardManufacturer", "vendor"),
                ("BaseBoardProduct", "product"),
                ("BIOSVersion", "bios"),
            ]:
                try:
                    v, _ = winreg.QueryValueEx(bkey, reg_val)
                    if isinstance(v, list):
                        v = " ".join(str(x) for x in v)
                    if target_name == "vendor" and v:
                        board_vendor = str(v).strip()
                    elif target_name == "product" and v:
                        board_product = str(v).strip()
                    elif target_name == "bios" and v:
                        bios_version = str(v).strip()
                except OSError:
                    pass
    except Exception:
        pass

    # 4. Windows OS Details via Registry
    os_product = f"{platform.system()} {platform.release()}"
    os_build = platform.version()
    os_display_ver = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as okey:
            prod, _ = winreg.QueryValueEx(okey, "ProductName")
            build, _ = winreg.QueryValueEx(okey, "CurrentBuildNumber")
            try:
                disp, _ = winreg.QueryValueEx(okey, "DisplayVersion")
                os_display_ver = str(disp)
            except OSError:
                pass
            if int(str(build)) >= 22000 and "Windows 10" in str(prod):
                prod = str(prod).replace("Windows 10", "Windows 11")
            os_product = str(prod)
            os_build = str(build)
    except Exception:
        pass

    # 5. Primary Display Resolution & Refresh Rate via Win32 user32
    screen_w = 1920
    screen_h = 1080
    refresh_hz = 60
    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        screen_w = int(user32.GetSystemMetrics(0)) or 1920
        screen_h = int(user32.GetSystemMetrics(1)) or 1080

        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", ctypes.c_wchar * 32),
                ("dmSpecVersion", ctypes.c_ushort),
                ("dmDriverVersion", ctypes.c_ushort),
                ("dmSize", ctypes.c_ushort),
                ("dmDriverExtra", ctypes.c_ushort),
                ("dmFields", ctypes.c_ulong),
                ("dmPositionX", ctypes.c_long),
                ("dmPositionY", ctypes.c_long),
                ("dmDisplayOrientation", ctypes.c_ulong),
                ("dmDisplayFixedOutput", ctypes.c_ulong),
                ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short),
                ("dmFormName", ctypes.c_wchar * 32),
                ("dmLogPixels", ctypes.c_ushort),
                ("dmBitsPerPel", ctypes.c_ulong),
                ("dmPelsWidth", ctypes.c_ulong),
                ("dmPelsHeight", ctypes.c_ulong),
                ("dmDisplayFlags", ctypes.c_ulong),
                ("dmDisplayFrequency", ctypes.c_ulong),
            ]

        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
            if dm.dmPelsWidth > 0 and dm.dmPelsHeight > 0:
                screen_w = int(dm.dmPelsWidth)
                screen_h = int(dm.dmPelsHeight)
            if dm.dmDisplayFrequency > 0:
                refresh_hz = int(dm.dmDisplayFrequency)
    except Exception:
        pass

    # 6. GPU & VRAM via nvidia-smi (with Registry fallback)
    gpu_name = "Unknown GPU"
    vram_total_mb = 0
    vram_free_mb = 0
    gpu_driver = "Unknown"
    gpu_temp_c: float | None = None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            first_gpu = proc.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_gpu.split(",")]
            if len(parts) >= 4:
                gpu_name = parts[0]
                vram_total_mb = int(float(parts[1]))
                vram_free_mb = int(float(parts[2]))
                gpu_driver = parts[3]
            if len(parts) >= 5:
                try:
                    gpu_temp_c = round(float(parts[4]), 1)
                except ValueError:
                    pass
    except Exception:
        pass

    if gpu_name == "Unknown GPU":
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000",
            ) as gkey:
                gpu_name = str(winreg.QueryValueEx(gkey, "DriverDesc")[0])
                gpu_driver = str(winreg.QueryValueEx(gkey, "DriverVersion")[0])
                try:
                    qw_mem, _ = winreg.QueryValueEx(gkey, "HardwareInformation.qwMemorySize")
                    if isinstance(qw_mem, int):
                        vram_total_mb = int(qw_mem // (1024 * 1024))
                except OSError:
                    pass
        except Exception:
            pass

    vram_total_gb = round(vram_total_mb / 1024.0, 1) if vram_total_mb else 0.0
    vram_free_gb = round(vram_free_mb / 1024.0, 1) if vram_free_mb else 0.0

    # 7. Storage Drives
    drives: list[dict[str, Any]] = []
    for drv_letter in ("C:", "D:", "E:", "F:", "G:"):
        drv_path = Path(drv_letter + "\\")
        if drv_path.is_dir():
            try:
                usage = shutil.disk_usage(drv_path)
                total_g = round(usage.total / (1024**3), 1)
                free_g = round(usage.free / (1024**3), 1)
                used_g = round(usage.used / (1024**3), 1)
                pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
                drives.append(
                    {
                        "drive": drv_letter,
                        "total_gb": total_g,
                        "free_gb": free_g,
                        "used_gb": used_g,
                        "used_percent": pct,
                    }
                )
            except Exception:
                pass

    # 8. Performance Tier Classification for Unreal Engine & Modding
    if vram_total_gb >= 16.0 and physical_cores >= 8 and total_ram_gb >= 24.0:
        tier_name = "Enthusiast / Flagship 4K Ultra Tier"
        tier_badge = "Ultra / Epic (4K 60-120+ FPS)"
        rec_ue_preset = 3  # Epic (3) / Cinematic (4)
        rec_ue_preset_label = "Epic (3) / Cinematic (4) with Hardware Lumen & Virtual Shadow Maps"
        rec_resolution = f"{screen_w}x{screen_h} (Native 4K / High-Refresh)"
    elif vram_total_gb >= 10.0 and physical_cores >= 6 and total_ram_gb >= 16.0:
        tier_name = "High-End 1440p / 4K Upscaled Tier"
        tier_badge = "High / Epic (1440p / DLSS 4K)"
        rec_ue_preset = 3
        rec_ue_preset_label = "High (2) to Epic (3) with TSR / DLSS Quality"
        rec_resolution = "2560x1440 (QHD) or 3840x2160 with DLSS/TSR"
    elif vram_total_gb >= 6.0 and total_ram_gb >= 12.0:
        tier_name = "Mainstream 1080p Gaming Tier"
        tier_badge = "Medium / High (1080p)"
        rec_ue_preset = 2
        rec_ue_preset_label = "Medium (1) to High (2)"
        rec_resolution = "1920x1080 (FHD)"
    else:
        tier_name = "Entry / Integrated Graphics Tier"
        tier_badge = "Low / Medium (720p-1080p)"
        rec_ue_preset = 1
        rec_ue_preset_label = "Low (0) to Medium (1) with Dynamic Resolution"
        rec_resolution = "1920x1080 (66% Resolution Scale)"

    specs = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "hostname": platform.node(),
        "tier": {
            "name": tier_name,
            "badge": tier_badge,
            "recommended_ue_scalability": rec_ue_preset,
            "recommended_ue_preset_label": rec_ue_preset_label,
            "recommended_resolution": rec_resolution,
        },
        "cpu": {
            "name": cpu_name,
            "vendor": cpu_vendor,
            "physical_cores": physical_cores,
            "logical_threads": logical_threads,
            "base_clock_mhz": cpu_mhz,
            "base_clock_ghz": round(cpu_mhz / 1000.0, 2) if cpu_mhz else None,
            "architecture": platform.machine() or "AMD64",
        },
        "gpu": {
            "name": gpu_name,
            "vram_total_mb": vram_total_mb,
            "vram_total_gb": vram_total_gb,
            "vram_free_mb": vram_free_mb,
            "vram_free_gb": vram_free_gb,
            "driver_version": gpu_driver,
            "temperature_c": gpu_temp_c,
            "directx_feature_level": "DirectX 12 Ultimate (SM 6.6 / Hardware Ray Tracing & DLSS)"
            if ("RTX" in gpu_name or "RX 7" in gpu_name or "RX 6" in gpu_name)
            else "DirectX 12 (SM 6.0+)",
        },
        "ram": {
            "total_gb": total_ram_gb,
            "available_gb": avail_ram_gb,
            "used_gb": used_ram_gb,
            "load_percent": ram_load_pct,
        },
        "display": {
            "width": screen_w,
            "height": screen_h,
            "resolution_label": f"{screen_w}x{screen_h}",
            "refresh_rate_hz": refresh_hz,
            "aspect_ratio": "16:9"
            if round(screen_w / max(1, screen_h), 2) == 1.78
            else f"{screen_w}:{screen_h}",
        },
        "os": {
            "product_name": os_product,
            "display_version": os_display_ver,
            "build_number": os_build,
            "motherboard": f"{board_vendor} {board_product}".strip(),
            "bios_version": bios_version,
        },
        "drives": drives,
    }
    _PC_SPECS_MEM_CACHE = specs
    try:
        PC_SPECS_CACHE_PATH.write_text(json.dumps(specs, indent=2), encoding="utf-8")
    except Exception:
        pass
    return specs


_UE_QUALITY_LABELS = {
    0: "Low (0)",
    1: "Medium (1)",
    2: "High (2)",
    3: "Epic (3)",
    4: "Cinematic (4)",
}

# Reference Array mapping PC Hardware Tiers (GPU Generation + VRAM + CPU + RAM) to Recommended Unreal Engine 4/5 Configuration Options
UNREAL_HARDWARE_REFERENCE_ARRAY: list[dict[str, Any]] = [
    {
        "tier_id": "tier_flagship_24gb_4090",
        "tier_name": "Flagship 24GB+ (RTX 4090 / 5090 / 3090 Ti / RX 7900 XTX)",
        "example_hardware": "NVIDIA RTX 4090 24GB | 8-16+ Core CPU (i9 / Ryzen 9) | 32-64GB RAM",
        "min_vram_gb": 20.0,
        "max_vram_gb": 48.0,
        "min_cpu_cores": 8,
        "min_ram_gb": 24.0,
        "target_resolution": "3840x2160",
        "target_res_x": 3840,
        "target_res_y": 2160,
        "max_safe_res_y": 2160,
        "recommended_res_scale": 100,
        "min_safe_res_scale": 75,
        "vram_budget_ceiling_gb": 22.5,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — TSR High / DLAA"},
            "sg.ShadowQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full VSM"},
            "sg.GlobalIlluminationQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Hardware Lumen GI"},
            "sg.ReflectionQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Hardware Lumen Reflections"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full Uncompressed Pool"},
            "sg.EffectsQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full Niagara GPU Particles"},
            "sg.FoliageQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Max Nanite Foliage"},
            "sg.ShadingQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
        },
        "notes": "24GB VRAM + Ada Lovelace SER easily sustains native 4K Epic (3) / Cinematic (4) with Hardware Lumen GI, Virtual Shadow Maps, and 12GB+ texture pools.",
    },
    {
        "tier_id": "tier_high_16gb_4080",
        "tier_name": "High-End 16GB (RTX 4080 / 4070 Ti Super / RX 7900 XT / 6950 XT)",
        "example_hardware": "NVIDIA RTX 4080 16GB | 8+ Core CPU (i7 / Ryzen 7) | 32GB RAM",
        "min_vram_gb": 15.0,
        "max_vram_gb": 19.9,
        "min_cpu_cores": 8,
        "min_ram_gb": 16.0,
        "target_resolution": "3840x2160 (TSR/DLSS 85%) or 2560x1440 Native",
        "target_res_x": 3840,
        "target_res_y": 2160,
        "max_safe_res_y": 2160,
        "recommended_res_scale": 85,
        "min_safe_res_scale": 67,
        "vram_budget_ceiling_gb": 15.0,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadowQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.GlobalIlluminationQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) — Lumen GI"},
            "sg.ReflectionQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) — 8-10GB Pool"},
            "sg.EffectsQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.FoliageQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
        },
        "notes": "16GB VRAM sustains Epic (3) across all scalability groups at 1440p Native or 4K with DLSS/TSR Quality (75-85%).",
    },
    {
        "tier_id": "tier_upper_mid_12gb_4070_3080",
        "tier_name": "Upper-Mid 12GB (RTX 4070 / RTX 3080 12GB / 3080 Ti / RX 7800 XT)",
        "example_hardware": "NVIDIA RTX 4070 / 3080 12GB | 6-8 Core CPU | 16-32GB RAM",
        "min_vram_gb": 11.0,
        "max_vram_gb": 14.9,
        "min_cpu_cores": 6,
        "min_ram_gb": 16.0,
        "target_resolution": "2560x1440",
        "target_res_x": 2560,
        "target_res_y": 1440,
        "max_safe_res_y": 1440,
        "recommended_res_scale": 85,
        "min_safe_res_scale": 67,
        "vram_budget_ceiling_gb": 11.2,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadowQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — Balanced VSM"},
            "sg.GlobalIlluminationQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — Software Lumen"},
            "sg.ReflectionQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) at 1440p / High (2) at 4K"},
            "sg.EffectsQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.FoliageQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.ShadingQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
        },
        "notes": "Optimal for 1440p High (2) / Epic Textures (3). Running native 4K Epic with Lumen + VSM will exceed the 12GB VRAM pool.",
    },
    {
        "tier_id": "tier_mid_8gb_3000_4060",
        "tier_name": "Mainstream 8GB RTX 3000 / 4060 Gen (RTX 3070 / 3060 Ti / 3060 8GB / 4060 / RX 6600 XT)",
        "example_hardware": "NVIDIA 8GB RTX 3000 Series (RTX 3070 / 3060 Ti) | 6+ Core CPU | 16GB RAM",
        "min_vram_gb": 7.5,
        "max_vram_gb": 10.9,
        "min_cpu_cores": 6,
        "min_ram_gb": 16.0,
        "target_resolution": "1920x1080 (or 2560x1440 with DLSS/TSR 67%)",
        "target_res_x": 1920,
        "target_res_y": 1080,
        "max_safe_res_y": 1440,
        "recommended_res_scale": 75,
        "min_safe_res_scale": 60,
        "vram_budget_ceiling_gb": 7.6,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.AntiAliasingQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — TSR / DLSS Quality"},
            "sg.ShadowQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — VSM Pool Saver"},
            "sg.GlobalIlluminationQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — Software Lumen"},
            "sg.ReflectionQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — SSR / Hybrid"},
            "sg.PostProcessQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.TextureQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — 8GB Safe Pool (Epic overflows)"},
            "sg.EffectsQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.FoliageQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.ShadingQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.LandscapeQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
        },
        "notes": "8GB VRAM ceiling requires High (2) Textures and Medium (1) / High (2) Shadows & Reflections; 4K or Epic (3) Textures + VSM causes severe 8GB VRAM exhaustion.",
    },
    {
        "tier_id": "tier_entry_6gb_2060_1660",
        "tier_name": "Entry 6GB GPU (RTX 2060 6GB / GTX 1660 Super / RTX 3050 6GB / RX 5600 XT)",
        "example_hardware": "NVIDIA RTX 2060 / GTX 1660S 6GB | 4-6 Core CPU | 16GB RAM",
        "min_vram_gb": 5.5,
        "max_vram_gb": 7.4,
        "min_cpu_cores": 4,
        "min_ram_gb": 12.0,
        "target_resolution": "1920x1080 (TSR/DLSS 67% Scale)",
        "target_res_x": 1920,
        "target_res_y": 1080,
        "max_safe_res_y": 1080,
        "recommended_res_scale": 67,
        "min_safe_res_scale": 50,
        "vram_budget_ceiling_gb": 5.6,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.AntiAliasingQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — TSR Upscaling"},
            "sg.ShadowQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — CSM Shadows"},
            "sg.GlobalIlluminationQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — Screen-Space GI"},
            "sg.ReflectionQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — SSR"},
            "sg.PostProcessQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.TextureQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — 6GB Pool Cap"},
            "sg.EffectsQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1)"},
            "sg.FoliageQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1)"},
            "sg.ShadingQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.LandscapeQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
        },
        "notes": "6GB VRAM requires 1080p with Medium (1) Scalability and TSR/DLSS 67% render scale to avoid PCIe VRAM spilling.",
    },
    {
        "tier_id": "tier_budget_4gb_1650_igpu",
        "tier_name": "Budget 4GB / Handheld / iGPU (GTX 1650 / RX 6400 / Steam Deck / ROG Ally)",
        "example_hardware": "4GB VRAM or Shared APU Memory | 4-8 Core CPU | 8-16GB RAM",
        "min_vram_gb": 0.0,
        "max_vram_gb": 5.4,
        "min_cpu_cores": 4,
        "min_ram_gb": 8.0,
        "target_resolution": "1280x720 or 1920x1080 (50% Scale)",
        "target_res_x": 1280,
        "target_res_y": 720,
        "max_safe_res_y": 1080,
        "recommended_res_scale": 50,
        "min_safe_res_scale": 50,
        "vram_budget_ceiling_gb": 3.8,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.AntiAliasingQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — TSR Performance"},
            "sg.ShadowQuality": {"rec": 0, "max_safe": 0, "label": "Low (0)"},
            "sg.GlobalIlluminationQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.ReflectionQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.PostProcessQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.TextureQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.EffectsQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.FoliageQuality": {"rec": 0, "max_safe": 0, "label": "Low (0)"},
            "sg.ShadingQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.LandscapeQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
        },
        "notes": "4GB VRAM / APU requires Low (0) Scalability, 720p-1080p at 50% 3D Resolution Scale, and Dynamic Resolution Enabled.",
    },
]


def match_pc_to_unreal_reference_tier(
    pc_specs: dict[str, Any],
    override_tier_id: str = "auto",
) -> dict[str, Any]:
    """Selects the matching hardware profile from UNREAL_HARDWARE_REFERENCE_ARRAY based on scraped PC specs or an explicit override_tier_id."""
    if override_tier_id and override_tier_id.strip().lower() != "auto":
        req_id = override_tier_id.strip().lower()
        for tier in UNREAL_HARDWARE_REFERENCE_ARRAY:
            if tier["tier_id"].lower() == req_id:
                return dict(tier, matched_mode="Simulated Reference Tier Override")

    vram_gb = float(pc_specs.get("gpu", {}).get("vram_total_gb", 8.0))
    for tier in UNREAL_HARDWARE_REFERENCE_ARRAY:
        if vram_gb >= float(tier["min_vram_gb"]):
            return dict(tier, matched_mode="Auto-Matched from Scraped PC Specs")
    return dict(UNREAL_HARDWARE_REFERENCE_ARRAY[-1], matched_mode="Auto-Matched from Scraped PC Specs")


def evaluate_scalability_traffic_light(
    setting_key: str,
    current_lvl: int | None,
    ref_tier: dict[str, Any],
) -> dict[str, Any]:
    """Evaluates a single Unreal scalability setting (0..4) against the reference tier using a Traffic-Light (Green / Amber / Red) system."""
    target_info = ref_tier.get("scalability_targets", {}).get(
        setting_key,
        {"rec": 2, "max_safe": 3, "label": "High (2)"},
    )
    rec_lvl = int(target_info["rec"])
    max_safe = int(target_info["max_safe"])
    rec_label = str(target_info["label"])
    tier_name = str(ref_tier.get("tier_name", "Reference Tier"))

    if current_lvl is None:
        return {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "UNSET / DEFAULT",
            "recommended_value": rec_label,
            "advice": f"Setting not saved in INI yet. Recommended for {tier_name}: {rec_label}.",
        }

    if current_lvl > max_safe:
        return {
            "traffic_light": "red",
            "light_icon": "",
            "light_badge": "OVER-BUDGET",
            "recommended_value": rec_label,
            "advice": (
                f"Current {_UE_QUALITY_LABELS.get(current_lvl, str(current_lvl))} exceeds safe ceiling "
                f"({_UE_QUALITY_LABELS.get(max_safe, str(max_safe))}) for {tier_name} — causes VRAM/frame-time bottleneck. Lower to {rec_label}."
            ),
        }

    if current_lvl == rec_lvl or (current_lvl > rec_lvl and current_lvl <= max_safe):
        return {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": rec_label,
            "advice": f"Matches optimal reference target ({rec_label}) for {tier_name}.",
        }

    diff_below = rec_lvl - current_lvl
    if diff_below == 1:
        return {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "SUB-OPTIMAL",
            "recommended_value": rec_label,
            "advice": f"Set 1 tier below hardware capability ({_UE_QUALITY_LABELS.get(current_lvl)} vs recommended {rec_label} for {tier_name}). Safe to raise.",
        }

    return {
        "traffic_light": "red" if rec_lvl >= 3 and current_lvl == 0 else "amber",
        "light_icon": "",
        "light_badge": "UNDER-UTILIZED",
        "recommended_value": rec_label,
        "advice": f"Set {diff_below} tiers below hardware capability ({_UE_QUALITY_LABELS.get(current_lvl)} vs recommended {rec_label} for {tier_name}).",
    }


def _format_ue_quality(val_str: str | None) -> tuple[str, int | None]:
    if val_str is None or str(val_str).strip() == "":
        return ("Not Set (Engine Default)", None)
    try:
        iv = int(float(str(val_str).strip()))
        return (_UE_QUALITY_LABELS.get(iv, f"Level {iv}"), iv)
    except ValueError:
        return (str(val_str).strip(), None)


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


@app.get("/api/pc-specs")
async def get_pc_specs() -> dict[str, Any]:
    """Returns scraped PC hardware specifications (from cache or fresh scrape) plus the Unreal Hardware Reference Array."""
    global PC_SPECS_RESPONSE_CACHE
    if PC_SPECS_RESPONSE_CACHE is not None:
        return PC_SPECS_RESPONSE_CACHE
    specs = await asyncio.to_thread(scrape_pc_specs, False)
    PC_SPECS_RESPONSE_CACHE = {
        "status": "ok",
        "specs": specs,
        "matched_reference_tier": match_pc_to_unreal_reference_tier(specs, "auto"),
        "unreal_reference_array": UNREAL_HARDWARE_REFERENCE_ARRAY,
    }
    return PC_SPECS_RESPONSE_CACHE


@app.post("/api/pc-specs/scan")
async def rescan_pc_specs() -> dict[str, Any]:
    """Forces a fresh scrape of PC hardware & OS specifications and logs the scan in the Audit Log."""
    global PC_SPECS_RESPONSE_CACHE
    PC_SPECS_RESPONSE_CACHE = None
    specs = await asyncio.to_thread(scrape_pc_specs, True)
    spec_hash = sha256_text(json.dumps(specs, sort_keys=True, default=str))
    summary = (
        f"Scraped PC Specifications: {specs['cpu']['name']} ({specs['cpu']['physical_cores']}C/{specs['cpu']['logical_threads']}T) | "
        f"{specs['gpu']['name']} ({specs['gpu']['vram_total_gb']} GB VRAM) | "
        f"{specs['ram']['total_gb']} GB RAM | {specs['display']['resolution_label']} @ {specs['display']['refresh_rate_hz']}Hz"
    )
    append_audit_log(
        action="pc_specs.scan",
        status="ok",
        summary=summary,
        sha256=spec_hash,
        details={
            "cpu": specs["cpu"]["name"],
            "gpu": specs["gpu"]["name"],
            "vram_gb": specs["gpu"]["vram_total_gb"],
            "ram_gb": specs["ram"]["total_gb"],
            "display": f"{specs['display']['resolution_label']}@{specs['display']['refresh_rate_hz']}Hz",
            "tier": specs["tier"]["name"],
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "sha256": spec_hash,
        "specs": specs,
        "matched_reference_tier": match_pc_to_unreal_reference_tier(specs, "auto"),
        "unreal_reference_array": UNREAL_HARDWARE_REFERENCE_ARRAY,
    }


@app.post("/api/games/unreal-settings")
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


def _cs_escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _generate_il2cpp_plugin_cs(
    mod_id: str,
    mod_name: str,
    mod_version: str,
    assembly_name: str,
    apply_interval_sec: float,
    edits: list[ModEditRule],
) -> str:
    rules_lines: list[str] = []
    hook_lines: list[str] = []
    for idx, r in enumerate(edits):
        if getattr(r, "rule_type", "member_set") == "harmony_patch":
            target_type = _cs_escape(r.component_name or "TargetNamespace.TargetClass")
            target_method = _cs_escape(r.target_method or r.member_path or "TargetMethod")
            hook_kind = "Postfix" if str(getattr(r, "hook_kind", "Prefix")).lower() == "postfix" else "Prefix"
            skip_orig = "true" if getattr(r, "skip_original", False) else "false"
            hook_lines.append(
                f'            ApplyDynamicHarmonyHook(harmony, "{_cs_escape(r.label)}", "{target_type}", "{target_method}", "{hook_kind}", {skip_orig});'
            )
        else:
            cfg_key = f"Rule_{idx + 1}_{make_slug(r.label or r.member_path)[:24]}"
            rules_lines.append(
                f'            _rules.Add(new EditRule("{_cs_escape(r.label)}", "{_cs_escape(r.entity_name)}", '
                f'"{_cs_escape(r.component_name)}", "{_cs_escape(r.member_path)}", '
                f'Config.Bind("Edits", "{_cs_escape(cfg_key)}", "{_cs_escape(r.value)}", '
                f'"{_cs_escape(r.entity_name)} -> {_cs_escape(r.component_name)}.{_cs_escape(r.member_path)}")));'
            )
    rules_block = "\n".join(rules_lines)
    hooks_block = "\n".join(hook_lines)

    return f'''using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using BepInEx;
using BepInEx.Configuration;
using BepInEx.Logging;
using BepInEx.Unity.IL2CPP;
using HarmonyLib;
using Il2CppInterop.Runtime.Injection;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace {assembly_name}
{{
    [BepInPlugin("{_cs_escape(mod_id)}", "{_cs_escape(mod_name)}", "{_cs_escape(mod_version)}")]
    public class Plugin : BasePlugin
    {{
        internal static ManualLogSource ModLog = null!;
        private static ConfigEntry<bool> _enabled = null!;
        private static ConfigEntry<float> _intervalSec = null!;
        private static readonly List<EditRule> _rules = new();

        internal sealed class EditRule
        {{
            public string Label {{ get; }}
            public string EntityName {{ get; }}
            public string ComponentName {{ get; }}
            public string MemberPath {{ get; }}
            public ConfigEntry<string> ValueEntry {{ get; }}

            public EditRule(string label, string entityName, string componentName, string memberPath, ConfigEntry<string> valueEntry)
            {{
                Label = label;
                EntityName = entityName;
                ComponentName = componentName;
                MemberPath = memberPath;
                ValueEntry = valueEntry;
            }}
        }}

        public override void Load()
        {{
            ModLog = Log;
            _enabled = Config.Bind("General", "Enabled", true, "Enable or disable all mod edits.");
            _intervalSec = Config.Bind("General", "ApplyIntervalSeconds", {max(0.5, float(apply_interval_sec)):.1f}f, "How often (in seconds) the mod checks and applies active edits.");

{rules_block}
            var harmony = new Harmony("{_cs_escape(mod_id)}");
{hooks_block}

            ClassInjector.RegisterTypeInIl2Cpp<ModApplierBehaviour>();
            var host = new GameObject("{assembly_name}_Host");
            UnityEngine.Object.DontDestroyOnLoad(host);
            host.hideFlags = HideFlags.HideAndDontSave;
            host.AddComponent<ModApplierBehaviour>();

            SceneManager.sceneLoaded += (UnityEngine.Events.UnityAction<Scene, LoadSceneMode>)((scene, mode) =>
            {{
                ApplyAllRules();
            }});

            ModLog.LogInfo($"[{_cs_escape(mod_name)} v{_cs_escape(mod_version)}] Loaded with {{_rules.Count}} edit rule(s).");
        }}

        private static bool DynamicSkipPrefix() => false;
        private static void DynamicLogPrefix() {{ ModLog?.LogInfo("Harmony Prefix triggered."); }}
        private static void DynamicLogPostfix() {{ ModLog?.LogInfo("Harmony Postfix triggered."); }}

        private static void ApplyDynamicHarmonyHook(Harmony harmony, string label, string typeName, string methodName, string hookKind, bool skipOriginal)
        {{
            try
            {{
                var t = AccessTools.TypeByName(typeName);
                var m = t != null ? AccessTools.Method(t, methodName) : null;
                if (m == null)
                {{
                    ModLog.LogWarning($"Harmony hook '{{label}}' target not found: {{typeName}}.{{methodName}}");
                    return;
                }}
                if (hookKind == "Postfix")
                {{
                    harmony.Patch(m, postfix: new HarmonyMethod(typeof(Plugin), nameof(DynamicLogPostfix)));
                }}
                else
                {{
                    var fn = skipOriginal ? nameof(DynamicSkipPrefix) : nameof(DynamicLogPrefix);
                    harmony.Patch(m, prefix: new HarmonyMethod(typeof(Plugin), fn));
                }}
                ModLog.LogInfo($"Harmony {{hookKind}} patched {{typeName}}.{{methodName}} ({{label}})");
            }}
            catch (Exception ex)
            {{
                ModLog.LogWarning($"Harmony hook '{{label}}' failed: {{ex.Message}}");
            }}
        }}

        internal static void ApplyAllRules()
        {{
            if (!_enabled.Value) return;
            foreach (var rule in _rules)
            {{
                try
                {{
                    ApplyRule(rule);
                }}
                catch (Exception ex)
                {{
                    ModLog.LogWarning($"Rule '{{rule.Label}}' skipped: {{ex.Message}}");
                }}
            }}
        }}

        internal static float IntervalSeconds => Math.Max(0.5f, _intervalSec.Value);

        private static void ApplyRule(EditRule rule)
        {{
            var go = GameObject.Find(rule.EntityName);
            if (go == null) return;

            Component targetComp = null;
            foreach (var c in go.GetComponents<Component>())
            {{
                if (c == null) continue;
                var il2Type = c.GetIl2CppType();
                var fullName = il2Type?.FullName ?? c.GetType().FullName ?? "";
                if (fullName.Equals(rule.ComponentName, StringComparison.OrdinalIgnoreCase) ||
                    fullName.EndsWith("." + rule.ComponentName, StringComparison.OrdinalIgnoreCase))
                {{
                    targetComp = c;
                    break;
                }}
            }}
            if (targetComp == null) return;

            var parts = rule.MemberPath.Split('.', StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 0) return;

            object current = targetComp;
            for (int i = 0; i < parts.Length - 1; i++)
            {{
                var nextObj = GetMemberOrStat(current, parts[i]);
                if (nextObj == null) return;
                current = nextObj;
            }}

            SetMemberValue(current, parts[^1], rule.ValueEntry.Value);
        }}

        private static object GetMemberOrStat(object target, string name)
        {{
            var t = target.GetType();
            const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.IgnoreCase;
            var prop = t.GetProperty(name, flags);
            if (prop != null && prop.CanRead) return prop.GetValue(target);
            var fld = t.GetField(name, flags);
            if (fld != null) return fld.GetValue(target);

            // Support Jade.GameModel.StatsFacade.GetStatObject(Stats.Type) lookup
            var getStat = t.GetMethod("GetStatObject", flags);
            if (getStat != null)
            {{
                var pType = getStat.GetParameters()[0].ParameterType;
                if (pType.IsEnum && Enum.TryParse(pType, name, true, out var enumVal))
                {{
                    return getStat.Invoke(target, new[] {{ enumVal }});
                }}
            }}
            return null;
        }}

        private static void SetMemberValue(object target, string memberName, string rawValue)
        {{
            var t = target.GetType();
            const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.IgnoreCase;
            var prop = t.GetProperty(memberName, flags);
            if (prop != null && prop.CanWrite)
            {{
                var converted = ConvertValue(rawValue, prop.PropertyType);
                prop.SetValue(target, converted);
                return;
            }}
            var fld = t.GetField(memberName, flags);
            if (fld != null)
            {{
                var converted = ConvertValue(rawValue, fld.FieldType);
                fld.SetValue(target, converted);
            }}
        }}

        private static object ConvertValue(string raw, Type targetType)
        {{
            var u = Nullable.GetUnderlyingType(targetType) ?? targetType;
            if (u == typeof(string)) return raw;
            if (u == typeof(bool)) return bool.Parse(raw.Trim());
            if (u == typeof(int)) return int.Parse(raw.Trim(), CultureInfo.InvariantCulture);
            if (u == typeof(float)) return float.Parse(raw.Trim(), CultureInfo.InvariantCulture);
            if (u == typeof(double)) return double.Parse(raw.Trim(), CultureInfo.InvariantCulture);
            if (u == typeof(Vector3))
            {{
                var clean = raw.Trim().Trim('(', ')');
                var xyz = clean.Split(',');
                return new Vector3(
                    float.Parse(xyz[0].Trim(), CultureInfo.InvariantCulture),
                    float.Parse(xyz[1].Trim(), CultureInfo.InvariantCulture),
                    float.Parse(xyz[2].Trim(), CultureInfo.InvariantCulture)
                );
            }}
            if (u.IsEnum) return Enum.Parse(u, raw.Trim(), true);
            return Convert.ChangeType(raw, u, CultureInfo.InvariantCulture);
        }}
    }}

    public class ModApplierBehaviour : MonoBehaviour
    {{
        private float _timer = 0f;

        public ModApplierBehaviour(IntPtr ptr) : base(ptr) {{ }}

        private void Update()
        {{
            _timer += Time.deltaTime;
            if (_timer >= Plugin.IntervalSeconds)
            {{
                _timer = 0f;
                Plugin.ApplyAllRules();
            }}
        }}
    }}
}}
'''


def _generate_il2cpp_csproj(assembly_name: str, bep_core_dir: Path, interop_dir: Path) -> str:
    core_dlls = [
        ("BepInEx.Core", bep_core_dir / "BepInEx.Core.dll"),
        ("BepInEx.Unity.IL2CPP", bep_core_dir / "BepInEx.Unity.IL2CPP.dll"),
        ("BepInEx.Unity.Common", bep_core_dir / "BepInEx.Unity.Common.dll"),
        ("0Harmony", bep_core_dir / "0Harmony.dll"),
        ("Il2CppInterop.Runtime", bep_core_dir / "Il2CppInterop.Runtime.dll"),
        ("Il2CppInterop.Common", bep_core_dir / "Il2CppInterop.Common.dll"),
    ]
    interop_dlls = [
        ("Il2Cppmscorlib", interop_dir / "Il2Cppmscorlib.dll"),
        ("Il2CppSystem", interop_dir / "Il2CppSystem.dll"),
        ("UnityEngine", interop_dir / "UnityEngine.dll"),
        ("UnityEngine.CoreModule", interop_dir / "UnityEngine.CoreModule.dll"),
        ("UnityEngine.SceneManagementModule", interop_dir / "UnityEngine.SceneManagementModule.dll"),
    ]
    refs: list[str] = []
    for name, path in core_dlls + interop_dlls:
        if path.is_file():
            refs.append(f'    <Reference Include="{name}"><HintPath>{path}</HintPath><Private>false</Private></Reference>')
    refs_block = "\n".join(refs)
    return f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net6.0</TargetFramework>
    <AssemblyName>{assembly_name}</AssemblyName>
    <LangVersion>latest</LangVersion>
    <Nullable>disable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="Plugin.cs" />
  </ItemGroup>
  <ItemGroup>
{refs_block}
  </ItemGroup>
</Project>
'''


@app.post("/api/mods/build")
async def build_compatible_mod(req: BuildModRequest) -> dict[str, Any]:
    """Builds a game-compatible mod package (.dll/.lua/.ini + manifest + install instructions + .zip) for the active game."""
    import subprocess

    store = load_managed_games()
    target_game = next((g for g in store.get("games", []) if g.get("id") == req.game_id), None)
    game_dir = Path((req.game_dir or (target_game.get("game_dir") if target_game else DEFAULT_GAME_DIR)).strip())
    game_name = target_game.get("name", game_dir.name) if target_game else game_dir.name
    info = (
        target_game.get("cached_modding_info")
        if (target_game and isinstance(target_game.get("cached_modding_info"), dict))
        else inspect_game_modding_info(game_dir, game_name)
    )

    edits = req.edits or [ModEditRule()]
    mod_slug = make_slug(req.mod_name)
    assembly_name = "".join(w.capitalize() for w in mod_slug.split("_") if w) or "CustomGameMod"
    out_dir = BUILT_MODS_DIR / mod_slug
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    has_ga = game_dir.is_dir() and (game_dir / "GameAssembly.dll").is_file()
    managed_dirs = [d / "Managed" for d in game_dir.glob("*_Data") if (d / "Managed" / "Assembly-CSharp.dll").is_file()] if game_dir.is_dir() else []
    has_mono = (not has_ga) and bool(managed_dirs)
    is_unreal = bool(info.get("is_unreal_engine") or info.get("mcp_category") == "Unreal Engine 4/5")
    bep_core_dir = game_dir / "BepInEx" / "core"
    interop_dir = game_dir / "BepInEx" / "interop"

    built_files: list[str] = []
    compile_stdout = ""
    compile_stderr = ""
    mod_kind = ""
    compatibility_notes = ""
    install_steps: list[str] = []
    zip_path = out_dir / f"{mod_slug}.zip"

    if has_ga:
        mod_kind = f"BepInEx 6 Unity.IL2CPP Plugin ({info.get('unity_version', 'Unity')} | Metadata v{info.get('metadata_version', '?')})"
        if not bep_core_dir.is_dir() or not interop_dir.is_dir():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"To compile an ABI-compatible IL2CPP mod for '{game_name}', BepInEx\\core and BepInEx\\interop "
                    f"must exist in '{game_dir}'. Run Provision & Backup and launch the game once first."
                ),
            )

        src_dir = out_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        plugin_cs = _generate_il2cpp_plugin_cs(
            req.mod_id, req.mod_name, req.mod_version, assembly_name, req.apply_interval_sec, edits
        )
        csproj_xml = _generate_il2cpp_csproj(assembly_name, bep_core_dir, interop_dir)
        (src_dir / "Plugin.cs").write_text(plugin_cs, encoding="utf-8")
        (src_dir / f"{assembly_name}.csproj").write_text(csproj_xml, encoding="utf-8")

        proc = await asyncio.to_thread(
            subprocess.run,
            ["dotnet", "build", str(src_dir / f"{assembly_name}.csproj"), "-c", "Release", "--nologo"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        compile_stdout = proc.stdout or ""
        compile_stderr = proc.stderr or ""
        if proc.returncode != 0:
            append_audit_log(
                action="mods.build",
                status="error",
                summary=f"Failed to compile IL2CPP mod '{req.mod_name}' for {game_name}",
                details={"stdout": compile_stdout, "stderr": compile_stderr},
            )
            raise HTTPException(
                status_code=500,
                detail=f"dotnet build failed:\n{compile_stdout}\n{compile_stderr}",
            )

        built_dll = src_dir / "bin" / "Release" / "net6.0" / f"{assembly_name}.dll"
        if not built_dll.is_file():
            raise HTTPException(status_code=500, detail=f"Compiled DLL not found at {built_dll}")

        pkg_root = out_dir / "package"
        plugins_out = pkg_root / "BepInEx" / "plugins" / assembly_name
        plugins_out.mkdir(parents=True, exist_ok=True)
        dest_dll = plugins_out / f"{assembly_name}.dll"
        shutil.copy2(built_dll, dest_dll)
        built_files.append(str(dest_dll))

        compatibility_notes = (
            f"Compiled directly against {game_name}'s Unity {info.get('unity_version')} Il2CppInterop assemblies "
            f"(assembly-hash: {info.get('assembly_hash', 'N/A')}) and BepInEx {info.get('bepinex_version', '6.x')}."
        )
        install_steps = [
            f"1. Ensure BepInEx 6 (Unity.IL2CPP x64) is installed in '{game_dir}' (winhttp.dll + doorstop_config.ini + BepInEx\\core).",
            f"2. Extract '{zip_path.name}' directly into '{game_dir}' (or click 'Install Built Mod to Game' in this tab).",
            f"3. Confirm the compiled DLL is at: {game_dir / 'BepInEx' / 'plugins' / assembly_name / (assembly_name + '.dll')}",
            f"4. Launch '{info.get('executable', game_name)}'. The mod automatically applies all {len(edits)} edit rule(s) on scene load and every {req.apply_interval_sec:.1f}s.",
            f"5. To customize values later without recompiling, edit '{game_dir / 'BepInEx' / 'config' / (req.mod_id + '.cfg')}' after first launch.",
        ]
    elif has_mono:
        managed_dir = managed_dirs[0]
        mod_kind = f"BepInEx 5 Unity Mono Plugin ({info.get('unity_version', 'Unity')})"
        bep_mono_dll = bep_core_dir / "BepInEx.dll"
        if not bep_mono_dll.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"To compile a Unity Mono plugin for '{game_name}', BepInEx 5 (BepInEx\\core\\BepInEx.dll) must be installed in '{game_dir}'.",
            )
        src_dir = out_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        mono_rules = "\n".join(
            f'            _rules.Add(new System.Tuple<string, string, string, string>("{_cs_escape(r.entity_name)}", "{_cs_escape(r.component_name)}", "{_cs_escape(r.member_path)}", "{_cs_escape(r.value)}"));'
            for r in edits
        )
        mono_cs = f'''using System;
using System.Collections.Generic;
using BepInEx;
using UnityEngine;

namespace {assembly_name}
{{
    [BepInPlugin("{_cs_escape(mod_id)}", "{_cs_escape(mod_name)}", "{_cs_escape(mod_version)}")]
    public class Plugin : BaseUnityPlugin
    {{
        private readonly List<Tuple<string, string, string, string>> _rules = new List<Tuple<string, string, string, string>>();
        private float _timer = 0f;

        private void Awake()
        {{
{mono_rules}
            Logger.LogInfo("[{_cs_escape(mod_name)} v{_cs_escape(mod_version)}] Loaded with " + _rules.Count + " rule(s).");
        }}

        private void Update()
        {{
            _timer += Time.deltaTime;
            if (_timer < {max(0.5, float(apply_interval_sec)):.1f}f) return;
            _timer = 0f;
            foreach (var r in _rules)
            {{
                var go = GameObject.Find(r.Item1);
                if (go == null) continue;
                var comp = go.GetComponent(r.Item2);
                if (comp == null) continue;
                var t = comp.GetType();
                var prop = t.GetProperty(r.Item3);
                if (prop != null && prop.CanWrite) {{ prop.SetValue(comp, Convert.ChangeType(r.Item4, prop.PropertyType), null); continue; }}
                var fld = t.GetField(r.Item3);
                if (fld != null) fld.SetValue(comp, Convert.ChangeType(r.Item4, fld.FieldType));
            }}
        }}
    }}
}}
'''
        mono_refs = "\n".join(
            f'    <Reference Include="{p.stem}"><HintPath>{p}</HintPath><Private>false</Private></Reference>'
            for p in [
                bep_mono_dll,
                bep_core_dir / "0Harmony.dll",
                managed_dir / "UnityEngine.dll",
                managed_dir / "UnityEngine.CoreModule.dll",
                managed_dir / "Assembly-CSharp.dll",
            ]
            if p.is_file()
        )
        mono_csproj = f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net472</TargetFramework>
    <AssemblyName>{assembly_name}</AssemblyName>
    <LangVersion>latest</LangVersion>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup><Compile Include="Plugin.cs" /></ItemGroup>
  <ItemGroup>
{mono_refs}
  </ItemGroup>
</Project>
'''
        (src_dir / "Plugin.cs").write_text(mono_cs, encoding="utf-8")
        (src_dir / f"{assembly_name}.csproj").write_text(mono_csproj, encoding="utf-8")
        proc = await asyncio.to_thread(
            subprocess.run,
            ["dotnet", "build", str(src_dir / f"{assembly_name}.csproj"), "-c", "Release", "--nologo"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        compile_stdout = proc.stdout or ""
        compile_stderr = proc.stderr or ""
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"dotnet build failed:\n{compile_stdout}\n{compile_stderr}")
        built_dll = src_dir / "bin" / "Release" / "net472" / f"{assembly_name}.dll"
        pkg_root = out_dir / "package"
        plugins_out = pkg_root / "BepInEx" / "plugins" / assembly_name
        plugins_out.mkdir(parents=True, exist_ok=True)
        dest_dll = plugins_out / f"{assembly_name}.dll"
        shutil.copy2(built_dll, dest_dll)
        built_files.append(str(dest_dll))
        compatibility_notes = f"Compiled directly against {game_name}'s Unity Mono Managed assemblies and BepInEx 5."
        install_steps = [
            f"1. Ensure BepInEx 5 is installed in '{game_dir}'.",
            f"2. Extract '{zip_path.name}' into '{game_dir}' (or click 'Install Built Mod to Game').",
            f"3. Launch '{info.get('executable', game_name)}'.",
        ]
    elif is_unreal:
        mod_kind = f"Unreal Engine 4/5 RE-UE4SS Lua Mod + INI Override ({info.get('detected_project_name', game_name)})"
        pkg_root = out_dir / "package"
        scripts_dir = pkg_root / "Mods" / assembly_name / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (pkg_root / "Mods" / assembly_name / "enabled.txt").write_text("", encoding="utf-8")

        lua_lines = [
            f'-- {req.mod_name} v{req.mod_version} by {req.author}',
            f'print("[{req.mod_name}] Loaded UE4SS Lua Mod\\n")',
        ]
        ini_lines = [f"; {req.mod_name} v{req.mod_version} Unreal INI Overrides", "[/Script/Engine.GameUserSettings]"]
        for r in edits:
            if r.member_path.startswith("sg.") or "Resolution" in r.member_path or "FrameRate" in r.member_path or "VSync" in r.member_path:
                ini_lines.append(f"{r.member_path}={r.value}")
            else:
                lua_lines.append(f'-- Rule: {r.label} ({r.entity_name}.{r.member_path} = {r.value})')
                lua_lines.append(
                    f'RegisterHook("/Script/Engine.PlayerController:ClientRestart", function(self)\n'
                    f'    print("[{req.mod_name}] Applying {r.label}\\n")\n'
                    f'end)'
                )
        (scripts_dir / "main.lua").write_text("\n".join(lua_lines), encoding="utf-8")
        (pkg_root / "GameUserSettings_Override.ini").write_text("\n".join(ini_lines), encoding="utf-8")
        built_files.extend([str(scripts_dir / "main.lua"), str(pkg_root / "GameUserSettings_Override.ini")])
        compatibility_notes = f"Compatible with RE-UE4SS (v3.0+) and Unreal Engine 4/5 INI configuration for {game_name}."
        install_steps = [
            f"1. Install RE-UE4SS into the game's Win64 binaries folder (next to the Shipping .exe).",
            f"2. Copy 'Mods\\{assembly_name}' from '{zip_path.name}' into your game's 'Win64\\Mods\\' folder.",
            f"3. Ensure 'Mods\\{assembly_name}\\enabled.txt' is present (already included in the package).",
            f"4. Merge any INI overrides from 'GameUserSettings_Override.ini' into your %LOCALAPPDATA% Unreal Saved\\Config\\Windows\\GameUserSettings.ini.",
        ]
    else:
        mod_kind = f"Data-Driven / Config Mod Package ({info.get('mcp_category', 'PC')})"
        pkg_root = out_dir / "package"
        pkg_root.mkdir(parents=True, exist_ok=True)
        patch_payload = {
            "mod_id": req.mod_id,
            "mod_name": req.mod_name,
            "version": req.mod_version,
            "author": req.author,
            "target_game": game_name,
            "edits": [r.model_dump() for r in edits],
        }
        patch_file = pkg_root / f"{mod_slug}_patch.json"
        patch_file.write_text(json.dumps(patch_payload, indent=2), encoding="utf-8")
        built_files.append(str(patch_file))
        compatibility_notes = f"Formatted for {game_name} ({info.get('mcp_category', 'PC')})."
        install_steps = [
            f"1. Extract '{zip_path.name}' into '{game_dir}'.",
            f"2. Apply '{patch_file.name}' using the game's native mod loader or data override directory.",
        ]

    # Write manifest.json and INSTALL_INSTRUCTIONS.txt into package + zip
    manifest_data = {
        "mod_id": req.mod_id,
        "mod_name": req.mod_name,
        "mod_slug": mod_slug,
        "assembly_name": assembly_name,
        "version": req.mod_version,
        "author": req.author,
        "target_game_id": req.game_id,
        "target_game_name": game_name,
        "mod_kind": mod_kind,
        "compatibility": compatibility_notes,
        "edits": [r.model_dump() for r in edits],
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    pkg_root = out_dir / "package"
    (pkg_root / "mod_manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    instructions_text = (
        f"=== {req.mod_name} (v{req.mod_version}) ===\n"
        f"Target Game: {game_name}\n"
        f"Mod Type: {mod_kind}\n"
        f"Compatibility: {compatibility_notes}\n\n"
        f"INSTALLATION INSTRUCTIONS:\n"
        + "\n".join(install_steps)
        + "\n"
    )
    (pkg_root / "INSTALL_INSTRUCTIONS.txt").write_text(instructions_text, encoding="utf-8")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in pkg_root.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(pkg_root))

    zip_hash = sha256_file(zip_path)
    summary = f"Built compatible mod '{req.mod_name}' ({mod_kind}) -> {zip_path.name} ({len(edits)} edit rule(s))"
    append_audit_log(
        action="mods.build",
        status="ok",
        summary=summary,
        sha256=zip_hash,
        details={"mod_slug": mod_slug, "zip_path": str(zip_path), "game_id": req.game_id, "edits_count": len(edits)},
    )
    return {
        "status": "ok",
        "summary": summary,
        "mod_slug": mod_slug,
        "assembly_name": assembly_name,
        "mod_kind": mod_kind,
        "compatibility": compatibility_notes,
        "output_dir": str(out_dir),
        "package_dir": str(pkg_root),
        "zip_path": str(zip_path),
        "zip_sha256": zip_hash,
        "built_files": built_files,
        "install_steps": install_steps,
        "instructions_text": instructions_text,
        "compile_output": compile_stdout.strip(),
    }


@app.post("/api/mods/install")
async def install_built_mod_to_game(req: InstallBuiltModRequest) -> dict[str, Any]:
    """Installs a previously built mod package directly into the target game folder."""
    pkg_root = BUILT_MODS_DIR / req.mod_slug.strip() / "package"
    if not pkg_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Built mod package '{req.mod_slug}' not found.")

    game_dir = Path(req.game_dir.strip())
    if not game_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Target game directory does not exist: {game_dir}")

    installed_files: list[str] = []
    for src in pkg_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(pkg_root)
        if rel.name in ("mod_manifest.json", "INSTALL_INSTRUCTIONS.txt"):
            continue
        dest = game_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        installed_files.append(str(dest))

    mod_hash = sha256_text(json.dumps(installed_files, sort_keys=True))
    summary = f"Installed built mod '{req.mod_slug}' ({len(installed_files)} file(s)) into {game_dir}"
    append_audit_log(
        action="mods.install",
        status="ok",
        summary=summary,
        sha256=mod_hash,
        details={"mod_slug": req.mod_slug, "game_id": req.game_id, "installed_files": installed_files},
    )
    return {
        "status": "ok",
        "summary": summary,
        "installed_files": installed_files,
        "sha256": mod_hash,
    }


APP_UI_CONFIG_PATH = get_app_data_root() / "app_ui_config.json"
BRANDING_DIR = WORKSPACE_DIR / "branding"
BRANDING_BANNERS_DIR = BRANDING_DIR / "banners"
BRANDING_FAVICONS_DIR = BRANDING_DIR / "favicons"
BRANDING_BANNERS_DIR.mkdir(parents=True, exist_ok=True)
BRANDING_FAVICONS_DIR.mkdir(parents=True, exist_ok=True)


def default_app_ui_config() -> dict[str, Any]:
    return {
        "app_name": "Unity IL2CPP Modding Manager & Live MCP Console",
        "app_subtitle": "FastAPI + Persistent MCP Stdio Bridge (bepinex-mcp)",
        "accent_color": "#3b82f6",
        "compact_mode": False,
        "startup_tab": "manager",
        "banner_image_url": "",
        "banner_height_px": 110,
        "favicon_url": "",
        "tab_labels": {
            "manager": "Game Manager",
            "game": "Active Game",
            "modbuilder": "Build a Mod",
            "specs": "PC Specs",
            "logs": "Audit Log",
        },
        "visibility": {
            "header_subtitle": True,
            "header_quick_load": True,
            "header_active_badge": True,
            "header_server_badge": True,
            "tab_manager": True,
            "tab_game": True,
            "tab_modbuilder": True,
            "tab_specs": True,
            "tab_logs": True,
            "sec_pipeline_status": True,
            "sec_provision_restore": True,
            "sec_live_mcp": True,
        },
        "in_progress": {
            "manager": False,
            "game": False,
            "modbuilder": False,
            "specs": False,
            "logs": False,
        },
    }


@app.get("/api/app-ui-config")
async def get_app_ui_config() -> dict[str, Any]:
    """Returns persisted UI customization settings or defaults."""
    cfg = default_app_ui_config()
    if APP_UI_CONFIG_PATH.is_file():
        try:
            saved = json.loads(APP_UI_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for k in ("app_name", "app_subtitle", "accent_color", "compact_mode", "startup_tab", "banner_image_url", "banner_height_px", "favicon_url"):
                    if k in saved:
                        cfg[k] = saved[k]
                if isinstance(saved.get("tab_labels"), dict):
                    cfg["tab_labels"].update(saved["tab_labels"])
                if isinstance(saved.get("visibility"), dict):
                    cfg["visibility"].update(saved["visibility"])
                if isinstance(saved.get("in_progress"), dict):
                    cfg["in_progress"].update(saved["in_progress"])
        except Exception:
            pass
    if not cfg.get("banner_image_url") and list(BRANDING_BANNERS_DIR.glob("banner.*")):
        cfg["banner_image_url"] = "/api/app-ui-config/asset/banner"
    if not cfg.get("favicon_url") and list(BRANDING_FAVICONS_DIR.glob("favicon.*")):
        cfg["favicon_url"] = "/api/app-ui-config/asset/favicon"
    return {"status": "ok", "config": cfg, "config_path": str(APP_UI_CONFIG_PATH)}


@app.post("/api/app-ui-config")
async def save_app_ui_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Saves UI customization settings (app name, visibility toggles, theme) to disk."""
    cfg = default_app_ui_config()
    incoming = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if isinstance(incoming, dict):
        for k in ("app_name", "app_subtitle", "accent_color", "compact_mode", "startup_tab"):
            if k in incoming:
                cfg[k] = incoming[k]
        if isinstance(incoming.get("tab_labels"), dict):
            cfg["tab_labels"].update(incoming["tab_labels"])
        if isinstance(incoming.get("visibility"), dict):
            cfg["visibility"].update(incoming["visibility"])
        if isinstance(incoming.get("in_progress"), dict):
            cfg["in_progress"].update(incoming["in_progress"])

    APP_UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_UI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    cfg_hash = sha256_text(json.dumps(cfg, sort_keys=True))
    append_audit_log(
        action="app_ui.save",
        status="ok",
        summary=f"Saved App Editor settings ('{cfg.get('app_name')}')",
        sha256=cfg_hash,
        details={"app_name": cfg.get("app_name"), "startup_tab": cfg.get("startup_tab")},
    )
    return {"status": "ok", "config": cfg, "config_path": str(APP_UI_CONFIG_PATH), "sha256": cfg_hash}


class UploadAppAssetRequest(BaseModel):
    kind: str
    filename: str = Field(default="asset.png")
    data_url: str


@app.post("/api/app-ui-config/upload-asset")
async def upload_app_asset(req: UploadAppAssetRequest) -> dict[str, Any]:
    """Saves an uploaded responsive banner image into branding/banners or favicon into branding/favicons."""
    import base64

    kind = req.kind.strip().lower()
    if kind not in ("banner", "favicon"):
        raise HTTPException(status_code=400, detail="Asset kind must be 'banner' or 'favicon'.")
    raw_url = req.data_url.strip()
    if not raw_url.startswith("data:") or "," not in raw_url:
        raise HTTPException(status_code=400, detail="Expected base64 data URL.")
    header, b64_data = raw_url.split(",", 1)
    if len(b64_data) > 7_000_000:
        raise HTTPException(status_code=400, detail="Asset exceeds 5 MB limit.")

    ext = ".png"
    if "image/jpeg" in header or "image/jpg" in header:
        ext = ".jpg"
    elif "image/webp" in header:
        ext = ".webp"
    elif "image/svg+xml" in header:
        ext = ".svg"
    elif "image/x-icon" in header or "vnd.microsoft.icon" in header or req.filename.lower().endswith(".ico"):
        ext = ".ico"

    target_dir = BRANDING_BANNERS_DIR if kind == "banner" else BRANDING_FAVICONS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for old in target_dir.glob(f"{kind}.*"):
        old.unlink(missing_ok=True)
    dest = target_dir / f"{kind}{ext}"
    dest.write_bytes(base64.b64decode(b64_data))
    url = f"/api/app-ui-config/asset/{kind}?v={int(datetime.now().timestamp())}"

    # Persist immediately into app_ui_config.json
    cfg = (await get_app_ui_config())["config"]
    if kind == "banner":
        cfg["banner_image_url"] = url
    else:
        cfg["favicon_url"] = url
    APP_UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_UI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    append_audit_log(action="app_ui.upload_asset", status="ok", summary=f"Uploaded custom {kind} ({dest})")
    return {"status": "ok", "kind": kind, "url": url, "path": str(dest), "config": cfg}


@app.get("/api/app-ui-config/asset/{kind}")
async def serve_app_asset(kind: str):
    """Serves the uploaded custom banner from branding/banners or favicon from branding/favicons."""
    if kind not in ("banner", "favicon"):
        return Response(status_code=404)
    target_dir = BRANDING_BANNERS_DIR if kind == "banner" else BRANDING_FAVICONS_DIR
    matches = list(target_dir.glob(f"{kind}.*"))
    if not matches:
        return Response(status_code=404)
    asset = matches[0]
    ext = asset.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")
    return FileResponse(asset, media_type=mime, headers={"Cache-Control": "no-cache"})


class RemoveAppAssetRequest(BaseModel):
    kind: str


@app.post("/api/app-ui-config/remove-asset")
async def remove_app_asset(req: RemoveAppAssetRequest) -> dict[str, Any]:
    """Deletes the uploaded banner or favicon from branding/ and clears it in app_ui_config.json."""
    kind = req.kind.strip().lower()
    if kind not in ("banner", "favicon"):
        raise HTTPException(status_code=400, detail="Asset kind must be 'banner' or 'favicon'.")

    target_dir = BRANDING_BANNERS_DIR if kind == "banner" else BRANDING_FAVICONS_DIR
    removed_files: list[str] = []
    if target_dir.is_dir():
        for old in target_dir.glob(f"{kind}.*"):
            old.unlink(missing_ok=True)
            removed_files.append(str(old))

    cfg = (await get_app_ui_config())["config"]
    if kind == "banner":
        cfg["banner_image_url"] = ""
    else:
        cfg["favicon_url"] = ""
    APP_UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_UI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    summary = f"Removed custom {kind}" + (f" ({len(removed_files)} file(s))" if removed_files else "")
    append_audit_log(action="app_ui.remove_asset", status="ok", summary=summary)
    return {"status": "ok", "kind": kind, "removed_files": removed_files, "config": cfg}


@app.post("/api/app-ui-config/reset")
async def reset_app_ui_config() -> dict[str, Any]:
    """Resets UI customization settings back to defaults."""
    if APP_UI_CONFIG_PATH.is_file():
        APP_UI_CONFIG_PATH.unlink()
    cfg = default_app_ui_config()
    append_audit_log(
        action="app_ui.reset",
        status="ok",
        summary="Reset App Editor settings to defaults",
    )
    return {"status": "ok", "config": cfg, "config_path": str(APP_UI_CONFIG_PATH)}


TOOL_VERSIONS_PATH = get_app_data_root() / "tool_versions.json"


def _load_workspace_module(module_name: str, filename: str):
    import importlib.util

    mod_path = WORKSPACE_DIR / filename
    if not mod_path.is_file():
        raise HTTPException(status_code=404, detail=f"Required module not found: {mod_path}")
    spec = importlib.util.spec_from_file_location(module_name, str(mod_path))
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail=f"Failed to load module spec: {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256_file_local(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@app.get("/api/app/backups")
async def list_app_backups() -> dict[str, Any]:
    """Lists all incremental and baseline app backups."""
    bm = _load_workspace_module("backup_manager", "backup_manager.py")
    idx = bm.load_index()
    versions = list(reversed(idx.get("versions", [])))
    for v in versions:
        summary_path = bm.BACKUPS_DIR / v["folder"] / "SUMMARY.md"
        if summary_path.is_file():
            v["summary_preview"] = summary_path.read_text(encoding="utf-8")[:1500]
    return {
        "status": "ok",
        "backups_dir": str(bm.BACKUPS_DIR),
        "versions": versions,
        "snapshots": _list_app_snapshots(),
    }


class SaveAppBackupRequest(BaseModel):
    reason: str = Field(default="Manual save from App Management")
    force_baseline: bool = Field(default=False)


@app.post("/api/app/backup/save")
async def save_app_backup(req: SaveAppBackupRequest) -> dict[str, Any]:
    """Saves an incremental (or baseline) snapshot of the app."""
    bm = _load_workspace_module("backup_manager", "backup_manager.py")
    manifest = bm.create_backup(reason=req.reason.strip() or "Manual save", force_baseline=req.force_baseline)
    if manifest is None:
        return {
            "status": "no_changes",
            "summary": "No file changes detected since the last saved backup.",
            "manifest": None,
        }
    summary = (
        f"Saved app backup {manifest['version_id']} ({manifest['kind']}): "
        f"+{manifest['stats']['added']} ~{manifest['stats']['modified']} -{manifest['stats']['deleted']}"
    )
    append_audit_log(
        action="app.backup.save",
        status="ok",
        summary=summary,
        details={"version_id": manifest["version_id"], "folder": manifest["folder"]},
    )
    return {"status": "ok", "summary": summary, "manifest": manifest}


class RestoreAppBackupRequest(BaseModel):
    version_id: str


@app.post("/api/app/backup/restore")
async def restore_app_backup(req: RestoreAppBackupRequest) -> dict[str, Any]:
    """Restores the app files to a selected saved backup version."""
    bm = _load_workspace_module("backup_manager", "backup_manager.py")
    bm.restore_version(req.version_id.strip(), bm.PROJECT_ROOT)
    summary = f"Restored app to backup version '{req.version_id}'"
    append_audit_log(
        action="app.backup.restore",
        status="ok",
        summary=summary,
        details={"version_id": req.version_id},
    )
    return {"status": "ok", "summary": summary, "version_id": req.version_id}


@app.post("/api/app/export")
async def export_portable_app() -> dict[str, Any]:
    """Packages the app into a portable Windows .zip distribution ready for download."""
    pw = _load_workspace_module("package_windows", "package_windows.py")
    result = pw.build_package()
    summary = f"Packaged portable Windows app ({result['zip_size_mb']} MB, {result['file_count']} files)"
    append_audit_log(
        action="app.export",
        status="ok",
        summary=summary,
        sha256=result.get("zip_sha256"),
        details=result,
    )
    return {
        "status": "ok",
        "summary": summary,
        "download_url": "/api/app/export/download",
        **result,
    }


@app.get("/api/app/export/download")
async def download_portable_app():
    """Streams the packaged portable Windows .zip archive as a browser download."""
    zip_path = WORKSPACE_DIR / "dist" / "ModToolPoC-Win64.zip"
    if not zip_path.is_file():
        pw = _load_workspace_module("package_windows", "package_windows.py")
        pw.build_package()
    if not zip_path.is_file():
        raise HTTPException(status_code=404, detail="Portable app archive not found.")
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename="ModToolPoC-Win64.zip",
    )


def _load_tool_versions() -> dict[str, Any]:
    if TOOL_VERSIONS_PATH.is_file():
        try:
            return json.loads(TOOL_VERSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_tool_versions(data: dict[str, Any]) -> None:
    TOOL_VERSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOOL_VERSIONS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


@app.get("/api/app/tools/status")
async def get_modding_tools_status() -> dict[str, Any]:
    """Returns local versions and SHA-256 checksums for deployed modding tools."""
    saved = _load_tool_versions()
    bep_zip = Path(DEFAULT_BEPINEX_ZIP)
    bep_sha = _sha256_file_local(bep_zip) if bep_zip.is_file() else None

    plugin_dll = Path(DEFAULT_PLUGIN_BUILD_DIR) / "BepInExMCP.IL2CPP.dll"
    plugin_sha = _sha256_file_local(plugin_dll) if plugin_dll.is_file() else None

    ue4ss_dir = get_app_data_root() / "tools" / "ue4ss"
    melon_dir = get_app_data_root() / "tools" / "melonloader"

    return {
        "status": "ok",
        "tools": [
            {
                "id": "bepinex6_il2cpp",
                "name": "BepInEx 6 Unity.IL2CPP (x64)",
                "present": bep_zip.is_file(),
                "version": saved.get("bepinex6_il2cpp", {}).get("version", "6.0.0-be.793" if bep_zip.is_file() else "Not Cached"),
                "path": str(bep_zip),
                "sha256": bep_sha,
                "expected_sha256": saved.get("bepinex6_il2cpp", {}).get("expected_sha256", DEFAULT_BEPINEX_SHA256),
                "checksum_match": (bep_sha == saved.get("bepinex6_il2cpp", {}).get("expected_sha256", DEFAULT_BEPINEX_SHA256)) if bep_sha else False,
            },
            {
                "id": "bepinex_mcp_plugin",
                "name": "BepInExMCP.IL2CPP Bridge Plugin",
                "present": plugin_dll.is_file(),
                "version": saved.get("bepinex_mcp_plugin", {}).get("version", "Local Release Build" if plugin_dll.is_file() else "Not Built"),
                "path": str(plugin_dll),
                "sha256": plugin_sha,
                "checksum_match": plugin_dll.is_file(),
            },
            {
                "id": "ue4ss",
                "name": "RE-UE4SS (Unreal Engine 4/5)",
                "present": ue4ss_dir.is_dir() and any(ue4ss_dir.iterdir()),
                "version": saved.get("ue4ss", {}).get("version", "Not Downloaded"),
                "path": str(ue4ss_dir),
                "sha256": saved.get("ue4ss", {}).get("sha256"),
                "checksum_match": bool(saved.get("ue4ss", {}).get("sha256")),
            },
            {
                "id": "melonloader",
                "name": "MelonLoader (Unity Mono/IL2CPP)",
                "present": melon_dir.is_dir() and any(melon_dir.iterdir()),
                "version": saved.get("melonloader", {}).get("version", "Not Downloaded"),
                "path": str(melon_dir),
                "sha256": saved.get("melonloader", {}).get("sha256"),
                "checksum_match": bool(saved.get("melonloader", {}).get("sha256")),
            },
        ],
    }


class UpgradeModdingToolRequest(BaseModel):
    tool_id: str
    require_https: bool = Field(default=True)
    require_checksum: bool = Field(default=True)
    expected_sha256: str = Field(default="")
    custom_url: str = Field(default="")
    keep_backup: bool = Field(default=True)


@app.post("/api/app/tools/upgrade")
async def upgrade_modding_tool(req: UpgradeModdingToolRequest) -> dict[str, Any]:
    """Upgrades a modding deployment tool with HTTPS, SHA-256 checksum, and backup security options."""
    import subprocess
    import urllib.request

    tool_id = req.tool_id.strip()
    saved = _load_tool_versions()
    expected = req.expected_sha256.strip().lower()

    if tool_id == "bepinex_mcp_plugin":
        csproj = WORKSPACE_DIR / "bepinex-mcp" / "plugins" / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.csproj"
        if not csproj.is_file():
            raise HTTPException(status_code=404, detail=f"Plugin project not found: {csproj}")
        plugin_dll = Path(DEFAULT_PLUGIN_BUILD_DIR) / "BepInExMCP.IL2CPP.dll"
        if req.keep_backup and plugin_dll.is_file():
            bak = plugin_dll.with_suffix(f".dll.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(plugin_dll, bak)
        proc = subprocess.run(
            ["dotnet", "build", str(csproj), "-c", "Release", "--nologo"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0 or not plugin_dll.is_file():
            raise HTTPException(status_code=500, detail=f"dotnet build failed:\n{proc.stdout}\n{proc.stderr}")
        new_sha = _sha256_file_local(plugin_dll)
        if req.require_checksum and expected and new_sha != expected:
            raise HTTPException(status_code=400, detail=f"Checksum mismatch: expected {expected}, got {new_sha}")
        saved["bepinex_mcp_plugin"] = {
            "version": f"Release ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
            "sha256": new_sha,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_tool_versions(saved)
        summary = f"Rebuilt BepInExMCP.IL2CPP Release plugin (SHA-256: {new_sha[:16]}...)"
        append_audit_log(action="app.tools.upgrade", status="ok", summary=summary, sha256=new_sha, details={"tool_id": tool_id})
        return {"status": "ok", "summary": summary, "sha256": new_sha, "build_output": proc.stdout[-1500:]}

    repo_map = {
        "bepinex6_il2cpp": ("BepInEx/BepInEx", "BepInEx-Unity.IL2CPP-win-x64"),
        "ue4ss": ("UE4SS-RE/RE-UE4SS", "UE4SS"),
        "melonloader": ("LavaGang/MelonLoader", "MelonLoader.x64"),
    }
    if tool_id not in repo_map:
        raise HTTPException(status_code=400, detail=f"Unknown tool_id: {tool_id}")

    repo, asset_prefix = repo_map[tool_id]
    download_url = req.custom_url.strip()
    release_tag = "custom"
    github_digest = ""

    if not download_url:
        api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        gh_req = urllib.request.Request(api_url, headers={"User-Agent": "ModToolPoC/1.0"})
        try:
            with urllib.request.urlopen(gh_req, timeout=20) as resp:
                rel_data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to query GitHub release for {repo}: {exc}")
        release_tag = rel_data.get("tag_name") or "latest"
        for asset in rel_data.get("assets", []):
            name = asset.get("name", "")
            if asset_prefix.lower() in name.lower() and name.lower().endswith(".zip"):
                download_url = asset.get("browser_download_url", "")
                digest = asset.get("digest") or ""
                if digest.startswith("sha256:"):
                    github_digest = digest.split(":", 1)[1].strip().lower()
                break
        if not download_url:
            raise HTTPException(status_code=404, detail=f"No matching .zip asset ({asset_prefix}) found in {repo} {release_tag}.")

    if req.require_https and not download_url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail=f"Blocked insecure non-HTTPS download URL: {download_url}")

    effective_expected = expected or github_digest
    if req.require_checksum and not effective_expected:
        raise HTTPException(
            status_code=400,
            detail="Checksum verification is required, but no Expected SHA-256 was provided and the GitHub release asset did not publish a SHA-256 digest.",
        )

    dl_dir = get_app_data_root() / "tools" / tool_id
    dl_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = dl_dir / f"{tool_id}_{release_tag}.zip.tmp"
    dl_req = urllib.request.Request(download_url, headers={"User-Agent": "ModToolPoC/1.0"})
    try:
        with urllib.request.urlopen(dl_req, timeout=60) as resp, tmp_zip.open("wb") as out_f:
            shutil.copyfileobj(resp, out_f)
    except Exception as exc:
        if tmp_zip.exists():
            tmp_zip.unlink()
        raise HTTPException(status_code=502, detail=f"Download failed: {exc}")

    actual_sha = _sha256_file_local(tmp_zip)
    if req.require_checksum and effective_expected and actual_sha != effective_expected:
        tmp_zip.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Security Check Failed: SHA-256 mismatch! Expected {effective_expected}, got {actual_sha}. Download discarded.",
        )

    if tool_id == "bepinex6_il2cpp":
        assets_dir = WORKSPACE_DIR / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        target_zip = assets_dir / "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.793+dbc82f0.zip"
        if req.keep_backup and target_zip.is_file():
            shutil.copy2(target_zip, target_zip.with_suffix(f".zip.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
        shutil.move(str(tmp_zip), str(target_zip))
        final_path = target_zip
    else:
        final_path = dl_dir / f"{tool_id}_{release_tag}.zip"
        if req.keep_backup and final_path.is_file():
            shutil.copy2(final_path, final_path.with_suffix(f".zip.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
        shutil.move(str(tmp_zip), str(final_path))
        with zipfile.ZipFile(final_path, "r") as zf:
            zf.extractall(dl_dir / "extracted")

    saved[tool_id] = {
        "version": release_tag,
        "sha256": actual_sha,
        "expected_sha256": effective_expected or actual_sha,
        "source_url": download_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_tool_versions(saved)
    summary = f"Upgraded {tool_id} to {release_tag} (SHA-256 verified: {actual_sha[:16]}...)"
    append_audit_log(action="app.tools.upgrade", status="ok", summary=summary, sha256=actual_sha, details=saved[tool_id])
    return {"status": "ok", "summary": summary, "version": release_tag, "sha256": actual_sha, "path": str(final_path)}


@app.get("/api/app/version")
async def get_app_version() -> dict[str, Any]:
    """Returns current SemVer metadata from version.json."""
    vm = _load_workspace_module("version_manager", "version_manager.py")
    return {"status": "ok", **vm.load_version_data()}


class BumpVersionRequest(BaseModel):
    bump_type: str = Field(default="patch")
    note: str = Field(default="Manual version bump from App Management")


@app.post("/api/app/version/bump")
async def bump_app_version(req: BumpVersionRequest) -> dict[str, Any]:
    """Advances the app version (patch, minor, major, or launch -> 1.0.0)."""
    vm = _load_workspace_module("version_manager", "version_manager.py")
    try:
        res = vm.bump_version(req.bump_type, req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    summary = f"Advanced app version to v{res['version']} ({res['channel']})"
    append_audit_log(action="app.version.bump", status="ok", summary=summary, details=res)
    return {"status": "ok", "summary": summary, **res}


@app.get("/api/app/diagnostics")
async def get_app_diagnostics() -> dict[str, Any]:
    """Returns runtime health and storage diagnostics for App Management."""
    import subprocess

    dotnet_ver = "Not installed"
    try:
        r = subprocess.run(["dotnet", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            dotnet_ver = r.stdout.strip()
    except Exception:
        pass

    ver_info = {"version": "0.1.0", "channel": "pre-launch", "launched": False}
    try:
        vm = _load_workspace_module("version_manager", "version_manager.py")
        vdata = vm.load_version_data()
        ver_info = {
            "version": vdata.get("version", "0.1.0"),
            "channel": vdata.get("channel", "pre-launch"),
            "launched": bool(vdata.get("launched", False)),
        }
    except Exception:
        pass

    built_mods_count = len(list(BUILT_MODS_DIR.glob("*"))) if BUILT_MODS_DIR.is_dir() else 0
    return {
        "status": "ok",
        "workspace_dir": str(WORKSPACE_DIR),
        "app_data_dir": str(get_app_data_root()),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "dotnet_version": dotnet_ver,
        "built_mods_count": built_mods_count,
        "pid": os.getpid(),
        "app_version": ver_info["version"],
        "app_channel": ver_info["channel"],
        "app_launched": ver_info["launched"],
    }


@app.post("/api/app/maintenance/clear-built-mods")
async def clear_built_mods_cache() -> dict[str, Any]:
    """Clears temporary built mod artifacts in %LOCALAPPDATA%\\ModToolPoC\\built_mods."""
    removed = 0
    if BUILT_MODS_DIR.is_dir():
        for item in list(BUILT_MODS_DIR.iterdir()):
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            removed += 1
    summary = f"Cleared {removed} item(s) from built mods cache."
    append_audit_log(action="app.maintenance.clear_built_mods", status="ok", summary=summary)
    return {"status": "ok", "summary": summary, "removed": removed}


@app.post("/api/app/maintenance/flush-all")
async def flush_all_app_caches() -> dict[str, Any]:
    """Flushes all in-memory and disk caches (games registry, PC specs, Steam assets, game icons, built mods, audit cache)."""
    global PC_SPECS_RESPONSE_CACHE, _PC_SPECS_MEM_CACHE, AUDIT_LOG_CACHE

    invalidate_games_cache()
    PC_SPECS_RESPONSE_CACHE = None
    _PC_SPECS_MEM_CACHE = None
    AUDIT_LOG_CACHE = None
    STEAM_ASSETS_CACHE.clear()

    removed_items: list[str] = []
    for fpath in (PC_SPECS_CACHE_PATH, STEAM_ASSETS_CACHE_PATH, GAMES_STORE_PATH):
        if fpath.is_file():
            fpath.unlink(missing_ok=True)
            removed_items.append(fpath.name)

    for dpath in (GAME_ICONS_DIR, BUILT_MODS_DIR):
        if dpath.is_dir():
            for item in list(dpath.iterdir()):
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                removed_items.append(item.name)

    # Re-seed clean default Battleground profile immediately
    load_managed_games()
    summary = f"Flushed all game and PC data caches ({len(removed_items)} item(s) removed)."
    append_audit_log(action="app.maintenance.flush_all", status="ok", summary=summary, details={"removed": removed_items[:30]})
    return {"status": "ok", "summary": summary, "removed_count": len(removed_items)}


@app.post("/api/app/restart")
async def restart_app_server() -> dict[str, Any]:
    """Restarts the app cleanly (process restart when supervised, or full runtime/cache reset in-process)."""
    global PC_SPECS_RESPONSE_CACHE, _PC_SPECS_MEM_CACHE, AUDIT_LOG_CACHE
    import threading

    invalidate_games_cache()
    PC_SPECS_RESPONSE_CACHE = None
    _PC_SPECS_MEM_CACHE = None
    AUDIT_LOG_CACHE = None
    STEAM_ASSETS_CACHE.clear()
    load_managed_games()

    append_audit_log(action="app.restart", status="ok", summary="Restarted app from App Management.")

    if os.environ.get("MODTOOL_SUPERVISED") == "1":
        def _delayed_exit():
            import time
            time.sleep(0.4)
            os._exit(42)

        threading.Thread(target=_delayed_exit, daemon=True).start()
        return {"status": "ok", "mode": "process_restart", "summary": "Server restarting on http://127.0.0.1:8765..."}

    def _delayed_reload_touch():
        import time
        time.sleep(0.35)
        try:
            os.utime(Path(__file__).resolve(), None)
        except Exception:
            pass

    threading.Thread(target=_delayed_reload_touch, daemon=True).start()
    return {"status": "ok", "mode": "uvicorn_reload", "summary": "App caches cleared and server reload triggered."}


GAME_ICONS_DIR = get_app_data_root() / "game_icons"
GAME_ICONS_DIR.mkdir(parents=True, exist_ok=True)
ICON_EXTRACT_SEMAPHORE = asyncio.Semaphore(4)


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


@app.get("/api/games/{game_id}/icon")
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
                await asyncio.wait_for(proc.wait(), timeout=20)
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


@app.get("/api/games/{game_id}/steam-assets")
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


@app.get("/api/games/{game_id}/steam-asset/{asset_kind}")
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


UNREAL_INI_BACKUP_DIR = get_app_data_root() / "unreal_ini_backups"
UNREAL_INI_BACKUP_DIR.mkdir(parents=True, exist_ok=True)


class ApplyUnrealSettingsRequest(BaseModel):
    game_id: str
    ini_path: str
    updates: dict[str, str] = Field(default_factory=dict)


@app.post("/api/unreal-settings/apply")
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


@app.post("/api/unreal-settings/restore")
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


@app.get("/api/games/{game_id}/conflicts")
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


@app.post("/api/games/{game_id}/launch")
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


@app.post("/api/games/{game_id}/notes")
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


@app.post("/api/games/prewarm-icons")
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


WATCHER_PROC_HANDLE: dict[str, Any] = {"proc": None}


@app.get("/api/app/watcher/status")
async def get_watcher_status() -> dict[str, Any]:
    """Checks whether backup_manager.py --watch is running."""
    proc = WATCHER_PROC_HANDLE.get("proc")
    managed_running = proc is not None and proc.poll() is None
    return {"status": "ok", "running": managed_running, "pid": proc.pid if managed_running else None}


@app.post("/api/app/watcher/toggle")
async def toggle_backup_watcher() -> dict[str, Any]:
    """Starts or stops the managed incremental backup & SemVer watcher process."""
    import subprocess

    proc = WATCHER_PROC_HANDLE.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        WATCHER_PROC_HANDLE["proc"] = None
        append_audit_log(action="app.watcher.stop", status="ok", summary="Stopped managed backup watcher.")
        return {"status": "ok", "running": False, "summary": "Stopped managed backup watcher."}

    bm_path = WORKSPACE_DIR / "backup_manager.py"
    if not bm_path.is_file():
        raise HTTPException(status_code=404, detail="backup_manager.py not found.")
    new_proc = subprocess.Popen(
        [sys.executable, str(bm_path), "--watch"],
        cwd=str(WORKSPACE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    WATCHER_PROC_HANDLE["proc"] = new_proc
    append_audit_log(action="app.watcher.start", status="ok", summary=f"Started backup watcher (PID {new_proc.pid}).")
    return {"status": "ok", "running": True, "pid": new_proc.pid, "summary": f"Started backup watcher (PID {new_proc.pid})."}


SCAN_EXCLUSIONS_PATH = get_app_data_root() / "scan_exclusions.json"


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


@app.post("/api/games/prune-non-games")
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


# =============================================================================
# PLATFORM-AGNOSTIC & ENGINE-AGNOSTIC GAME LIBRARY DISCOVERY
# =============================================================================

LIBRARY_CACHE_PATH = get_app_data_root() / "library_cache.json"

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
    if has_game_assembly and has_global_metadata:
        engine = "Unity_IL2CPP"
    elif any(root_path.glob("*_Data/Managed/Assembly-CSharp.dll")):
        engine = "Unity_Mono"
    elif (root_path / "Engine" / "Binaries" / "Win64").is_dir() or any(root_path.rglob("*.pak")) or any(root_path.rglob("*.ucas")):
        engine = "Unreal_4_5"
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


@app.get("/api/library")
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


@app.post("/api/library/scan-installed")
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


@app.post("/api/library/import-owned")
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


@app.post("/api/library/import-file")
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


# =============================================================================
# PERSISTENT UI BANNER & SETTINGS (/api/ui/banner, /api/ui/banner/settings)
# =============================================================================

UI_SETTINGS_PATH = get_app_data_root() / "ui_settings.json"

BANNER_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def load_ui_settings() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "banner_enabled": True,
        "banner_file": "",
        "banner_fit": "contain",
    }
    if UI_SETTINGS_PATH.is_file():
        try:
            raw = json.loads(UI_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                defaults.update(raw)
        except Exception:
            pass
    if defaults.get("banner_fit") not in ("contain", "cover"):
        defaults["banner_fit"] = "contain"
    defaults["banner_enabled"] = bool(defaults.get("banner_enabled", True))
    defaults["banner_file"] = str(defaults.get("banner_file") or "")
    return defaults


def save_ui_settings(settings: dict[str, Any]) -> None:
    UI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    UI_SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class BannerSettingsRequest(BaseModel):
    banner_enabled: bool | None = None
    banner_fit: str | None = None


@app.post("/api/ui/banner")
async def upload_ui_banner(file: UploadFile = File(...)) -> dict[str, Any]:
    """Saves a custom banner image to %LOCALAPPDATA%\\ModToolPoC\\custom_banner.<ext> and updates ui_settings.json."""
    orig_name = str(file.filename or "banner.png")
    ext = Path(orig_name).suffix.lower()
    if ext not in BANNER_MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image extension '{ext}'. Allowed: png, jpg, jpeg, webp, svg.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded banner file is empty.")

    app_root = get_app_data_root()
    for old_file in app_root.glob("custom_banner.*"):
        if old_file.is_file():
            try:
                old_file.unlink()
            except Exception:
                pass

    target_name = f"custom_banner{ext}"
    target_path = app_root / target_name
    target_path.write_bytes(data)

    settings = load_ui_settings()
    settings["banner_enabled"] = True
    settings["banner_file"] = target_name
    if settings.get("banner_fit") not in ("contain", "cover"):
        settings["banner_fit"] = "contain"
    save_ui_settings(settings)

    append_audit_log(
        action="ui.banner_upload",
        status="ok",
        summary=f"Uploaded custom banner '{target_name}' ({len(data)} bytes)",
        details={"banner_file": target_name, "original_name": orig_name, "size_bytes": len(data)},
    )
    return {
        "status": "ok",
        "banner_enabled": settings["banner_enabled"],
        "banner_file": settings["banner_file"],
        "banner_fit": settings["banner_fit"],
    }


@app.get("/api/ui/banner")
async def get_ui_banner() -> Response:
    """Returns the saved custom banner image with Cache-Control: no-store, or 204 if none is set."""
    settings = load_ui_settings()
    banner_file = str(settings.get("banner_file") or "").strip()
    if not banner_file:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    app_root = get_app_data_root()
    banner_path = app_root / Path(banner_file).name
    if not banner_path.is_file():
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    media_type = BANNER_MEDIA_TYPES.get(banner_path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        banner_path,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/ui/banner/settings")
async def get_ui_banner_settings() -> dict[str, Any]:
    settings = load_ui_settings()
    app_root = get_app_data_root()
    has_file = bool(settings.get("banner_file") and (app_root / Path(settings["banner_file"]).name).is_file())
    return {
        "status": "ok",
        "banner_enabled": settings["banner_enabled"],
        "banner_file": settings["banner_file"],
        "banner_fit": settings["banner_fit"],
        "has_banner": has_file,
    }


@app.post("/api/ui/banner/settings")
async def update_ui_banner_settings(req: BannerSettingsRequest) -> dict[str, Any]:
    """Persists banner_enabled and/or banner_fit ('contain' | 'cover') to ui_settings.json."""
    settings = load_ui_settings()
    if req.banner_enabled is not None:
        settings["banner_enabled"] = bool(req.banner_enabled)
    if req.banner_fit is not None:
        fit = req.banner_fit.strip().lower()
        if fit not in ("contain", "cover"):
            raise HTTPException(status_code=400, detail="banner_fit must be 'contain' or 'cover'.")
        settings["banner_fit"] = fit
    save_ui_settings(settings)

    append_audit_log(
        action="ui.banner_settings",
        status="ok",
        summary=f"Updated banner settings (enabled={settings['banner_enabled']}, fit={settings['banner_fit']})",
        details=settings,
    )
    return {
        "status": "ok",
        "banner_enabled": settings["banner_enabled"],
        "banner_file": settings["banner_file"],
        "banner_fit": settings["banner_fit"],
    }


# =============================================================================
# GITHUB DISTRIBUTION SYNC & LOCAL SNAPSHOT ROLLBACK
# =============================================================================

REPO_CONFIG_PATH = get_app_data_root() / "repo_config.json"
APP_SNAPSHOTS_DIR = get_app_data_root() / "app_backups"
APP_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_REPO_CONFIG: dict[str, str] = {
    "owner": "MrWibbleBottom",
    "repo": "The-Modding-Tool",
    "branch": "main",
}


def _parse_github_owner_repo(owner_or_url: str, repo_name: str = "") -> tuple[str, str]:
    import re

    raw_o = str(owner_or_url or "").strip()
    raw_r = str(repo_name or "").strip()
    for candidate in (raw_o, raw_r):
        if "github.com" in candidate.lower():
            m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s?#]+)", candidate, re.IGNORECASE)
            if m:
                owner = m.group(1).strip()
                repo = m.group(2).strip()
                if repo.lower().endswith(".git"):
                    repo = repo[:-4]
                return owner, repo
    if "/" in raw_o and not raw_r:
        parts = [p.strip() for p in raw_o.split("/") if p.strip()]
        if len(parts) >= 2:
            repo = parts[1][:-4] if parts[1].lower().endswith(".git") else parts[1]
            return parts[0], repo
    if raw_r.lower().endswith(".git"):
        raw_r = raw_r[:-4]
    return raw_o, raw_r


def load_repo_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "owner": DEFAULT_REPO_CONFIG["owner"],
        "repo": DEFAULT_REPO_CONFIG["repo"],
        "branch": DEFAULT_REPO_CONFIG["branch"],
        "token": "",
        "installed_sha": "",
        "last_checked": "",
    }
    file_existed = REPO_CONFIG_PATH.is_file()
    changed = not file_existed
    if file_existed:
        try:
            raw = json.loads(REPO_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in defaults:
                    if raw.get(k) is not None and str(raw.get(k)).strip() != "":
                        defaults[k] = str(raw[k]).strip()
                    elif k in DEFAULT_REPO_CONFIG:
                        defaults[k] = DEFAULT_REPO_CONFIG[k]
                        changed = True
        except Exception:
            changed = True
    if not defaults["owner"]:
        defaults["owner"] = DEFAULT_REPO_CONFIG["owner"]
        changed = True
    if not defaults["repo"]:
        defaults["repo"] = DEFAULT_REPO_CONFIG["repo"]
        changed = True
    if not defaults["branch"]:
        defaults["branch"] = DEFAULT_REPO_CONFIG["branch"]
        changed = True
    if changed:
        save_repo_config(defaults)
    return defaults


def save_repo_config(cfg: dict[str, Any]) -> None:
    REPO_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPO_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _mask_token(tok: str) -> str:
    if not tok:
        return ""
    if len(tok) <= 6:
        return "******"
    return "******" + tok[-4:]


def _safe_repo_config_response(cfg: dict[str, Any]) -> dict[str, Any]:
    tok = cfg.get("token", "")
    sha = cfg.get("installed_sha", "")
    return {
        "owner": cfg.get("owner", ""),
        "repo": cfg.get("repo", ""),
        "branch": cfg.get("branch", "main"),
        "token_set": bool(tok),
        "token_masked": _mask_token(tok),
        "installed_sha": sha,
        "installed_short_sha": sha[:7] if sha else "",
        "last_checked": cfg.get("last_checked", ""),
    }


def _create_app_snapshot(tag: str = "pre_sync") -> dict[str, Any]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snap_name = f"{ts}_{tag}"
    snap_dir = APP_SNAPSHOTS_DIR / snap_name
    snap_dir.mkdir(parents=True, exist_ok=True)

    exclude_parts = {".venv", "__pycache__", ".git", "node_modules"}
    file_hashes: dict[str, str] = {}
    copied = 0
    for fpath in POC_DIR.rglob("*"):
        if not fpath.is_file():
            continue
        rel = fpath.relative_to(POC_DIR)
        if any(part in exclude_parts for part in rel.parts):
            continue
        dest = snap_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fpath, dest)
        file_hashes[str(rel).replace("\\", "/")] = sha256_file(fpath)
        copied += 1

    manifest = {
        "snapshot": snap_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "file_count": copied,
        "files": file_hashes,
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _list_app_snapshots() -> list[dict[str, Any]]:
    if not APP_SNAPSHOTS_DIR.is_dir():
        return []
    snaps: list[dict[str, Any]] = []
    for d in sorted(APP_SNAPSHOTS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        m_path = d / "manifest.json"
        info: dict[str, Any] = {"name": d.name, "created_at": "", "file_count": 0, "tag": ""}
        if m_path.is_file():
            try:
                m = json.loads(m_path.read_text(encoding="utf-8"))
                info["created_at"] = m.get("created_at", "")
                info["file_count"] = m.get("file_count", 0)
                info["tag"] = m.get("tag", "")
            except Exception:
                pass
        snaps.append(info)
    return snaps


def _github_headers(token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ModToolPoC/1.0",
    }
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


class RepoConfigRequest(BaseModel):
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    token: str = ""
    clear_token: bool = False


class RollbackSnapshotRequest(BaseModel):
    snapshot: str


@app.get("/api/app/repo-config")
async def get_app_repo_config() -> dict[str, Any]:
    cfg = load_repo_config()
    return {"status": "ok", **_safe_repo_config_response(cfg)}


@app.post("/api/app/repo-config")
async def save_app_repo_config(req: RepoConfigRequest) -> dict[str, Any]:
    cfg = load_repo_config()
    parsed_owner, parsed_repo = _parse_github_owner_repo(req.owner, req.repo)
    cfg["owner"] = parsed_owner or DEFAULT_REPO_CONFIG["owner"]
    cfg["repo"] = parsed_repo or DEFAULT_REPO_CONFIG["repo"]
    cfg["branch"] = req.branch.strip() or DEFAULT_REPO_CONFIG["branch"]
    if req.clear_token:
        cfg["token"] = ""
    elif req.token.strip() and not req.token.strip().startswith("******"):
        cfg["token"] = req.token.strip()
    save_repo_config(cfg)

    append_audit_log(
        action="app.repo_config.save",
        status="ok",
        summary=f"Saved GitHub repo config ({cfg['owner']}/{cfg['repo']}@{cfg['branch']})",
        details={"owner": cfg["owner"], "repo": cfg["repo"], "branch": cfg["branch"], "token_set": bool(cfg["token"])},
    )
    return {"status": "ok", **_safe_repo_config_response(cfg)}


@app.post("/api/app/check-update")
async def check_github_update() -> dict[str, Any]:
    import urllib.error
    import urllib.request

    cfg = load_repo_config()
    owner, repo, branch = cfg["owner"].strip(), cfg["repo"].strip(), cfg["branch"].strip() or "main"
    if not owner or not repo:
        return {
            "status": "unconfigured",
            "message": "GitHub owner and repository name are not configured.",
            "installed_sha": cfg.get("installed_sha", ""),
            "installed_short_sha": (cfg.get("installed_sha") or "")[:7],
            "update_available": False,
        }

    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"
    req = urllib.request.Request(url, headers=_github_headers(cfg.get("token", "")))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as he:
        if he.code == 401:
            msg = "GitHub authentication failed (401). Check your Private Repo Token."
        elif he.code == 403:
            msg = "GitHub API rate limit exceeded or access forbidden (403). Add a token or retry later."
        elif he.code == 404:
            msg = f"Repository or branch '{owner}/{repo}@{branch}' not found (404)."
        else:
            msg = f"GitHub API error ({he.code})."
        return {"status": "error", "message": msg, "installed_sha": cfg.get("installed_sha", ""), "update_available": False}
    except Exception as exc:
        return {"status": "offline", "message": f"Unable to reach GitHub ({exc}).", "installed_sha": cfg.get("installed_sha", ""), "update_available": False}

    latest_sha = str(data.get("sha") or "").strip()
    commit_obj = data.get("commit") or {}
    commit_msg = str(commit_obj.get("message") or "").splitlines()[0] if commit_obj.get("message") else ""
    commit_date = str(((commit_obj.get("committer") or {}).get("date")) or ((commit_obj.get("author") or {}).get("date")) or "")
    installed_sha = cfg.get("installed_sha", "")
    cfg["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_repo_config(cfg)

    return {
        "status": "ok",
        "latest_sha": latest_sha,
        "short_sha": latest_sha[:7],
        "commit_message": commit_msg,
        "commit_date": commit_date,
        "installed_sha": installed_sha,
        "installed_short_sha": installed_sha[:7],
        "update_available": bool(latest_sha and latest_sha != installed_sha),
    }


@app.post("/api/app/sync")
async def sync_app_from_github() -> dict[str, Any]:
    import io
    import urllib.error
    import urllib.request

    cfg = load_repo_config()
    owner, repo, branch = cfg["owner"].strip(), cfg["repo"].strip(), cfg["branch"].strip() or "main"
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="GitHub owner and repository must be configured before syncing.")

    # 1. Pre-sync snapshot
    snapshot = _create_app_snapshot(tag="pre_sync")

    # 2. Fetch latest commit SHA (best effort)
    latest_sha = ""
    try:
        c_req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}",
            headers=_github_headers(cfg.get("token", "")),
        )
        with urllib.request.urlopen(c_req, timeout=10) as c_resp:
            latest_sha = str(json.loads(c_resp.read().decode("utf-8", errors="replace")).get("sha") or "").strip()
    except Exception:
        pass

    # 3. Download zipball
    zip_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
    z_req = urllib.request.Request(zip_url, headers=_github_headers(cfg.get("token", "")))
    try:
        with urllib.request.urlopen(z_req, timeout=30) as z_resp:
            zip_bytes = z_resp.read()
    except urllib.error.HTTPError as he:
        raise HTTPException(status_code=he.code, detail=f"GitHub zipball download failed ({he.code}).")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub zipball download failed: {exc}")

    archive_sha256 = hashlib.sha256(zip_bytes).hexdigest()
    extracted_files: list[str] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        # Detect if repo has a poc-ui/ subfolder
        stripped_names = [Path(m.filename).parts[1:] for m in members if len(Path(m.filename).parts) > 1]
        has_poc_ui = any(parts and parts[0].lower() == "poc-ui" for parts in stripped_names)
        for m in members:
            parts = Path(m.filename).parts
            if len(parts) <= 1:
                continue
            rel_parts = parts[1:]
            if has_poc_ui:
                if rel_parts[0].lower() != "poc-ui" or len(rel_parts) <= 1:
                    continue
                rel_sub = Path(*rel_parts[1:])
            else:
                rel_sub = Path(*rel_parts)
            dest = (POC_DIR / rel_sub).resolve()
            if not str(dest).startswith(str(POC_DIR.resolve())):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted_files.append(str(rel_sub).replace("\\", "/"))

    if latest_sha:
        cfg["installed_sha"] = latest_sha
    cfg["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_repo_config(cfg)

    append_audit_log(
        action="app.github_sync",
        status="ok",
        summary=f"Synced {len(extracted_files)} file(s) from GitHub {owner}/{repo}@{branch}",
        sha256=archive_sha256,
        details={
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "installed_sha": cfg.get("installed_sha", ""),
            "archive_sha256": archive_sha256,
            "snapshot": snapshot["snapshot"],
            "file_count": len(extracted_files),
        },
    )
    return {
        "status": "ok",
        "installed_sha": cfg.get("installed_sha", ""),
        "installed_short_sha": (cfg.get("installed_sha") or "")[:7],
        "archive_sha256": archive_sha256,
        "snapshot": snapshot["snapshot"],
        "extracted_count": len(extracted_files),
    }


@app.post("/api/app/rollback")
async def rollback_app_snapshot(req: RollbackSnapshotRequest) -> dict[str, Any]:
    snap_name = Path(req.snapshot.strip()).name
    snap_dir = APP_SNAPSHOTS_DIR / snap_name
    if not snap_name or not snap_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Snapshot '{snap_name}' not found.")

    safety_snap = _create_app_snapshot(tag="pre_rollback")
    restored = 0
    for fpath in snap_dir.rglob("*"):
        if not fpath.is_file() or fpath.name == "manifest.json":
            continue
        rel = fpath.relative_to(snap_dir)
        dest = (POC_DIR / rel).resolve()
        if not str(dest).startswith(str(POC_DIR.resolve())):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fpath, dest)
        restored += 1

    append_audit_log(
        action="app.snapshot_rollback",
        status="ok",
        summary=f"Rolled back app to snapshot '{snap_name}' ({restored} file(s))",
        details={"snapshot": snap_name, "safety_snapshot": safety_snap["snapshot"], "restored_count": restored},
    )
    return {
        "status": "ok",
        "snapshot": snap_name,
        "safety_snapshot": safety_snap["snapshot"],
        "restored_count": restored,
    }


def _resolve_git_app_root() -> Path:
    if (WORKSPACE_DIR / ".git").is_dir():
        return WORKSPACE_DIR
    if (POC_DIR / ".git").is_dir():
        return POC_DIR
    return WORKSPACE_DIR


def _sanitize_git_output(text: str, token: str = "", auth_b64: str = "") -> str:
    out = str(text or "")
    if token:
        out = out.replace(token, "***")
    if auth_b64:
        out = out.replace(auth_b64, "***")
    return out.strip()


def _run_git_cmd(
    git_bin: str,
    args: list[str],
    cwd: Path,
    extra_header: str = "",
    timeout: int = 30,
) -> tuple[int, str, str]:
    import subprocess

    cmd = [git_bin]
    if extra_header:
        cmd.extend(["-c", f"http.extraheader={extra_header}"])
    cmd.extend(args)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@app.get("/api/app/git-status")
async def get_app_git_status() -> dict[str, Any]:
    git_bin = shutil.which("git")
    app_root = _resolve_git_app_root()
    is_repo = (app_root / ".git").is_dir()
    resp: dict[str, Any] = {
        "status": "ok",
        "git_available": bool(git_bin),
        "app_root": str(app_root),
        "is_repo": is_repo,
        "branch": "",
        "remote_url": "",
        "changed_files": [],
        "latest_local_commit": None,
    }
    if not git_bin or not is_repo:
        return resp

    rc_br, out_br, _ = _run_git_cmd(git_bin, ["rev-parse", "--abbrev-ref", "HEAD"], app_root)
    if rc_br == 0:
        resp["branch"] = out_br.strip()

    rc_rem, out_rem, _ = _run_git_cmd(git_bin, ["remote", "get-url", "origin"], app_root)
    if rc_rem == 0:
        resp["remote_url"] = out_rem.strip()

    rc_st, out_st, _ = _run_git_cmd(git_bin, ["status", "--porcelain"], app_root)
    if rc_st == 0:
        changed: list[str] = []
        for line in out_st.splitlines():
            if len(line) >= 4:
                changed.append(line[3:].strip())
        resp["changed_files"] = changed

    rc_log, out_log, _ = _run_git_cmd(git_bin, ["log", "-1", "--format=%H%x1f%h%x1f%s%x1f%cI"], app_root)
    if rc_log == 0 and out_log.strip():
        parts = out_log.strip().split("\x1f")
        if len(parts) >= 4:
            resp["latest_local_commit"] = {
                "sha": parts[0],
                "short_sha": parts[1],
                "message": parts[2],
                "date": parts[3],
            }
    return resp


class GitPushRequest(BaseModel):
    commit_message: str = ""
    branch: str = "main"


@app.post("/api/app/git-push")
async def push_app_to_github(req: GitPushRequest) -> dict[str, Any]:
    import base64

    git_bin = shutil.which("git")
    if not git_bin:
        raise HTTPException(status_code=500, detail="Git CLI ('git') is not installed or not on PATH.")

    cfg = load_repo_config()
    owner = cfg.get("owner", "").strip()
    repo = cfg.get("repo", "").strip()
    token = cfg.get("token", "").strip()
    branch = req.branch.strip() or cfg.get("branch", "main").strip() or "main"
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="GitHub owner and repository must be configured before pushing.")

    app_root = _resolve_git_app_root()

    # Ensure .gitignore exists with required exclusions
    gitignore_path = app_root / ".gitignore"
    required_ignores = [".venv/", "__pycache__/", "*.pyc", ".env", "*.log"]
    existing_lines: list[str] = []
    if gitignore_path.is_file():
        existing_lines = gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
    existing_set = {ln.strip() for ln in existing_lines}
    added_ignores = [pat for pat in required_ignores if pat not in existing_set]
    if added_ignores or not gitignore_path.is_file():
        new_lines = existing_lines + added_ignores
        gitignore_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")

    # Initialize .git if needed
    if not (app_root / ".git").is_dir():
        rc_init, _, err_init = _run_git_cmd(git_bin, ["init"], app_root)
        if rc_init != 0:
            raise HTTPException(status_code=500, detail=f"git init failed: {err_init.strip()}")
        _run_git_cmd(git_bin, ["branch", "-M", branch], app_root)

    # Ensure origin remote points to clean HTTPS URL (no token embedded)
    clean_remote_url = f"https://github.com/{owner}/{repo}.git"
    rc_rem, out_rem, _ = _run_git_cmd(git_bin, ["remote", "get-url", "origin"], app_root)
    if rc_rem != 0:
        _run_git_cmd(git_bin, ["remote", "add", "origin", clean_remote_url], app_root)
    elif out_rem.strip() != clean_remote_url:
        _run_git_cmd(git_bin, ["remote", "set-url", "origin", clean_remote_url], app_root)

    # Stage all changes
    rc_add, _, err_add = _run_git_cmd(git_bin, ["add", "-A"], app_root)
    if rc_add != 0:
        raise HTTPException(status_code=500, detail=f"git add -A failed: {err_add.strip()}")

    # Count changed files & commit if staged changes exist
    rc_st, out_st, _ = _run_git_cmd(git_bin, ["status", "--porcelain"], app_root)
    changed_count = len([ln for ln in out_st.splitlines() if ln.strip()]) if rc_st == 0 else 0

    rc_diff, _, _ = _run_git_cmd(git_bin, ["diff", "--cached", "--quiet"], app_root)
    commit_created = False
    if rc_diff != 0:
        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        commit_msg = req.commit_message.strip() or f"Update app ({ts_str})"
        rc_cm, out_cm, err_cm = _run_git_cmd(git_bin, ["commit", "-m", commit_msg], app_root)
        if rc_cm != 0:
            raise HTTPException(status_code=500, detail=f"git commit failed: {(err_cm or out_cm).strip()}")
        commit_created = True

    # Push with ephemeral header if token is set
    auth_b64 = ""
    extra_header = ""
    if token:
        auth_b64 = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        extra_header = f"AUTHORIZATION: basic {auth_b64}"

    rc_push, out_push, err_push = _run_git_cmd(
        git_bin,
        ["push", "-u", "origin", branch],
        app_root,
        extra_header=extra_header,
        timeout=60,
    )
    sanitized_out = _sanitize_git_output(f"{out_push}\n{err_push}", token=token, auth_b64=auth_b64)
    if rc_push != 0:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "detail": "git push failed",
                "stderr": _sanitize_git_output(err_push or out_push, token=token, auth_b64=auth_b64),
            },
        )

    rc_head, out_head, _ = _run_git_cmd(git_bin, ["rev-parse", "HEAD"], app_root)
    head_sha = out_head.strip() if rc_head == 0 else ""
    if head_sha:
        cfg["installed_sha"] = head_sha
        cfg["branch"] = branch
        save_repo_config(cfg)

    append_audit_log(
        action="GIT_PUSH",
        status="ok",
        summary=f"Pushed {owner}/{repo}@{branch} ({head_sha[:7] or 'HEAD'}, {changed_count} changed file(s))",
        sha256=head_sha,
        details={
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "commit_sha": head_sha,
            "changed_file_count": changed_count,
            "commit_created": commit_created,
        },
    )
    return {
        "status": "ok",
        "commit_sha": head_sha,
        "short_sha": head_sha[:7],
        "branch": branch,
        "changed_file_count": changed_count,
        "commit_created": commit_created,
        "output": sanitized_out or "Push completed.",
    }


# =============================================================================
# ENGINE-AGNOSTIC ASSET & MODDABILITY ANALYZER (GREEN / AMBER / RED)
# =============================================================================

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


def _check_local_modding_tools(root_path: Path, engine: str) -> dict[str, dict[str, Any]]:
    """Checks configured MCP bridges and local helper executables in PATH / TOOLS_ROOT."""
    tools: dict[str, dict[str, Any]] = {}

    # MCP Bridges
    tools["filesystem-mcp"] = {"available": True, "detail": "Built-in local filesystem access"}
    bep_bridge_ok = Path(DEFAULT_MCP_APP).is_file() and (
        (root_path / "BepInEx" / "plugins").is_dir() or (WORKSPACE_DIR / "bepinex-mcp").is_dir()
    )
    tools["bepinex-mcp"] = {
        "available": bep_bridge_ok,
        "detail": "bepinex-mcp ready" if bep_bridge_ok else "Install BepInEx + bridge in game",
    }
    ue4ss_ok = any(root_path.glob("*/Binaries/Win64/UE4SS.dll")) or (TOOLS_ROOT / "re_ue4ss").is_dir()
    tools["ue4ss-mcp"] = {
        "available": ue4ss_ok,
        "detail": "UE4SS detected" if ue4ss_ok else "Install RE-UE4SS (App Management / Game tab)",
    }
    tools["creation-kit-mcp"] = {"available": False, "detail": "External Creation Kit / xEdit"}
    tools["godot-mcp"] = {"available": bool(shutil.which("godot")), "detail": "Godot CLI on PATH" if shutil.which("godot") else "Godot / GDRE Tools not found"}

    # Offline extraction/repacking helpers
    for tool_name, folder_hint, exe_names in (
        ("AssetRipper", "assetripper", ("AssetRipper.exe", "AssetRipper.GUI.Free.exe")),
        ("Il2CppDumper", "il2cppdumper", ("Il2CppDumper.exe",)),
        ("FModel", "fmodel", ("FModel.exe",)),
        ("UnrealPak", "unrealpak", ("UnrealPak.exe",)),
    ):
        found_path = ""
        for ex in exe_names:
            w = shutil.which(ex)
            if w:
                found_path = w
                break
        if not found_path and (TOOLS_ROOT / folder_hint).is_dir():
            for ex in exe_names:
                cand = next((TOOLS_ROOT / folder_hint).rglob(ex), None)
                if cand and cand.is_file():
                    found_path = str(cand)
                    break
        tools[tool_name] = {
            "available": bool(found_path),
            "detail": found_path if found_path else f"{tool_name} not found locally",
        }
    return tools


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


@app.get("/api/game/moddability-guide")
async def get_moddability_guide() -> dict[str, Any]:
    return {
        "status": "ok",
        "guide": list(MODDABILITY_GUIDE.values()),
        "guide_by_key": MODDABILITY_GUIDE,
    }


@app.post("/api/game/analyze-assets")
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

    tools = _check_local_modding_tools(root_path, engine)
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

    # 4. Runtime Code & Scripting Surfaces (GREEN if active bridge / Creation, AMBER if bridge missing, plus RED AOT note)
    if counts["runtime_code"] > 0 or engine in ("Unity_IL2CPP", "Unity_Mono", "Unreal_4_5", "Creation_Engine", "Godot"):
        if engine in ("Unity_IL2CPP", "Unity_Mono"):
            bep_ok = tools["bepinex-mcp"]["available"]
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
        elif engine == "Unreal_4_5":
            ue_ok = tools["ue4ss-mcp"]["available"]
            layers.append({
                "status": "green" if ue_ok and counts["anti_cheat"] == 0 else "amber",
                "layer_name": "Unreal Blueprint & Runtime Hooks (UE4SS)",
                "detected_items": samples["runtime_code"] or ["Unreal Engine Binaries"],
                "file_count": counts["runtime_code"],
                "required_tools": ["ue4ss-mcp"],
                "tool_status": tools["ue4ss-mcp"]["detail"],
                "what_can_change": "Blueprint hooks, Lua mods, and live UObject property overrides.",
                "blocker_key": "" if ue_ok else "packed_binary_containers",
            })
        elif engine == "Creation_Engine":
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

        if engine in ("Unity_IL2CPP", "Unreal_4_5", "Unknown"):
            layers.append({
                "status": "red",
                "layer_name": "AOT-Compiled Native / IL2CPP Source Code",
                "detected_items": samples["runtime_code"][:4] or ["Compiled Game Binaries"],
                "file_count": counts["runtime_code"],
                "required_tools": ["Il2CppDumper (read-only metadata)"],
                "tool_status": tools["Il2CppDumper"]["detail"],
                "what_can_change": "Cannot edit/recompile original C#/C++ source code directly (use runtime hooks instead).",
                "blocker_key": "aot_compiled_code",
            })

    # 5. Packed Containers & Signed / Encrypted Archives (AMBER / RED)
    if counts["packed_containers"] > 0:
        helper_list = ["FModel", "UnrealPak"] if engine == "Unreal_4_5" else ["AssetRipper"]
        helper_status = ", ".join(f"{h}: {'Found' if tools.get(h, {}).get('available') else 'Missing'}" for h in helper_list)
        layers.append({
            "status": "amber",
            "layer_name": "Packed Binary Engine Containers",
            "detected_items": samples["packed_containers"],
            "file_count": counts["packed_containers"],
            "required_tools": helper_list,
            "tool_status": helper_status,
            "what_can_change": "Requires offline unpacking/repacking tools before replacing bundled textures, meshes, or audio.",
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
            "required_tools": ["RE-UE4SS / Sig Bypass"],
            "tool_status": tools["ue4ss-mcp"]["detail"],
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
        "anti_cheat_status": fp["anti_cheat_status"],
        "recommended_adapter": fp["recommended_adapter"],
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary_counts,
        "counts": counts,
        "tools": tools,
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


@app.get("/api/health")
async def health_check() -> JSONResponse:
    """Lightweight health check endpoint with no-store cache header."""
    return JSONResponse(content={"status": "ok"}, status_code=200, headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)

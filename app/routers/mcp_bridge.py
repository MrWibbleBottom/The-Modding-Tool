"""Thin HTTP routes for MCP stdio bridge, fingerprinting, provisioning, restore, and mod builder."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

from app.core.audit import append_audit_log
from app.core.config import (
    BUILT_MODS_DIR,
    DEFAULT_BEPINEX_SHA256,
    DEFAULT_BEPINEX_ZIP,
    DEFAULT_GAME_DIR,
    DEFAULT_MCP_APP,
    DEFAULT_PLUGIN_BUILD_DIR,
    DEFAULT_VENV_PYTHON,
    GAMES_STORE_PATH,
    get_bepinex_bridge_roots,
    sha256_file,
)
from app.engines.unity import fingerprint_unity_directory
from app.services.discovery import make_slug
from app.services.game_store import inspect_game_modding_info
from app.services.mod_builder import (
    _cs_escape,
    _generate_il2cpp_csproj,
    _generate_il2cpp_plugin_cs,
    build_compatible_mod_impl,
    install_built_mod_impl,
    provision_game_impl,
    restore_game_impl,
)

router = APIRouter()


class PersistentMcpStdioManager:
    """Maintain a single persistent MCP stdio session to ModdersHelperApp.py."""

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
        if not Path(python_path).is_file() and "bepinex-mcp" in python_path.lower():
            if Path(DEFAULT_VENV_PYTHON).is_file():
                python_path = DEFAULT_VENV_PYTHON
        if not Path(script_path).is_file() and "bepinex-mcp" in script_path.lower():
            for root in get_bepinex_bridge_roots():
                candidate = root / "mcp" / "ModdersHelperApp.py"
                if candidate.is_file():
                    script_path = str(candidate)
                    break
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
    author: str = Field(default="The Modding Tool")
    apply_interval_sec: float = Field(default=2.0)
    edits: list[ModEditRule] = Field(default_factory=list)


class InstallBuiltModRequest(BaseModel):
    mod_slug: str
    game_id: str = Field(default="battleground")
    game_dir: str = Field(default=DEFAULT_GAME_DIR)


@router.post("/api/fingerprint")
async def fingerprint_game(req: FingerprintRequest) -> dict[str, Any]:
    game_dir = Path(req.game_dir.strip())
    if not game_dir.is_dir():
        append_audit_log(
            action="fingerprint",
            status="error",
            summary=f"Target directory not found: {game_dir}",
        )
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {game_dir}")

    result = fingerprint_unity_directory(game_dir)
    summary = (
        f"Fingerprint {game_dir.name}: runtime={result['runtime']}, "
        f"interop={result['bepinex_interop_exists']}, blocked={result['blocked']}"
    )
    append_audit_log(
        action="fingerprint",
        status="blocked" if result["blocked"] else ("ok" if result["is_unity_il2cpp"] else "warning"),
        summary=summary,
        sha256=result["game_assembly_sha256"],
        details=result,
    )
    return result


@router.post("/api/provision")
async def provision_game(req: ProvisionRequest) -> dict[str, Any]:
    return provision_game_impl(
        req.game_dir,
        req.bepinex_zip_path,
        req.expected_sha256,
        req.plugin_build_dir,
    )


@router.post("/api/restore")
async def restore_game(req: RestoreRequest) -> dict[str, Any]:
    return restore_game_impl(req.game_dir)


@router.post("/api/mcp/call")
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
            append_audit_log("mcp.search", "ok", f"MCP {mode} query executed", details={"arguments": args})
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


@router.post("/api/mods/build")
async def build_compatible_mod(req: BuildModRequest) -> dict[str, Any]:
    return await build_compatible_mod_impl(
        req,
        ModEditRule,
        built_mods_dir=BUILT_MODS_DIR,
        inspect_fn=inspect_game_modding_info,
    )


@router.post("/api/mods/install")
async def install_built_mod_to_game(req: InstallBuiltModRequest) -> dict[str, Any]:
    return install_built_mod_impl(
        req.mod_slug,
        req.game_id,
        req.game_dir,
        built_mods_dir=BUILT_MODS_DIR,
    )

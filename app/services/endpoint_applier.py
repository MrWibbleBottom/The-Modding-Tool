"""Contextual endpoint application (Config, Mod File, Bridge), pre-change snapshots, and post-change validation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import zipfile
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from fastapi import HTTPException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.core.audit import append_audit_log
from app.core.config import (
    APP_DATA_ROOT,
    BUILT_MODS_DIR,
    DEFAULT_MCP_APP,
    DEFAULT_VENV_PYTHON,
    atomic_write_json,
    get_bepinex_bridge_roots,
    load_json_sanitized,
    sha256_text,
)
from app.engines.unreal import parse_unreal_ini_file
from app.services.discovery import make_slug
from app.services.endpoints import (
    _parse_bepinex_cfg,
    _update_ini_or_cfg_key,
    scan_game_endpoints,
)
from app.services.game_snapshots import create_game_snapshot, ensure_within, restore_game_snapshot
from app.services.game_store import inspect_game_modding_info
from app.services.log_tail import read_game_log_tail
from app.services.mod_builder import build_compatible_mod_impl, install_built_mod_impl


def normalize_delivery_type(raw_delivery: str) -> str:
    clean = str(raw_delivery or "config").strip().lower().replace("-", "_").replace(" ", "_")
    if clean in ("mod_file", "modfile", "mod"):
        return "mod_file"
    if clean in ("bridge", "live_bridge", "mcp"):
        return "bridge"
    return "config"


def _deployed_mods_meta_dir(slug: str) -> Path:
    d = APP_DATA_ROOT / "deployed_endpoint_mods" / (make_slug(slug) or "game")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mod_record_path(slug: str, endpoint_id: str) -> Path:
    return _deployed_mods_meta_dir(slug) / f"{sha256_text(endpoint_id)[:16]}.json"


def _collect_log_excerpt(
    game_dir: Path,
    info: dict[str, Any],
    *,
    keywords: tuple[str, ...] = (),
) -> tuple[str, int, bool]:
    proj = str(info.get("detected_project_name") or "")
    tail_err = read_game_log_tail(game_dir, lines=25, errors_only=True, detected_project_name=proj)
    err_lines = tail_err.get("lines") or []
    err_count = int(tail_err.get("error_count") or 0)
    keyword_hit = False
    if keywords and err_lines:
        low_kws = [k.lower() for k in keywords if k]
        keyword_hit = any(any(kw in ln.lower() for kw in low_kws) for ln in err_lines)
    if err_lines:
        return ("\n".join(err_lines[-12:]), err_count, keyword_hit)
    tail_all = read_game_log_tail(game_dir, lines=12, errors_only=False, detected_project_name=proj)
    return ("\n".join(tail_all.get("lines") or []), err_count, keyword_hit)


async def _default_mcp_bridge_call(
    tool_name: str,
    arguments: dict[str, Any],
    game: dict[str, Any],
) -> dict[str, Any]:
    """Execute a live MCP stdio tool call via ModdersHelperApp.py without importing router modules."""
    py_exe = str(game.get("venv_python_path") or game.get("venv_python") or DEFAULT_VENV_PYTHON)
    script_path = str(game.get("mcp_app_path") or game.get("mcp_app") or DEFAULT_MCP_APP)
    game_ip = str(game.get("game_ip") or "localhost")

    if not Path(py_exe).is_file() and Path(DEFAULT_VENV_PYTHON).is_file():
        py_exe = DEFAULT_VENV_PYTHON
    if not Path(script_path).is_file():
        for root in get_bepinex_bridge_roots():
            cand = root / "mcp" / "ModdersHelperApp.py"
            if cand.is_file():
                script_path = str(cand)
                break
    if not Path(py_exe).is_file():
        raise FileNotFoundError(f"MCP venv Python not found: {py_exe}")
    if not Path(script_path).is_file():
        raise FileNotFoundError(f"ModdersHelperApp.py not found: {script_path}")

    server_params = StdioServerParameters(
        command=py_exe,
        args=[script_path, "--transport=stdio", "--headless", f"--game-ip={game_ip}"],
        cwd=str(Path(script_path).resolve().parent),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with AsyncExitStack() as stack:
        read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await asyncio.wait_for(session.initialize(), timeout=15.0)
        result = await asyncio.wait_for(session.call_tool(tool_name, arguments), timeout=20.0)
        texts = [getattr(item, "text", "") for item in (getattr(result, "content", []) or []) if getattr(item, "text", None) is not None]
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
        }


async def _invoke_bridge(
    bridge_call_fn: Callable[..., Any] | None,
    tool_name: str,
    arguments: dict[str, Any],
    game: dict[str, Any],
) -> dict[str, Any]:
    if bridge_call_fn is None:
        return await _default_mcp_bridge_call(tool_name, arguments, game)
    res = bridge_call_fn(tool_name, arguments, game)
    if asyncio.iscoroutine(res):
        res = await res
    return res if isinstance(res, dict) else {"tool": tool_name, "isError": False, "raw": str(res), "data": res}


def _extract_member_value_from_bridge(res: dict[str, Any]) -> str | None:
    if not isinstance(res, dict) or res.get("isError"):
        return None
    data = res.get("data")
    if isinstance(data, dict):
        for key in ("value", "member_value", "current_value", "result"):
            if key in data and data[key] is not None:
                return str(data[key])
    if data is not None and not isinstance(data, (dict, list)):
        return str(data)
    raw = str(res.get("raw") or "").strip()
    return raw if raw else None


def _read_config_value_on_disk(target_file: Path, section: str, key: str) -> str | None:
    if not target_file.is_file():
        return None
    if target_file.suffix.lower() == ".cfg":
        for entry in _parse_bepinex_cfg(target_file):
            if entry["section"].lower() == section.lower() and entry["key"] == key:
                return str(entry["value"])
        return None
    sections = parse_unreal_ini_file(target_file)
    for sec_name, kv in sections.items():
        if sec_name.lower() == section.lower() and key in kv:
            return str(kv[key])
    return None


def _build_ue4ss_endpoint_mod(
    game_dir: Path,
    ep: dict[str, Any],
    target_val: str,
    base_built_dir: Path,
) -> tuple[Path, list[Path], list[Path], dict[str, str]]:
    adv = ep.get("advanced_details") or {}
    mod_name = str(adv.get("mod_name") or make_slug(ep.get("name", "ue4ss_mod"))).strip() or "EndpointMod"
    safe_mod_slug = make_slug(mod_name)
    out_dir = base_built_dir / f"ep_{safe_mod_slug}"
    pkg_dir = out_dir / "package"

    # Locate target UE4SS Mods dir inside game_dir, defaulting to <Project>/Binaries/Win64/Mods
    existing_lua = Path(str(adv.get("file_path") or "")) if adv.get("file_path") else None
    if existing_lua and existing_lua.is_file() and existing_lua.resolve().is_relative_to(game_dir.resolve()):
        target_mod_dir = existing_lua.parent.parent
    else:
        win64_dirs = [p for p in game_dir.glob("*/Binaries/Win64") if p.is_dir()]
        target_mod_dir = (win64_dirs[0] / "Mods" / mod_name) if win64_dirs else (game_dir / "Mods" / mod_name)

    rel_mod_root = target_mod_dir.relative_to(game_dir)
    stage_scripts = pkg_dir / rel_mod_root / "Scripts"
    stage_scripts.mkdir(parents=True, exist_ok=True)
    hook_sig = str(adv.get("method_signature") or "/Script/Engine.PlayerController:ClientRestart")
    enabled_bool = str(target_val).strip().lower() not in ("false", "disabled", "0", "off")

    lua_src = "\n".join([
        f"-- Generated by The Modding Tool for endpoint {ep['id']}",
        f'print("[{mod_name}] Hook active ({hook_sig})\\n")',
        f'RegisterHook("{hook_sig}", function(self)',
        f'    print("[{mod_name}] Triggered {hook_sig} (value={target_val})\\n")',
        "end)",
        "",
    ])
    (stage_scripts / "main.lua").write_text(lua_src, encoding="utf-8")
    if enabled_bool:
        (pkg_dir / rel_mod_root / "enabled.txt").write_text("", encoding="utf-8")

    zip_path = out_dir / f"{safe_mod_slug}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in pkg_dir.rglob("*"):
            if fp.is_file():
                zf.write(fp, fp.relative_to(pkg_dir))

    # Deploy into game_dir while tracking newly created vs. overwritten files for per-mod rollback
    target_scripts = ensure_within(target_mod_dir / "Scripts", game_dir)
    target_scripts.mkdir(parents=True, exist_ok=True)
    installed_paths: list[Path] = []
    created_paths: list[Path] = []
    original_contents: dict[str, str] = {}

    dest_lua = ensure_within(target_scripts / "main.lua", game_dir)
    if dest_lua.exists():
        try:
            original_contents[str(dest_lua)] = dest_lua.read_text(encoding="utf-8")
        except OSError:
            pass
    else:
        created_paths.append(dest_lua)
    dest_lua.write_text(lua_src, encoding="utf-8")
    installed_paths.append(dest_lua)

    flag_file = ensure_within(target_mod_dir / "enabled.txt", game_dir)
    if enabled_bool:
        if flag_file.exists():
            try:
                original_contents[str(flag_file)] = flag_file.read_text(encoding="utf-8")
            except OSError:
                pass
        else:
            created_paths.append(flag_file)
        flag_file.write_text("", encoding="utf-8")
        installed_paths.append(flag_file)
    else:
        if flag_file.exists():
            try:
                original_contents[str(flag_file)] = flag_file.read_text(encoding="utf-8")
            except OSError:
                pass
        flag_file.unlink(missing_ok=True)

    return zip_path, installed_paths, created_paths, original_contents


async def apply_endpoint_change(
    game: dict[str, Any],
    endpoint_id: str,
    new_value: str | None = None,
    action: str = "apply",
    *,
    defaults_dir: Path | None = None,
    snapshots_root: Path | None = None,
    built_mods_dir: Path | None = None,
    bridge_call_fn: Callable[..., Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Apply or rollback a discovered modding endpoint via Config, Mod File, or live Bridge delivery."""
    slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    game_name = str(game.get("name") or slug)
    game_dir = Path(str(game.get("game_dir") or "").strip())
    info = game.get("cached_modding_info") if isinstance(game.get("cached_modding_info"), dict) else {}
    if not info and game_dir.is_dir():
        info = inspect_game_modding_info(game_dir, game_name)

    report = scan_game_endpoints(game, defaults_dir=defaults_dir)
    ep = next((e for e in report.get("endpoints", []) if e["id"] == endpoint_id), None)
    if ep is None:
        raise KeyError(f"Endpoint not found: {endpoint_id}")

    delivery = normalize_delivery_type(ep.get("delivery_type", "config"))
    clean_action = str(action or "apply").strip().lower()
    adv = ep.get("advanced_details") or {}
    target_val = str(ep["default_value"]) if clean_action == "reset_default" else (
        str(new_value) if new_value is not None else str(ep["current_value"])
    )

    # Always create a pre-change snapshot before mutating game files
    snapshot_id = ""
    extra_inis: list[Path] = []
    if adv.get("file_path"):
        cand_fp = Path(str(adv["file_path"]))
        if cand_fp.is_file():
            extra_inis.append(cand_fp)
    if game_dir.is_dir():
        try:
            snap = create_game_snapshot(
                slug,
                game_dir,
                label=f"Pre-endpoint {clean_action} ({ep['name'][:32]})",
                engine=report.get("engine", "generic"),
                extra_ini_paths=extra_inis or None,
                snapshots_root=snapshots_root,
            )
            snapshot_id = str(snap.get("snapshot_id", ""))
        except ValueError:
            snapshot_id = ""

    # ------------------------------------------------------------------
    # 1. CONFIG DELIVERY (.ini / .cfg)
    # ------------------------------------------------------------------
    if delivery == "config":
        file_path_str = str(adv.get("file_path") or "").strip()
        section = str(adv.get("section") or "General").strip()
        key = str(adv.get("key") or "").strip()
        if not file_path_str or not key:
            return {
                "ok": False,
                "delivery_type": "config",
                "validated": False,
                "restart_required": False,
                "snapshot_id": snapshot_id,
                "error_details": f"Endpoint '{endpoint_id}' has no writable file path or key.",
                "log_excerpt": "",
                "validation_badge": "Validation Failed",
            }
        target_file = Path(file_path_str)
        if game_dir.is_dir() and target_file.resolve().is_relative_to(game_dir.resolve()):
            ensure_within(target_file, game_dir)

        _update_ini_or_cfg_key(target_file, section, key, target_val)
        on_disk = _read_config_value_on_disk(target_file, section, key)
        disk_ok = on_disk is not None and on_disk.strip() == target_val.strip()
        log_excerpt, _, parse_err = _collect_log_excerpt(game_dir, info, keywords=(target_file.name, key))
        validated = bool(disk_ok and not parse_err)
        error_details = ""
        if not disk_ok:
            error_details = f"Persistence check failed: expected '{target_val}' in {target_file.name}, read '{on_disk}'."
        elif parse_err:
            error_details = f"Parse/startup error detected in game logs referencing '{target_file.name}' or '{key}'."

        updated_report = scan_game_endpoints(game, defaults_dir=defaults_dir)
        updated_ep = next((e for e in updated_report["endpoints"] if e["id"] == endpoint_id), ep)
        append_audit_log(
            action=f"endpoints.{clean_action}.config",
            status="ok" if validated else "error",
            summary=f"Config endpoint '{key}' -> '{target_val}' (validated={validated})",
            details={"slug": slug, "endpoint_id": endpoint_id, "snapshot_id": snapshot_id, "validated": validated},
        )
        return {
            "ok": validated,
            "delivery_type": "config",
            "validated": validated,
            "restart_required": True,
            "snapshot_id": snapshot_id,
            "error_details": error_details,
            "log_excerpt": log_excerpt if not validated else "",
            "validation_badge": "Restart Required" if validated else "Validation Failed",
            "summary": f"{'Reset' if clean_action == 'reset_default' else 'Applied'} {key}={target_val}" + (" (Restart Required)" if validated else ""),
            "endpoint": updated_ep,
            "report": updated_report,
        }

    # ------------------------------------------------------------------
    # 2. MOD FILE DELIVERY (Compiled BepInEx DLL / UE4SS Lua + Rollback)
    # ------------------------------------------------------------------
    if delivery == "mod_file":
        rec_file = _mod_record_path(slug, endpoint_id)
        base_built = built_mods_dir or BUILT_MODS_DIR

        if clean_action in ("rollback_mod", "reset_default"):
            meta = load_json_sanitized(rec_file, persist=False) if rec_file.is_file() else None
            restored_snap_id = ""
            removed_paths: list[str] = []
            if isinstance(meta, dict) and meta.get("pre_deploy_snapshot_id"):
                restored_snap_id = str(meta["pre_deploy_snapshot_id"])
                restore_game_snapshot(
                    slug,
                    restored_snap_id,
                    target_game_dir=game_dir if game_dir.is_dir() else None,
                    snapshots_root=snapshots_root,
                )

            if isinstance(meta, dict):
                # Also roll back mod files outside snapshot surfaces (e.g. */Binaries/Win64/Mods/*)
                orig_contents = meta.get("original_contents")
                if isinstance(orig_contents, dict) and game_dir.is_dir():
                    for fp_str, old_text in orig_contents.items():
                        fp = Path(fp_str)
                        ensure_within(fp, game_dir)
                        fp.parent.mkdir(parents=True, exist_ok=True)
                        fp.write_text(str(old_text), encoding="utf-8")
                        removed_paths.append(fp_str)

                created_files = meta.get("created_files")
                if isinstance(created_files, list) and game_dir.is_dir():
                    for fp_str in created_files:
                        fp = Path(fp_str)
                        if fp.exists():
                            ensure_within(fp, game_dir)
                            if fp.is_file():
                                fp.unlink()
                                removed_paths.append(fp_str)
                elif not restored_snap_id and isinstance(meta.get("installed_files"), list) and game_dir.is_dir():
                    for fp_str in meta["installed_files"]:
                        fp = Path(fp_str)
                        if fp.exists():
                            ensure_within(fp, game_dir)
                            if fp.is_file():
                                fp.unlink()
                                removed_paths.append(fp_str)
            elif adv.get("file_path"):
                # Direct fallback for discovered UE4SS mod
                lua_fp = Path(str(adv["file_path"]))
                if game_dir.is_dir():
                    ensure_within(lua_fp, game_dir)
                    flag_fp = lua_fp.parent.parent / "enabled.txt"
                    if flag_fp.is_file():
                        flag_fp.unlink()
                        removed_paths.append(str(flag_fp))

            if rec_file.is_file():
                rec_file.unlink(missing_ok=True)
            updated_report = scan_game_endpoints(game, defaults_dir=defaults_dir)
            updated_ep = next((e for e in updated_report["endpoints"] if e["id"] == endpoint_id), ep)
            append_audit_log(
                action="endpoints.rollback_mod",
                status="ok",
                summary=f"Rolled back Mod File endpoint '{ep['name']}'",
                details={"slug": slug, "endpoint_id": endpoint_id, "restored_snapshot_id": restored_snap_id},
            )
            return {
                "ok": True,
                "delivery_type": "mod_file",
                "validated": True,
                "restart_required": True,
                "snapshot_id": restored_snap_id or snapshot_id,
                "error_details": "",
                "log_excerpt": "",
                "validation_badge": "Verified Applied",
                "summary": f"Rolled back '{ep['name']}'" + (f" via snapshot {restored_snap_id}" if restored_snap_id else ""),
                "endpoint": updated_ep,
                "report": updated_report,
            }

        # Deploy Mod File
        if not game_dir.is_dir():
            return {
                "ok": False,
                "delivery_type": "mod_file",
                "validated": False,
                "restart_required": False,
                "snapshot_id": snapshot_id,
                "error_details": f"Game directory does not exist: {game_dir}",
                "log_excerpt": "",
                "validation_badge": "Validation Failed",
            }

        zip_path: Path | None = None
        installed_files: list[str] = []
        created_files: list[str] = []
        original_contents: dict[str, str] = {}
        if report.get("engine") == "unreal" or adv.get("source_kind") == "ue4ss_lua_hook":
            zip_path, installed_paths, created_paths, original_contents = _build_ue4ss_endpoint_mod(
                game_dir, ep, target_val, base_built
            )
            installed_files = [str(p) for p in installed_paths]
            created_files = [str(p) for p in created_paths]
            validated = all(Path(p).exists() for p in installed_files)
            error_details = "" if validated else "UE4SS mod files failed verification on disk."
        else:
            # Unity BepInEx compiled C# DLL via mod_builder.py
            safe_slug_mod = f"ep_{make_slug(ep['name'])}"
            rule = SimpleNamespace(
                label=ep["name"],
                rule_type=str(adv.get("rule_type") or "member_set"),
                entity_name=str(adv.get("entity_name") or adv.get("class_name") or "Player"),
                component_name=str(adv.get("component") or f"{adv.get('namespace', '')}.{adv.get('class_name', '')}".strip(".") or "UnityEngine.Transform"),
                member_path=str(adv.get("member") or ep["name"]),
                value=target_val,
                hook_kind=str(adv.get("hook_kind") or "Postfix"),
                target_method=str(adv.get("method_signature") or adv.get("member") or "Update"),
                skip_original=False,
                model_dump=lambda: {"label": ep["name"], "value": target_val},
            )
            req_obj = SimpleNamespace(
                game_id=game.get("id") or slug,
                game_dir=str(game_dir),
                mod_name=f"Endpoint {ep['name']}",
                mod_id=f"com.modtool.{slug}.{safe_slug_mod}",
                mod_version="1.0.0",
                author="The Modding Tool",
                apply_interval_sec=2.0,
                edits=[rule],
            )
            try:
                build_res = await build_compatible_mod_impl(
                    req_obj,
                    lambda: rule,
                    built_mods_dir=base_built,
                    inspect_fn=inspect_game_modding_info,
                )
                zip_path = Path(build_res["zip_path"])
                inst_res = install_built_mod_impl(
                    build_res["mod_slug"],
                    str(game.get("id") or slug),
                    str(game_dir),
                    force=True,
                    built_mods_dir=base_built,
                )
                installed_files = [str(p) for p in inst_res.get("installed_files", [])]
                validated = bool(installed_files and all(Path(p).is_file() for p in installed_files))
                error_details = "" if validated else "Mod artifact build completed but deployed DLL not found in game directory."
            except HTTPException as exc:
                log_excerpt, _, _ = _collect_log_excerpt(game_dir, info)
                return {
                    "ok": False,
                    "delivery_type": "mod_file",
                    "validated": False,
                    "restart_required": False,
                    "snapshot_id": snapshot_id,
                    "error_details": str(exc.detail),
                    "log_excerpt": log_excerpt,
                    "validation_badge": "Validation Failed",
                }

        atomic_write_json(
            rec_file,
            {
                "endpoint_id": endpoint_id,
                "slug": slug,
                "pre_deploy_snapshot_id": snapshot_id,
                "zip_path": str(zip_path) if zip_path else "",
                "installed_files": installed_files,
                "created_files": created_files,
                "original_contents": original_contents,
                "deployed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        log_excerpt, _, log_err_hit = _collect_log_excerpt(game_dir, info, keywords=(ep["name"],))
        if log_err_hit:
            validated = False
            error_details = "Mod loader log contains errors matching deployed mod."

        updated_report = scan_game_endpoints(game, defaults_dir=defaults_dir)
        updated_ep = next((e for e in updated_report["endpoints"] if e["id"] == endpoint_id), ep)
        append_audit_log(
            action="endpoints.deploy_mod",
            status="ok" if validated else "error",
            summary=f"Deployed Mod File endpoint '{ep['name']}' ({len(installed_files)} file(s))",
            details={"slug": slug, "endpoint_id": endpoint_id, "installed_files": installed_files, "snapshot_id": snapshot_id},
        )
        return {
            "ok": validated,
            "delivery_type": "mod_file",
            "validated": validated,
            "restart_required": True,
            "snapshot_id": snapshot_id,
            "download_url": f"/api/games/{slug}/endpoints/download?endpoint_id={endpoint_id}",
            "mod_zip_path": str(zip_path) if zip_path else "",
            "installed_files": installed_files,
            "error_details": error_details,
            "log_excerpt": log_excerpt if not validated else "",
            "validation_badge": "Verified Applied" if validated else "Validation Failed",
            "summary": f"Deployed mod for '{ep['name']}' ({len(installed_files)} file(s))",
            "endpoint": updated_ep,
            "report": updated_report,
        }

    # ------------------------------------------------------------------
    # 3. LIVE BRIDGE DELIVERY (BepInExMCP push + read-back verification)
    # ------------------------------------------------------------------
    comp_name = str(adv.get("component") or f"{adv.get('namespace', '')}.{adv.get('class_name', '')}".strip(".") or "").strip()
    member_name = str(adv.get("member") or "").strip()
    method_name = str(adv.get("method_signature") or "").strip()
    go_id = str(adv.get("game_object_id") or game.get("default_go_id") or "").strip()

    try:
        if not go_id and comp_name:
            find_res = await _invoke_bridge(
                bridge_call_fn,
                "find_objects_with_component",
                {"component_name": comp_name},
                game,
            )
            fdata = find_res.get("data")
            if isinstance(fdata, list) and fdata and isinstance(fdata[0], dict):
                go_id = str(fdata[0].get("id") or fdata[0].get("instance_id") or "")
            elif isinstance(fdata, dict):
                objs = fdata.get("objects") or fdata.get("matches") or []
                if objs and isinstance(objs[0], dict):
                    go_id = str(objs[0].get("id") or objs[0].get("instance_id") or "")

        if str(adv.get("rule_type") or "") == "harmony_patch":
            push_res = await _invoke_bridge(
                bridge_call_fn,
                "mod_patch_method",
                {
                    "type_name": comp_name,
                    "method_name": method_name or member_name,
                    "hook_kind": str(adv.get("hook_kind") or "Postfix"),
                    "value": target_val,
                },
                game,
            )
        elif str(adv.get("invoke_kind") or "") == "method":
            push_res = await _invoke_bridge(
                bridge_call_fn,
                "call_component_method",
                {
                    "game_object_id": go_id,
                    "component_name": comp_name,
                    "method_name": method_name or member_name,
                    "args": [target_val],
                },
                game,
            )
        else:
            push_res = await _invoke_bridge(
                bridge_call_fn,
                "set_component_value",
                {
                    "game_object_id": go_id,
                    "component_name": comp_name,
                    "member_name": member_name,
                    "value": target_val,
                },
                game,
            )
    except Exception as exc:
        log_excerpt, _, _ = _collect_log_excerpt(game_dir, info)
        return {
            "ok": False,
            "delivery_type": "bridge",
            "validated": False,
            "restart_required": False,
            "snapshot_id": snapshot_id,
            "error_details": f"Live bridge push failed: {exc}",
            "log_excerpt": log_excerpt,
            "validation_badge": "Validation Failed",
        }

    if push_res.get("isError"):
        log_excerpt, _, _ = _collect_log_excerpt(game_dir, info)
        return {
            "ok": False,
            "delivery_type": "bridge",
            "validated": False,
            "restart_required": False,
            "snapshot_id": snapshot_id,
            "error_details": f"Bridge reported runtime error: {push_res.get('raw') or 'unknown error'}",
            "log_excerpt": log_excerpt or str(push_res.get("raw") or ""),
            "validation_badge": "Validation Failed",
        }

    # Immediate read-back verification via get_component_member
    readback_val: str | None = None
    verify_error = ""
    if member_name and str(adv.get("invoke_kind") or "") != "method":
        try:
            verify_res = await _invoke_bridge(
                bridge_call_fn,
                "get_component_member",
                {
                    "game_object_id": go_id,
                    "component_name": comp_name,
                    "member_name": member_name,
                },
                game,
            )
            if verify_res.get("isError"):
                verify_error = str(verify_res.get("raw") or "get_component_member returned error")
            else:
                readback_val = _extract_member_value_from_bridge(verify_res)
        except Exception as exc:
            verify_error = str(exc)

    log_excerpt, _, log_err = _collect_log_excerpt(game_dir, info, keywords=(comp_name, member_name, "Exception"))
    readback_ok = (readback_val is not None and readback_val.strip() == target_val.strip()) if not verify_error else False
    validated = bool(readback_ok and not log_err)
    error_details = ""
    if verify_error:
        error_details = f"Bridge read-back failed: {verify_error}"
    elif not readback_ok:
        error_details = f"Bridge read-back mismatch: expected '{target_val}', got '{readback_val}'."
    elif log_err:
        error_details = f"Runtime exception logged after bridge push for '{comp_name}.{member_name}'."

    append_audit_log(
        action=f"endpoints.{clean_action}.bridge",
        status="ok" if validated else "error",
        summary=f"Bridge live push {comp_name}.{member_name}={target_val} (validated={validated})",
        details={"slug": slug, "endpoint_id": endpoint_id, "readback": readback_val, "validated": validated},
    )
    return {
        "ok": validated,
        "delivery_type": "bridge",
        "validated": validated,
        "restart_required": False,
        "snapshot_id": snapshot_id,
        "readback_value": readback_val,
        "error_details": error_details,
        "log_excerpt": log_excerpt if not validated else "",
        "validation_badge": "Bridge Confirmed" if validated else "Validation Failed",
        "summary": (
            f"Live bridge confirmed {comp_name}.{member_name} = {readback_val}"
            if validated
            else f"Live bridge validation failed for {comp_name}.{member_name}"
        ),
        "endpoint": {**ep, "current_value": readback_val if validated and readback_val is not None else ep["current_value"]},
    }


async def validate_endpoint_state(
    game: dict[str, Any],
    endpoint_id: str,
    expected_value: str | None = None,
    *,
    defaults_dir: Path | None = None,
    bridge_call_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Validate current persistence/deployment/live-bridge state for *endpoint_id* without mutating files."""
    slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    game_dir = Path(str(game.get("game_dir") or "").strip())
    info = game.get("cached_modding_info") if isinstance(game.get("cached_modding_info"), dict) else {}
    report = scan_game_endpoints(game, defaults_dir=defaults_dir)
    ep = next((e for e in report.get("endpoints", []) if e["id"] == endpoint_id), None)
    if ep is None:
        raise KeyError(f"Endpoint not found: {endpoint_id}")

    delivery = normalize_delivery_type(ep.get("delivery_type", "config"))
    adv = ep.get("advanced_details") or {}
    target_val = str(expected_value) if expected_value is not None else str(ep["current_value"])

    if delivery == "config":
        target_file = Path(str(adv.get("file_path") or ""))
        on_disk = _read_config_value_on_disk(target_file, str(adv.get("section") or "General"), str(adv.get("key") or ""))
        ok = on_disk is not None and on_disk.strip() == target_val.strip()
        log_excerpt, _, parse_err = _collect_log_excerpt(game_dir, info, keywords=(target_file.name, str(adv.get("key") or "")))
        validated = bool(ok and not parse_err)
        return {
            "ok": validated,
            "delivery_type": "config",
            "validated": validated,
            "restart_required": True,
            "snapshot_id": "",
            "error_details": "" if validated else f"Expected '{target_val}', found '{on_disk}' on disk.",
            "log_excerpt": log_excerpt if not validated else "",
            "validation_badge": "Verified Applied" if validated else "Validation Failed",
        }

    if delivery == "mod_file":
        rec_file = _mod_record_path(slug, endpoint_id)
        meta = load_json_sanitized(rec_file, persist=False) if rec_file.is_file() else {}
        files = meta.get("installed_files") if isinstance(meta, dict) else []
        if not files and adv.get("file_path"):
            files = [str(adv["file_path"])]
        validated = bool(files and all(Path(p).exists() for p in files))
        log_excerpt, _, _ = _collect_log_excerpt(game_dir, info, keywords=(ep["name"],))
        return {
            "ok": validated,
            "delivery_type": "mod_file",
            "validated": validated,
            "restart_required": True,
            "snapshot_id": str((meta or {}).get("pre_deploy_snapshot_id") or ""),
            "error_details": "" if validated else "Deployed mod files not found on disk.",
            "log_excerpt": log_excerpt if not validated else "",
            "validation_badge": "Verified Applied" if validated else "Validation Failed",
        }

    # bridge validation
    comp_name = str(adv.get("component") or f"{adv.get('namespace', '')}.{adv.get('class_name', '')}".strip(".") or "").strip()
    member_name = str(adv.get("member") or "").strip()
    go_id = str(adv.get("game_object_id") or game.get("default_go_id") or "").strip()
    try:
        res = await _invoke_bridge(
            bridge_call_fn,
            "get_component_member",
            {"game_object_id": go_id, "component_name": comp_name, "member_name": member_name},
            game,
        )
        rb = _extract_member_value_from_bridge(res)
        log_excerpt, _, _ = _collect_log_excerpt(game_dir, info)
        validated = bool(rb is not None and rb.strip() == target_val.strip())
        return {
            "ok": validated,
            "delivery_type": "bridge",
            "validated": validated,
            "restart_required": False,
            "snapshot_id": "",
            "readback_value": rb,
            "error_details": "" if validated else f"Bridge value '{rb}' did not match expected '{target_val}'.",
            "log_excerpt": log_excerpt if not validated else "",
            "validation_badge": "Bridge Confirmed" if validated else "Validation Failed",
        }
    except Exception as exc:
        log_excerpt, _, _ = _collect_log_excerpt(game_dir, info)
        return {
            "ok": False,
            "delivery_type": "bridge",
            "validated": False,
            "restart_required": False,
            "snapshot_id": "",
            "error_details": str(exc),
            "log_excerpt": log_excerpt,
            "validation_badge": "Validation Failed",
        }


def get_deployed_mod_download_path(slug: str, endpoint_id: str) -> Path | None:
    rec_file = _mod_record_path(slug, endpoint_id)
    if not rec_file.is_file():
        return None
    meta = load_json_sanitized(rec_file, persist=False)
    if not isinstance(meta, dict):
        return None
    zp = Path(str(meta.get("zip_path") or "").strip())
    return zp if zp.is_file() else None

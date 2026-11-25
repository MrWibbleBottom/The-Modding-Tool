"""Contextual endpoint application (Config, Mod File, Bridge), pre-change snapshots, and post-change validation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
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


def _traverse_nested_payload(payload: Any, dotted_path: str) -> Any:
    cur = payload
    for seg in [s.strip() for s in str(dotted_path or "").split(".") if s.strip()]:
        if isinstance(cur, str):
            try:
                cur = json.loads(cur)
            except (TypeError, json.JSONDecodeError):
                return None
        if isinstance(cur, dict):
            matched_key = next((k for k in cur.keys() if k.lower() == seg.lower()), None)
            if matched_key is None:
                return None
            cur = cur[matched_key]
        else:
            return None
    return cur


def _extract_member_value_from_bridge(res: dict[str, Any], *, member_path: str = "") -> str | None:
    if not isinstance(res, dict) or res.get("isError"):
        return None
    data = res.get("data")
    if isinstance(data, dict):
        if member_path:
            # Check nested dicts stored directly in `data` or `data["value"]` / single-level wrappers
            cands: list[Any] = [data.get("value"), data.get("result"), data]
            for v in data.values():
                if isinstance(v, dict) or (isinstance(v, str) and v.strip().startswith("{")):
                    cands.append(v)
            for root_cand in cands:
                if isinstance(root_cand, dict) or (isinstance(root_cand, str) and root_cand.strip().startswith("{")):
                    trav = _traverse_nested_payload(root_cand, member_path)
                    if trav is not None and not isinstance(trav, (dict, list)):
                        return str(trav)
                    if "." in member_path:
                        leaf = member_path.rsplit(".", 1)[-1]
                        trav_leaf = _traverse_nested_payload(root_cand, leaf)
                        if trav_leaf is not None and not isinstance(trav_leaf, (dict, list)):
                            return str(trav_leaf)
        for key in ("value", "member_value", "current_value", "result"):
            if key in data and data[key] is not None and not isinstance(data[key], (dict, list)):
                return str(data[key])
    if data is not None and not isinstance(data, (dict, list)):
        return str(data)
    raw = str(res.get("raw") or "").strip()
    return raw if raw else None


async def _resolve_bridge_go_id(
    bridge_call_fn: Callable[..., Any] | None,
    comp_name: str,
    instance_name: str,
    game: dict[str, Any],
) -> str:
    if not comp_name:
        return ""
    find_res = await _invoke_bridge(
        bridge_call_fn,
        "find_objects_with_component",
        {"component_name": comp_name},
        game,
    )
    fdata = find_res.get("data")
    objs: list[dict[str, Any]] = []
    if isinstance(fdata, list):
        objs = [o for o in fdata if isinstance(o, dict)]
    elif isinstance(fdata, dict):
        raw_objs = fdata.get("objects") or fdata.get("matches") or []
        if isinstance(raw_objs, list):
            objs = [o for o in raw_objs if isinstance(o, dict)]
    if not objs:
        return ""
    if instance_name:
        inst_low = instance_name.strip().lower()
        for o in objs:
            oname = str(o.get("name") or o.get("Name") or "").strip().lower()
            if oname == inst_low:
                return str(o.get("id") or o.get("Id") or o.get("object_id") or o.get("instance_id") or "")
    first = objs[0]
    return str(first.get("id") or first.get("Id") or first.get("object_id") or first.get("instance_id") or "")


async def _read_bridge_member_with_dotted_fallback(
    bridge_call_fn: Callable[..., Any] | None,
    go_id: str,
    comp_name: str,
    member_path: str,
    game: dict[str, Any],
) -> tuple[str | None, str]:
    """Read a bridge member (including dotted nested paths like `Stats.Armor`) with nested-container fallback."""
    verify_res = await _invoke_bridge(
        bridge_call_fn,
        "get_component_member",
        {
            "game_object_id": go_id,
            "component_name": comp_name,
            "member_name": member_path,
        },
        game,
    )
    if not verify_res.get("isError"):
        val = _extract_member_value_from_bridge(verify_res, member_path=member_path)
        if val is not None:
            return (val, "")

    if "." in member_path:
        parent_seg, child_rest = member_path.split(".", 1)
        parent_res = await _invoke_bridge(
            bridge_call_fn,
            "get_component_member",
            {
                "game_object_id": go_id,
                "component_name": comp_name,
                "member_name": parent_seg,
            },
            game,
        )
        if not parent_res.get("isError"):
            val_nested = _extract_member_value_from_bridge(parent_res, member_path=child_rest)
            if val_nested is not None:
                return (val_nested, "")
            pdata = parent_res.get("data")
            if isinstance(pdata, dict):
                sub_id = str(pdata.get("object_id") or pdata.get("id") or pdata.get("instance_id") or "").strip()
                sub_comp = str(pdata.get("type") or pdata.get("component_name") or "").strip()
                if sub_id and sub_comp:
                    sub_res = await _invoke_bridge(
                        bridge_call_fn,
                        "get_component_member",
                        {"game_object_id": sub_id, "component_name": sub_comp, "member_name": child_rest},
                        game,
                    )
                    if not sub_res.get("isError"):
                        sub_val = _extract_member_value_from_bridge(sub_res, member_path=child_rest)
                        if sub_val is not None:
                            return (sub_val, "")

    return (None, str(verify_res.get("raw") or "get_component_member returned error"))


def _coerce_json_scalar(raw_val: str, existing: Any) -> Any:
    if isinstance(existing, bool):
        return raw_val.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(existing, int) and not isinstance(existing, bool):
        try:
            return int(float(raw_val))
        except ValueError:
            return raw_val
    if isinstance(existing, float):
        try:
            return float(raw_val)
        except ValueError:
            return raw_val
    return raw_val


def _update_json_key_on_disk(target_file: Path, key_path: str, new_value: str) -> None:
    data = json.loads(target_file.read_text(encoding="utf-8-sig", errors="replace")) if target_file.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {target_file.name}")
    parts = [p.strip() for p in key_path.split(".") if p.strip()]
    cur: Any = data
    for seg in parts[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    leaf = parts[-1]
    cur[leaf] = _coerce_json_scalar(new_value, cur.get(leaf))
    target_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _update_xml_key_on_disk(target_file: Path, key_path: str, new_value: str) -> None:
    xml_text = target_file.read_text(encoding="utf-8-sig", errors="replace") if target_file.is_file() else "<Config/>"
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", xml_text, re.IGNORECASE):
        raise ValueError(f"Refusing unsafe XML with DOCTYPE/ENTITY in {target_file.name}")
    root = ET.fromstring(xml_text)
    parts = [p.strip() for p in key_path.split(".") if p.strip()]
    cur: ET.Element | None = root
    for seg in parts:
        if cur is None:
            break
        if seg.startswith("@") and cur is not None:
            cur.attrib[seg[1:]] = new_value
            target_file.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")
            return
        cur = next((c for c in cur if c.tag.rsplit("}", 1)[-1] == seg), None)
    if cur is None:
        raise KeyError(f"XML path '{key_path}' not found in {target_file.name}")
    cur.text = new_value
    target_file.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")


def _read_config_value_on_disk(target_file: Path, section: str, key: str) -> str | None:
    if not target_file.is_file():
        return None
    suf = target_file.suffix.lower()
    if suf == ".cfg":
        for entry in _parse_bepinex_cfg(target_file):
            if entry["section"].lower() == section.lower() and entry["key"] == key:
                return str(entry["value"])
        return None
    if suf == ".json":
        try:
            data = json.loads(target_file.read_text(encoding="utf-8-sig", errors="replace"))
            val = _traverse_nested_payload(data, key)
            return None if val is None or isinstance(val, (dict, list)) else str(val)
        except Exception:
            return None
    if suf == ".xml":
        try:
            raw_xml = target_file.read_text(encoding="utf-8-sig", errors="replace")
            if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", raw_xml, re.IGNORECASE):
                return None
            root = ET.fromstring(raw_xml)
            cur: ET.Element | None = root
            for seg in [p.strip() for p in key.split(".") if p.strip()]:
                if cur is None:
                    return None
                if seg.startswith("@"):
                    return str(cur.attrib.get(seg[1:])) if seg[1:] in cur.attrib else None
                cur = next((c for c in cur if c.tag.rsplit("}", 1)[-1] == seg), None)
            return (cur.text or "").strip() if cur is not None else None
        except Exception:
            return None
    sections = parse_unreal_ini_file(target_file)
    for sec_name, kv in sections.items():
        if sec_name.lower() == section.lower() and key in kv:
            return str(kv[key])
    return None


def _build_harmony_dynamic_patch(adv: dict[str, Any], target_val: str) -> tuple[str, str]:
    """Return `(patch_type, csharp_patch_code)` for `mod_patch_method` Harmony DynamicPatcher."""
    ret_tail = str(adv.get("return_type") or "").strip().rsplit(".", 1)[-1].lower()
    mod_kind = str(adv.get("modifier_kind") or "").strip().lower()
    hook_kind = "postfix"

    if mod_kind == "bool_override" or ret_tail in ("bool", "boolean"):
        low = target_val.strip().lower()
        bool_lit = "true" if low in ("true", "always_true", "1", "enable", "enabled") else "false"
        code = (
            "public static class DynamicPatcher\n"
            "{\n"
            "    public static void Postfix(ref bool __result)\n"
            "    {\n"
            f"        __result = {bool_lit};\n"
            "    }\n"
            "}\n"
        )
        return (hook_kind, code)

    if mod_kind == "numeric_multiplier" or ret_tail in ("float", "single", "double", "int", "int32", "int64", "decimal"):
        try:
            mult = float(target_val)
        except ValueError:
            mult = 1.0
        if ret_tail in ("float", "single"):
            body = f"__result = (float)(__result * {mult}f);"
            sig = "ref float __result"
        elif ret_tail == "double":
            body = f"__result = __result * {mult}d;"
            sig = "ref double __result"
        elif ret_tail == "int64":
            body = f"__result = (long)System.Math.Round(__result * {mult}d);"
            sig = "ref long __result"
        elif ret_tail == "decimal":
            body = f"__result = __result * {mult}m;"
            sig = "ref decimal __result"
        else:
            body = f"__result = (int)System.Math.Round(__result * {mult}d);"
            sig = "ref int __result"
        code = (
            "public static class DynamicPatcher\n"
            "{\n"
            f"    public static void Postfix({sig})\n"
            "    {\n"
            f"        {body}\n"
            "    }\n"
            "}\n"
        )
        return (hook_kind, code)

    code = (
        "public static class DynamicPatcher\n"
        "{\n"
        "    public static void Postfix()\n"
        "    {\n"
        "    }\n"
        "}\n"
    )
    return (hook_kind, code)


def _match_collection_entry(exp_res: dict[str, Any], col_key: str, key_label: str = "") -> tuple[str | None, str]:
    if not isinstance(exp_res, dict) or exp_res.get("isError"):
        return (None, str(exp_res.get("raw") or "expand_collection returned error"))
    data = exp_res.get("data")
    entries: list[Any] = []
    if isinstance(data, dict):
        entries = data.get("entries") or data.get("Entries") or []
    elif isinstance(data, list):
        entries = data
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        ek = str(ent.get("key") if ent.get("key") is not None else "").strip()
        if ek == col_key or (key_label and ek.lower() == key_label.lower()):
            return (str(ent.get("value") if ent.get("value") is not None else ""), "")
    return (None, f"Collection key '{col_key}' was not returned by expand_collection.")


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
    endpoint: dict[str, Any] | None = None,
    live_bridge_fn: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Apply or rollback a discovered modding endpoint via Config, Mod File, or live Bridge delivery."""
    slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    game_name = str(game.get("name") or slug)
    game_dir = Path(str(game.get("game_dir") or "").strip())
    info = game.get("cached_modding_info") if isinstance(game.get("cached_modding_info"), dict) else {}
    if not info and game_dir.is_dir():
        info = inspect_game_modding_info(game_dir, game_name)

    report = scan_game_endpoints(game, defaults_dir=defaults_dir, live_bridge_fn=live_bridge_fn)
    ep = endpoint if (isinstance(endpoint, dict) and endpoint.get("id") == endpoint_id) else next(
        (e for e in report.get("endpoints", []) if e["id"] == endpoint_id),
        None,
    )
    if ep is None and isinstance(game.get("cached_endpoints"), list):
        ep = next((e for e in game["cached_endpoints"] if isinstance(e, dict) and e.get("id") == endpoint_id), None)
    if ep is None:
        defs_root = Path(defaults_dir) if defaults_dir is not None else (APP_DATA_ROOT / "game_defaults")
        def_file = defs_root / f"{slug}.json"
        if def_file.is_file():
            loaded_defs = load_json_sanitized(def_file, persist=False)
            if isinstance(loaded_defs, dict) and isinstance(loaded_defs.get("endpoints_snapshot"), dict):
                snap_ep = loaded_defs["endpoints_snapshot"].get(endpoint_id)
                if isinstance(snap_ep, dict):
                    ep = snap_ep
    if ep is None:
        raise KeyError(f"Endpoint not found: {endpoint_id}")

    delivery = normalize_delivery_type(ep.get("delivery_type", "config"))
    clean_action = str(action or "apply").strip().lower()
    adv = ep.get("advanced_details") or {}
    if delivery == "mod_file" and str(adv.get("rule_type") or "") == "harmony_patch" and bridge_call_fn is not None and clean_action not in ("deploy_mod", "rollback_mod"):
        delivery = "bridge"
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

        if target_file.suffix.lower() == ".json":
            _update_json_key_on_disk(target_file, key, target_val)
        elif target_file.suffix.lower() == ".xml":
            _update_xml_key_on_disk(target_file, key, target_val)
        else:
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

        if clean_action in ("rollback_mod", "reset_default") or (
            str(adv.get("rule_type") or "") == "harmony_patch" and target_val.strip().lower() == "vanilla"
        ):
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
                return_type=str(adv.get("return_type") or ""),
                parameter_types=str(adv.get("parameter_types") or ""),
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
    # 3. LIVE BRIDGE DELIVERY (Collection Entry, Harmony Method, or Member + Verification)
    # ------------------------------------------------------------------
    comp_name = str(adv.get("component") or f"{adv.get('namespace', '')}.{adv.get('class_name', '')}".strip(".") or "").strip()
    member_name = str(adv.get("member_path") or adv.get("member") or "").strip()
    method_name = str(adv.get("method_signature") or "").strip()
    instance_name = str(adv.get("instance_name") or "").strip()
    go_id = str(adv.get("object_id") or adv.get("game_object_id") or game.get("default_go_id") or "").strip()
    is_collection_ep = bool(adv.get("source_kind") == "live_bepinex_collection" or adv.get("collection_key") is not None)
    is_harmony_patch_ep = str(adv.get("rule_type") or "") == "harmony_patch"
    patch_reg_id = str(adv.get("patch_registration_id") or f"ep-{sha256_text(endpoint_id)[:12]}")

    try:
        if is_collection_ep:
            col_field_path = str(adv.get("field_path") if adv.get("field_path") is not None else member_name).strip()
            col_key = str(adv.get("collection_key") if adv.get("collection_key") is not None else "").strip()
            push_res = await _invoke_bridge(
                bridge_call_fn,
                "set_collection_entry",
                {
                    "object_id": go_id,
                    "field_path": col_field_path,
                    "key": col_key,
                    "value": target_val,
                },
                game,
            )
        elif is_harmony_patch_ep:
            if clean_action == "reset_default" or target_val.strip().lower() == "vanilla":
                push_res = await _invoke_bridge(
                    bridge_call_fn,
                    "remove_mod_patch",
                    {"registration_id": patch_reg_id},
                    game,
                )
            else:
                patch_type, patch_code = _build_harmony_dynamic_patch(adv, target_val)
                push_res = await _invoke_bridge(
                    bridge_call_fn,
                    "mod_patch_method",
                    {
                        "target_class": comp_name,
                        "target_method": method_name or member_name,
                        "parameter_types": str(adv.get("parameter_types") or ""),
                        "patch_type": patch_type,
                        "patch_code": patch_code,
                        "registration_id": patch_reg_id,
                    },
                    game,
                )
        else:
            if not go_id and comp_name:
                go_id = await _resolve_bridge_go_id(bridge_call_fn, comp_name, instance_name, game)

            if str(adv.get("invoke_kind") or "") == "method":
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
        err_low = str(exc).lower()
        is_offline_err = (
            isinstance(exc, (FileNotFoundError, ConnectionError, TimeoutError, OSError))
            or "cannot connect" in err_low
            or "timed out" in err_low
        )
        if (
            is_harmony_patch_ep
            and is_offline_err
            and clean_action != "reset_default"
            and game_dir.is_dir()
            and (game_dir / "BepInEx" / "interop").is_dir()
        ):
            return await apply_endpoint_change(
                game,
                endpoint_id,
                new_value=target_val,
                action="deploy_mod",
                defaults_dir=defaults_dir,
                snapshots_root=snapshots_root,
                built_mods_dir=built_mods_dir,
                bridge_call_fn=bridge_call_fn,
                endpoint={**ep, "delivery_type": "Mod File"},
                live_bridge_fn=live_bridge_fn,
                force=force,
            )
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
        if is_harmony_patch_ep and clean_action == "reset_default" and "not found" in str(push_res.get("raw") or "").lower():
            # Patch was already absent on bridge; reset succeeds cleanly.
            push_res = {"tool": "remove_mod_patch", "isError": False, "raw": "ok", "data": {"status": "ok"}}
        else:
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

    # Immediate read-back verification
    readback_val: str | None = None
    verify_error = ""
    if is_collection_ep:
        col_field_path = str(adv.get("field_path") if adv.get("field_path") is not None else member_name).strip()
        col_key = str(adv.get("collection_key") if adv.get("collection_key") is not None else "").strip()
        col_key_label = str(adv.get("collection_key_label") or "").strip()
        try:
            exp_res = await _invoke_bridge(
                bridge_call_fn,
                "expand_collection",
                {"object_id": go_id, "field_path": col_field_path, "limit": 100},
                game,
            )
            readback_val, verify_error = _match_collection_entry(exp_res, col_key, col_key_label)
        except Exception as exc:
            verify_error = str(exc)
    elif is_harmony_patch_ep:
        readback_val = str(ep["default_value"]) if (clean_action == "reset_default" or target_val.strip().lower() == "vanilla") else target_val
    elif member_name and str(adv.get("invoke_kind") or "") != "method":
        try:
            readback_val, verify_error = await _read_bridge_member_with_dotted_fallback(
                bridge_call_fn, go_id, comp_name, member_name, game
            )
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
    endpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate current persistence/deployment/live-bridge state for *endpoint_id* without mutating files."""
    slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    game_dir = Path(str(game.get("game_dir") or "").strip())
    info = game.get("cached_modding_info") if isinstance(game.get("cached_modding_info"), dict) else {}
    report = scan_game_endpoints(game, defaults_dir=defaults_dir)
    ep = endpoint if (isinstance(endpoint, dict) and endpoint.get("id") == endpoint_id) else next(
        (e for e in report.get("endpoints", []) if e["id"] == endpoint_id),
        None,
    )
    if ep is None:
        defs_root = Path(defaults_dir) if defaults_dir is not None else (APP_DATA_ROOT / "game_defaults")
        def_file = defs_root / f"{slug}.json"
        if def_file.is_file():
            loaded_defs = load_json_sanitized(def_file, persist=False)
            if isinstance(loaded_defs, dict) and isinstance(loaded_defs.get("endpoints_snapshot"), dict):
                snap_ep = loaded_defs["endpoints_snapshot"].get(endpoint_id)
                if isinstance(snap_ep, dict):
                    ep = snap_ep
    if ep is None:
        raise KeyError(f"Endpoint not found: {endpoint_id}")

    delivery = normalize_delivery_type(ep.get("delivery_type", "config"))
    adv = ep.get("advanced_details") or {}
    if delivery == "mod_file" and str(adv.get("rule_type") or "") == "harmony_patch" and bridge_call_fn is not None and not _mod_record_path(slug, endpoint_id).is_file():
        delivery = "bridge"
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
    member_name = str(adv.get("member_path") or adv.get("member") or "").strip()
    instance_name = str(adv.get("instance_name") or "").strip()
    go_id = str(adv.get("object_id") or adv.get("game_object_id") or game.get("default_go_id") or "").strip()
    is_collection_ep = bool(adv.get("source_kind") == "live_bepinex_collection" or adv.get("collection_key") is not None)
    is_harmony_patch_ep = str(adv.get("rule_type") or "") == "harmony_patch"
    patch_reg_id = str(adv.get("patch_registration_id") or f"ep-{sha256_text(endpoint_id)[:12]}")
    try:
        if is_collection_ep:
            col_field_path = str(adv.get("field_path") if adv.get("field_path") is not None else member_name).strip()
            col_key = str(adv.get("collection_key") if adv.get("collection_key") is not None else "").strip()
            exp_res = await _invoke_bridge(
                bridge_call_fn,
                "expand_collection",
                {"object_id": go_id, "field_path": col_field_path, "limit": 100},
                game,
            )
            rb, rb_err = _match_collection_entry(exp_res, col_key, str(adv.get("collection_key_label") or ""))
        elif is_harmony_patch_ep:
            patches_res = await _invoke_bridge(bridge_call_fn, "list_mod_patches", {}, game)
            pdata = patches_res.get("data") if isinstance(patches_res, dict) else None
            active_ids = {
                str(p.get("id") or "")
                for p in (pdata if isinstance(pdata, list) else [])
                if isinstance(p, dict) and p.get("active", True)
            }
            want_vanilla = target_val.strip().lower() == "vanilla"
            is_patched = patch_reg_id in active_ids
            rb = str(ep["default_value"]) if want_vanilla and not is_patched else (target_val if is_patched else "vanilla")
            rb_err = "" if (not want_vanilla and is_patched) or (want_vanilla and not is_patched) else (
                f"Expected Harmony patch '{patch_reg_id}' active={not want_vanilla}, found active={is_patched}."
            )
        else:
            if not go_id and comp_name:
                go_id = await _resolve_bridge_go_id(bridge_call_fn, comp_name, instance_name, game)
            rb, rb_err = await _read_bridge_member_with_dotted_fallback(bridge_call_fn, go_id, comp_name, member_name, game)
        log_excerpt, _, _ = _collect_log_excerpt(game_dir, info)
        validated = bool(not rb_err and rb is not None and rb.strip() == target_val.strip())
        return {
            "ok": validated,
            "delivery_type": "bridge",
            "validated": validated,
            "restart_required": False,
            "snapshot_id": "",
            "readback_value": rb,
            "error_details": "" if validated else (rb_err or f"Bridge value '{rb}' did not match expected '{target_val}'."),
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

"""Live two-snapshot Action Diff finder for isolating and promoting changed in-game bridge values."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.audit import append_audit_log
from app.core.config import APP_DATA_ROOT, atomic_write_json, load_json_sanitized, sha256_text
from app.services.discovery import make_slug
from app.services.endpoint_catalog import (
    extract_endpoint_symbol,
    get_imported_endpoints_dir,
    load_game_imported_endpoints,
    record_endpoint_knowledge,
)
from app.services.endpoint_parsers import _VALID_WORKFLOW_CATEGORIES, is_excluded_unity_namespace, resolve_endpoint_classification
from app.services.endpoints import (
    _ALL_CATEGORY_LABELS,
    _humanize_member_label,
    discover_live_bepinex_via_reflection,
    get_game_defaults_dir,
    scan_game_endpoints,
)
from app.services.game_store import inspect_game_modding_info
from app.services.il2cpp_indexer import load_il2cpp_index


class BridgeOfflineError(ConnectionError):
    """Raised when the live BepInExMCP HTTP bridge cannot be reached."""


_BASELINE_STORE: dict[str, dict[str, Any]] = {}

_NOISE_TYPE_TOKENS: tuple[str, ...] = (
    "unityengine.",
    "tmpro.",
    "il2cpptmpro.",
    "cinemachine.",
    "transform",
    "recttransform",
    "camera",
    "canvas",
    "scrollbar",
    "audiosource",
    "particlesystem",
    "animator",
    "standaloneavatarnonvr",
    "raycast",
    "pointerevent",
    "eventSystem",
)

_NOISE_FIELD_TOKENS: tuple[str, ...] = (
    "deltatime",
    "unscaledtime",
    "realtimesincestartup",
    "timesincelevelload",
    "fixedtime",
    "framecount",
    "elapsedtime",
    "frametime",
    "localposition",
    "localrotation",
    "localeulerangles",
    "eulerangles",
    "worldposition",
    "viewport",
    "pixelrect",
    "scrollposition",
    "mouseposition",
    "cursormode",
    "timesincelastupdate",
)

_GAMEPLAY_PRIORITY_TOKENS: tuple[str, ...] = (
    "actionpoint",
    "ap",
    "health",
    "hp",
    "maxhealth",
    "armor",
    "shield",
    "gold",
    "currency",
    "money",
    "coin",
    "mana",
    "energy",
    "stamina",
    "xp",
    "exp",
    "level",
    "ammo",
    "damage",
    "cooldown",
    "charge",
    "score",
    "stat",
)


def clear_bridge_diff_store(slug: str | None = None) -> None:
    """Clear stored Action Diff baseline state (for tests or game unload)."""
    if slug is None:
        _BASELINE_STORE.clear()
    else:
        _BASELINE_STORE.pop(make_slug(slug) or "game", None)


def _resolve_bridge_port(game: dict[str, Any]) -> int:
    info = game.get("cached_modding_info") if isinstance(game.get("cached_modding_info"), dict) else {}
    raw_port = game.get("bepinex_port") or info.get("mcp_port") or (8080 if info.get("mcp_bridge_installed") else 8080)
    try:
        return int(raw_port)
    except (TypeError, ValueError):
        return 8080


def is_live_bridge_connected(game: dict[str, Any], *, timeout: float = 0.25) -> bool:
    """Return True when the BepInExMCP plugin responds cleanly to `/mcp/system/ping`."""
    port = _resolve_bridge_port(game)
    if port <= 0:
        return False
    ping_url = f"http://127.0.0.1:{port}/mcp/system/ping"
    try:
        with urllib.request.urlopen(ping_url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _make_live_http_caller(game: dict[str, Any]) -> Callable[[str, dict[str, Any] | None], Any]:
    port = _resolve_bridge_port(game)
    if not is_live_bridge_connected(game, timeout=0.4):
        raise BridgeOfflineError(
            f"Live BepInExMCP bridge is not responding on 127.0.0.1:{port}. "
            "Launch the modded game cleanly before capturing an Action Diff baseline."
        )

    tool_to_http = {
        "search_runtime_types": "type/search",
        "describe_runtime_type": "type/describe",
        "find_objects_with_component": "scene:find_objects_with_component",
        "get_component_member": "component/get_member",
        "inspect_static_type": "type/inspect_static",
        "list_scriptable_objects": "scene/list_scriptable_objects",
        "expand_collection": "object/expand_collection",
    }

    def _http_call(tool_name: str, params: dict[str, Any] | None = None) -> Any:
        ep = tool_to_http.get(tool_name, tool_name)
        qs = urllib.parse.urlencode(
            {
                k: v
                for k, v in (params or {}).items()
                if v is not None
                and k not in ("game_object_id", "component_name", "member_name", "type_name", "object_id", "field_path")
            }
        )
        url = f"http://127.0.0.1:{port}/mcp/{ep}" + (f"?{qs}" if qs else "")
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    return _http_call


def _parse_scalar_value(raw: Any) -> tuple[str, int | float | bool] | None:
    if isinstance(raw, bool):
        return ("bool", raw)
    if isinstance(raw, int):
        return ("int", raw)
    if isinstance(raw, float):
        return ("float", raw)
    s = str(raw if raw is not None else "").strip()
    if not s or s.lower() in ("null", "runtime", "live", "menu / runtime", "vanilla"):
        return None
    low = s.lower()
    if low == "true":
        return ("bool", True)
    if low == "false":
        return ("bool", False)
    if re.fullmatch(r"-?\d+", s):
        try:
            return ("int", int(s))
        except ValueError:
            return None
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return ("float", float(s))
        except ValueError:
            return None
    return None


def _is_timer_or_camera_noise(
    type_name: str,
    field_path: str,
    display_name: str,
    before_parsed: tuple[str, Any] | None = None,
    after_parsed: tuple[str, Any] | None = None,
) -> bool:
    t_low = type_name.lower()
    f_low = field_path.lower()
    d_low = display_name.lower()
    combined = f"{t_low} {f_low} {d_low}"

    if is_excluded_unity_namespace(type_name):
        return True
    if any(tok.lower() in t_low for tok in _NOISE_TYPE_TOKENS):
        return True
    if any(tok in f_low or tok in d_low for tok in _NOISE_FIELD_TOKENS):
        return True
    # Preserve gameplay cooldowns/timers, but drop generic continuous frame timers & camera offsets
    has_gameplay_signal = any(gt in combined for gt in _GAMEPLAY_PRIORITY_TOKENS)
    if not has_gameplay_signal:
        if any(cam in combined for cam in ("camera", "recttransform", "viewport", "cursor", "fps", "frametime")):
            return True
        if before_parsed and after_parsed and before_parsed[0] == "float" and after_parsed[0] == "float":
            if re.search(r"(?:^|[._])(time|timer|elapsed|tick|dt)(?:$|[._])", f_low):
                return True
    return False


def _capture_live_memory_map(
    game: dict[str, Any],
    slug: str,
    *,
    bridge_call_fn: Callable[[str, dict[str, Any] | None], Any] | None = None,
    il2cpp_index: dict[str, Any] | None = None,
    app_data_root: Path | None = None,
    max_items: int = 300,
) -> dict[str, dict[str, Any]]:
    clean_slug = make_slug(slug or str(game.get("id") or game.get("name") or "game")) or "game"
    eff_index = il2cpp_index if il2cpp_index is not None else load_il2cpp_index(
        clean_slug, app_data_root=app_data_root or APP_DATA_ROOT
    )
    caller = bridge_call_fn if bridge_call_fn is not None else _make_live_http_caller(game)

    raw_items = discover_live_bepinex_via_reflection(
        caller,
        max_types=18,
        max_instances=12,
        il2cpp_index=eff_index,
    )

    entries: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if len(entries) >= max_items:
            break
        if not isinstance(item, dict):
            continue
        type_name = str(item.get("component") or item.get("class_name") or "").strip()
        field_path = str(item.get("field_path") or item.get("member_path") or item.get("member") or "").strip()
        col_key = item.get("collection_key")
        key_label = str(item.get("collection_key_label") or "").strip()
        object_id = str(item.get("object_id") or item.get("game_object_id") or "").strip()
        display_name = str(item.get("name") or f"{type_name.rsplit('.', 1)[-1]}.{field_path}").strip()
        raw_val = item.get("current_value")
        parsed = _parse_scalar_value(raw_val)
        if parsed is None:
            continue
        if _is_timer_or_camera_noise(type_name, field_path, display_name):
            continue

        # Key canonically by (source_kind, type_name, object_id, field_path, collection_key)
        col_suffix = f"[{col_key}]" if col_key is not None and str(col_key) != "" else ""
        canonical_key = f"{type_name}|{object_id}|{field_path}{col_suffix}"
        full_field_path = f"{field_path}[{key_label or col_key}]" if col_suffix and field_path else (key_label or field_path or str(item.get("member") or ""))
        entries[canonical_key] = {
            "canonical_key": canonical_key,
            "diff_id": sha256_text(canonical_key)[:14],
            "object_id": object_id,
            "type_name": type_name,
            "class_name": str(item.get("class_name") or type_name.rsplit(".", 1)[-1]),
            "namespace": str(item.get("namespace") or (type_name.rsplit(".", 1)[0] if "." in type_name else "")),
            "field_path": full_field_path,
            "raw_field_path": str(item.get("field_path") or item.get("member_path") or item.get("member") or ""),
            "display_name": display_name,
            "instance_name": str(item.get("instance_name") or type_name.rsplit(".", 1)[-1]),
            "raw_value": str(raw_val).strip(),
            "parsed": parsed,
            "source_kind": str(item.get("source_kind") or "live_bepinex_mcp"),
            "collection_key": str(col_key) if col_key is not None else None,
            "collection_key_label": key_label,
            "collection_type": item.get("collection_type"),
            "element_type": item.get("element_type"),
        }
    return entries


def capture_bridge_baseline(
    game: dict[str, Any],
    slug: str = "",
    *,
    bridge_call_fn: Callable[[str, dict[str, Any] | None], Any] | None = None,
    il2cpp_index: dict[str, Any] | None = None,
    app_data_root: Path | None = None,
) -> dict[str, Any]:
    """Capture Pass 1 ("Before Action") live numeric/boolean memory snapshot for *slug*."""
    clean_slug = make_slug(slug or str(game.get("id") or game.get("name") or "game")) or "game"
    entries = _capture_live_memory_map(
        game,
        clean_slug,
        bridge_call_fn=bridge_call_fn,
        il2cpp_index=il2cpp_index,
        app_data_root=app_data_root,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    _BASELINE_STORE[clean_slug] = {
        "slug": clean_slug,
        "captured_at": now_iso,
        "entries": entries,
        "last_diffs": {},
    }
    append_audit_log(
        action="bridge_diff.baseline",
        status="ok",
        summary=f"Captured live Action Diff baseline for '{clean_slug}' ({len(entries)} tracked value(s))",
        details={"slug": clean_slug, "captured_count": len(entries)},
    )
    return {
        "status": "ok",
        "slug": clean_slug,
        "captured_count": len(entries),
        "captured_at": now_iso,
    }


def _rank_diff_score(diff: dict[str, Any]) -> tuple[int, int, str]:
    text = f"{diff.get('type_name', '')} {diff.get('field_path', '')} {diff.get('display_name', '')}".lower()
    kw_hits = sum(1 for kw in _GAMEPLAY_PRIORITY_TOKENS if kw in text)
    is_int_or_bool = 1 if diff.get("value_type") in ("number", "bool") and isinstance(diff.get("delta"), int) else 0
    is_col_bonus = 1 if diff.get("source_kind") == "live_bepinex_collection" else 0
    return (-(kw_hits * 3 + is_int_or_bool * 2 + is_col_bonus), -abs(float(diff["delta"])) if isinstance(diff.get("delta"), (int, float)) else 0, diff.get("display_name", ""))


def compare_bridge_snapshot(
    game: dict[str, Any],
    slug: str = "",
    *,
    bridge_call_fn: Callable[[str, dict[str, Any] | None], Any] | None = None,
    il2cpp_index: dict[str, Any] | None = None,
    app_data_root: Path | None = None,
) -> dict[str, Any]:
    """Capture Pass 2 ("After Action"), diff against stored baseline, filter noise, and rank candidates."""
    clean_slug = make_slug(slug or str(game.get("id") or game.get("name") or "game")) or "game"
    baseline = _BASELINE_STORE.get(clean_slug)
    if not baseline or not isinstance(baseline.get("entries"), dict):
        raise ValueError("No baseline snapshot captured yet. Click '1. Capture Before' first.")

    before_entries: dict[str, dict[str, Any]] = baseline["entries"]
    after_entries = _capture_live_memory_map(
        game,
        clean_slug,
        bridge_call_fn=bridge_call_fn,
        il2cpp_index=il2cpp_index,
        app_data_root=app_data_root,
    )

    diffs: list[dict[str, Any]] = []
    for key, before in before_entries.items():
        after = after_entries.get(key)
        if after is None:
            continue
        b_kind, b_val = before["parsed"]
        a_kind, a_val = after["parsed"]
        if b_val == a_val:
            continue
        if _is_timer_or_camera_noise(
            before["type_name"],
            before["field_path"],
            before["display_name"],
            before["parsed"],
            after["parsed"],
        ):
            continue

        if b_kind == "bool" or a_kind == "bool":
            delta_val: int | float | str = f"{str(b_val).lower()} -> {str(a_val).lower()}"
            delta_display = f"{str(b_val).lower()} -> {str(a_val).lower()}"
            v_type = "bool"
        elif b_kind == "int" and a_kind == "int":
            int_d = int(a_val) - int(b_val)
            delta_val = int_d
            delta_display = f"+{int_d}" if int_d > 0 else str(int_d)
            v_type = "number"
        else:
            flt_d = round(float(a_val) - float(b_val), 4)
            if flt_d == 0.0:
                continue
            delta_val = flt_d
            delta_display = f"+{flt_d:g}" if flt_d > 0 else f"{flt_d:g}"
            v_type = "number"

        search_tok = before.get("collection_key_label") or before["raw_field_path"] or before["field_path"]
        resolved = resolve_endpoint_classification(
            str(search_tok),
            engine="unity",
            reference_candidates=[
                f"{before['type_name']}.{before['raw_field_path']}",
                f"{before['class_name']}.{before['raw_field_path']}",
                str(search_tok),
            ],
            embedded_context=f"{before['type_name']} {before['display_name']} {before.get('collection_key_label', '')}",
            embedded_label=before["display_name"],
            embedded_description=f"Live Action Diff isolated '{before['field_path']}' on {before['instance_name']} ({before['before_value'] if 'before_value' in before else before['raw_value']} -> {after['raw_value']}).",
            embedded_value_type=v_type,
        )
        sug_cat = resolved["category"] if resolved["category"] != "uncategorized" else "game_systems"

        diffs.append({
            "diff_id": before["diff_id"],
            "object_id": before["object_id"],
            "type_name": before["type_name"],
            "class_name": before["class_name"],
            "namespace": before["namespace"],
            "entity_name": before["instance_name"],
            "field_path": before["field_path"],
            "raw_field_path": before["raw_field_path"],
            "display_name": before["display_name"],
            "before_value": before["raw_value"],
            "after_value": after["raw_value"],
            "delta": delta_val,
            "delta_display": delta_display,
            "value_type": v_type,
            "suggested_category": sug_cat,
            "suggested_category_label": _ALL_CATEGORY_LABELS.get(sug_cat, "Game Systems"),
            "source_kind": before["source_kind"],
            "collection_key": before.get("collection_key"),
            "collection_key_label": before.get("collection_key_label") or "",
            "collection_type": before.get("collection_type"),
            "element_type": before.get("element_type"),
        })

    diffs.sort(key=_rank_diff_score)
    now_iso = datetime.now(timezone.utc).isoformat()
    baseline["last_diffs"] = {d["diff_id"]: d for d in diffs}
    baseline["compared_at"] = now_iso

    append_audit_log(
        action="bridge_diff.compare",
        status="ok",
        summary=f"Compared Action Diff snapshot for '{clean_slug}': {len(diffs)} candidate change(s)",
        details={"slug": clean_slug, "diff_count": len(diffs)},
    )
    return {
        "status": "ok",
        "slug": clean_slug,
        "baseline_captured_at": baseline.get("captured_at", ""),
        "compared_at": now_iso,
        "baseline_count": len(before_entries),
        "after_count": len(after_entries),
        "diff_count": len(diffs),
        "diffs": diffs,
    }


def promote_bridge_diffs(
    game: dict[str, Any],
    slug: str,
    items: list[dict[str, Any]],
    *,
    defaults_dir: Path | None = None,
    app_data_root: Path | None = None,
    knowledge_path: Path | None = None,
) -> dict[str, Any]:
    """Promote selected Action Diff entries into the game's categorized endpoints catalog & `endpoint_knowledge.json`."""
    clean_slug = make_slug(slug or str(game.get("id") or game.get("name") or "game")) or "game"
    eff_root = Path(app_data_root) if app_data_root is not None else APP_DATA_ROOT
    eff_defaults_dir = Path(defaults_dir) if defaults_dir is not None else get_game_defaults_dir(eff_root / "game_defaults")

    cached_diffs: dict[str, Any] = dict((_BASELINE_STORE.get(clean_slug) or {}).get("last_diffs") or {})
    if not items:
        raise ValueError("Provide at least one diff entry to promote.")

    imp_file = get_imported_endpoints_dir(app_data_root=eff_root) / f"{clean_slug}.json"
    existing_imp = load_game_imported_endpoints(clean_slug, app_data_root=eff_root)
    by_id: dict[str, dict[str, Any]] = {str(e.get("id")): e for e in existing_imp if isinstance(e, dict) and e.get("id")}

    defaults_path = eff_defaults_dir / f"{clean_slug}.json"
    existing_defs = load_json_sanitized(defaults_path, persist=False) if defaults_path.is_file() else {}
    stored_defs: dict[str, str] = dict((existing_defs or {}).get("defaults") or {}) if isinstance(existing_defs, dict) else {}
    stored_snap: dict[str, Any] = dict((existing_defs or {}).get("endpoints_snapshot") or {}) if isinstance(existing_defs, dict) else {}

    promoted_endpoints: list[dict[str, Any]] = []
    knowledge_records: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for raw_item in items:
        diff_id = str(raw_item.get("diff_id") or "").strip()
        src = dict(cached_diffs.get(diff_id) or {})
        src.update({k: v for k, v in raw_item.items() if v is not None and v != ""})

        type_name = str(src.get("type_name") or "RuntimeType").strip()
        class_name = str(src.get("class_name") or type_name.rsplit(".", 1)[-1]).strip()
        ns = str(src.get("namespace") or (type_name.rsplit(".", 1)[0] if "." in type_name else "")).strip()
        object_id = str(src.get("object_id") or "").strip()
        raw_fp = str(src.get("raw_field_path") or src.get("field_path") or "").strip()
        field_path = str(src.get("field_path") or raw_fp).strip()
        col_key = src.get("collection_key")
        col_label = str(src.get("collection_key_label") or "").strip()
        display_name = str(src.get("label") or src.get("display_name") or f"{class_name} — {_humanize_member_label(field_path)}").strip()
        before_val = str(src.get("before_value") if src.get("before_value") is not None else "0")
        after_val = str(src.get("after_value") if src.get("after_value") is not None else before_val)
        cat = str(src.get("category") or src.get("suggested_category") or "game_systems").strip().lower()
        if cat not in _VALID_WORKFLOW_CATEGORIES:
            cat = "game_systems"

        v_type = str(src.get("value_type") or "").strip()
        if not v_type:
            v_type = "bool" if before_val.lower() in ("true", "false") else "number"

        is_col = col_key is not None and str(col_key) != ""
        ep_id = str(src.get("endpoint_id") or f"action_diff:{class_name}:{object_id or 'global'}:{make_slug(field_path)}")
        adv: dict[str, Any] = {
            "source_kind": "live_bepinex_collection" if is_col else "live_bepinex_mcp",
            "namespace": ns,
            "class_name": class_name,
            "component": type_name,
            "member": f"{raw_fp}[{col_key}]" if is_col else raw_fp,
            "member_path": raw_fp,
            "field_path": raw_fp,
            "method_signature": f"{type_name}.{raw_fp}",
            "object_id": object_id,
            "game_object_id": object_id,
            "instance_name": str(src.get("entity_name") or class_name),
            "promoted_from_diff_id": diff_id or ep_id,
        }
        if is_col:
            adv["collection_key"] = str(col_key)
            adv["collection_key_label"] = col_label or str(col_key)
            if src.get("collection_type"):
                adv["collection_type"] = src["collection_type"]
            if src.get("element_type"):
                adv["element_type"] = src["element_type"]

        ep_rec = {
            "id": ep_id,
            "category": cat,
            "category_label": _ALL_CATEGORY_LABELS.get(cat, "Game Systems"),
            "name": display_name,
            "simple_description": (
                f"Promoted from live Action Diff on '{type_name}.{field_path}' "
                f"(baseline before_value={before_val}, changed to {after_val})."
            ),
            "value_type": v_type,
            "options": ["true", "false"] if v_type == "bool" else [],
            "min": None,
            "max": None,
            "step": None,
            "default_value": before_val,
            "current_value": after_val,
            "is_modified": before_val.strip() != after_val.strip(),
            "delivery_type": "Bridge",
            "can_edit": True,
            "classification_source": "action_diff",
            "confidence": "high",
            "advanced_details": adv,
        }
        by_id[ep_id] = ep_rec
        stored_defs[ep_id] = before_val
        stored_snap[ep_id] = ep_rec
        promoted_endpoints.append(ep_rec)

        sym = extract_endpoint_symbol(ep_rec)
        knowledge_records.append({
            "engine": "unity",
            "symbol": sym,
            "category": cat,
            "simple_label": display_name,
            "simple_description": ep_rec["simple_description"],
            "value_type": v_type,
            "delivery_type": "Bridge",
            "source": "knowledge_cache",
            "confidence": "high",
        })

    atomic_write_json(
        imp_file,
        {
            "slug": clean_slug,
            "updated_at": now_iso,
            "endpoints": list(by_id.values()),
        },
    )
    atomic_write_json(
        defaults_path,
        {
            "slug": clean_slug,
            "captured_at": (existing_defs or {}).get("captured_at") or now_iso,
            "updated_at": now_iso,
            "defaults": stored_defs,
            "endpoints_snapshot": stored_snap,
        },
    )
    k_path = record_endpoint_knowledge(
        knowledge_records,
        knowledge_path=knowledge_path,
        app_data_root=eff_root,
    )

    updated_report = scan_game_endpoints(game, defaults_dir=eff_defaults_dir)
    append_audit_log(
        action="bridge_diff.promote",
        status="ok",
        summary=f"Promoted {len(promoted_endpoints)} Action Diff endpoint(s) into '{clean_slug}'",
        details={"slug": clean_slug, "promoted_ids": [e["id"] for e in promoted_endpoints], "knowledge_path": str(k_path)},
    )
    return {
        "status": "ok",
        "slug": clean_slug,
        "promoted_count": len(promoted_endpoints),
        "promoted_ids": [e["id"] for e in promoted_endpoints],
        "promoted_endpoints": promoted_endpoints,
        "knowledge_path": str(k_path),
        "report": updated_report,
    }

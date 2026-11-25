"""Contextual modding endpoint discovery, categorization, and baseline default persistence."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.audit import append_audit_log
from app.core.config import APP_DATA_ROOT, TOOLS_ROOT, atomic_write_json, load_json_sanitized, sha256_text
from app.engines.unreal import parse_unreal_ini_file
from app.services.discovery import make_slug
from app.services.endpoint_catalog import (
    extract_endpoint_symbol,
    load_game_imported_endpoints,
    lookup_endpoint_knowledge,
)
from app.services.endpoint_parsers import (
    is_excluded_unity_namespace,
    load_engine_reference_entries,
    parse_bepinex_cfg as _parse_bepinex_cfg_rich,
    parse_unity_dump_metadata,
    parse_unreal_property_identifier,
    resolve_endpoint_classification,
    scan_unreal_datatable_assets,
    split_identifier_tokens,
)
from app.services.game_snapshots import create_game_snapshot, ensure_within
from app.services.game_store import inspect_game_modding_info
from app.services.il2cpp_indexer import load_il2cpp_index
from app.services.log_tail import read_game_log_tail
from app.services.unreal_ini import resolve_unreal_ini_targets

ENDPOINT_CATEGORIES: tuple[dict[str, str], ...] = (
    {
        "id": "game_systems",
        "label": "Game Systems",
        "description": "Day/night cycles, enemy damage, player damage, max ammo, loot drops, enemy spawns, inventory size.",
    },
    {
        "id": "player_character",
        "label": "Player Character",
        "description": "Hero stats, currency/money, level, and experience progression.",
    },
    {
        "id": "engine_settings",
        "label": "Engine Settings",
        "description": "Draw distance, foliage density, texture/shader cache sizes, LOD, and frame pacing.",
    },
    {
        "id": "cheats",
        "label": "Cheats",
        "description": "Mission progress, achievement unblocking, and revealing hidden map objects.",
    },
    {
        "id": "qol",
        "label": "QoL",
        "description": "UI/FOV/HUD scaling and fixes for broken or awkward engine .ini/.cfg defaults.",
    },
    {
        "id": "error_logs",
        "label": "Error Log Capture",
        "description": "Live game, mod loader, and MCP bridge error diagnostics.",
    },
)

_CATEGORY_LABEL_MAP: dict[str, str] = {c["id"]: c["label"] for c in ENDPOINT_CATEGORIES}
_ALL_CATEGORY_LABELS: dict[str, str] = {**_CATEGORY_LABEL_MAP, "uncategorized": "Uncategorized"}


def get_game_defaults_dir(defaults_dir: Path | None = None) -> Path:
    root = Path(defaults_dir) if defaults_dir is not None else (APP_DATA_ROOT / "game_defaults")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _classify_keyword_category(text: str, *, fallback: str = "uncategorized") -> str:
    res = resolve_endpoint_classification(text)
    if res["confidence"] == "low" and fallback != "uncategorized":
        return fallback
    return str(res["category"])


def _infer_value_type(raw_val: str) -> tuple[str, list[str]]:
    clean = str(raw_val or "").strip()
    low = clean.lower()
    if low in ("true", "false"):
        return ("bool", ["True", "False"] if clean[0:1].isupper() else ["true", "false"])
    if re.fullmatch(r"-?\d+(?:\.\d+)?", clean):
        return ("number", [])
    return ("string", [])


def _make_endpoint(
    *,
    endpoint_id: str,
    category: str,
    name: str,
    simple_description: str,
    current_value: str,
    default_value: str,
    delivery_type: str,
    can_edit: bool,
    advanced_details: dict[str, Any],
    value_type: str | None = None,
    options: list[str] | None = None,
    min_val: float | None = None,
    max_val: float | None = None,
    step_val: float | None = None,
    classification_source: str = "heuristic",
    confidence: str = "medium",
) -> dict[str, Any]:
    cat = category if category in _ALL_CATEGORY_LABELS else "uncategorized"
    inferred_type, inferred_opts = _infer_value_type(current_value or default_value)
    v_type = value_type or inferred_type
    opts = options if options is not None else inferred_opts
    cur_s = str(current_value)
    def_s = str(default_value)
    adv_payload = dict(advanced_details)
    adv_payload.setdefault("classification_source", classification_source)
    adv_payload.setdefault("classification_confidence", confidence)
    return {
        "id": endpoint_id,
        "category": cat,
        "category_label": _ALL_CATEGORY_LABELS[cat],
        "name": name,
        "value_type": v_type,
        "options": opts,
        "min": min_val,
        "max": max_val,
        "step": step_val,
        "default_value": def_s,
        "current_value": cur_s,
        "is_modified": cur_s.strip() != def_s.strip(),
        "can_edit": can_edit,
        "delivery_type": delivery_type,
        "simple_description": simple_description,
        "classification_source": classification_source,
        "confidence": confidence,
        "advanced_details": adv_payload,
    }


def _scan_unreal_endpoints(game_dir: Path, game_name: str, info: dict[str, Any]) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []
    targets = resolve_unreal_ini_targets(game_dir, game_name=game_name)
    ini_files: list[tuple[str, Path]] = []
    for fname in ("GameUserSettings.ini", "Engine.ini"):
        p = targets.get(fname)
        if p and p.is_file():
            ini_files.append((fname, p))
            scal = p.parent / "Scalability.ini"
            if scal.is_file() and ("Scalability.ini", scal) not in ini_files:
                ini_files.append(("Scalability.ini", scal))

    for sub in ("Saved/Config/WindowsNoEditor", "Saved/Config/Windows"):
        for fname in ("GameUserSettings.ini", "Engine.ini", "Scalability.ini", "Game.ini"):
            cand = game_dir / sub / fname
            if cand.is_file() and all(existing[1] != cand for existing in ini_files):
                ini_files.append((fname, cand))

    seen_ids: set[str] = set()
    for fname, ini_path in ini_files:
        sections = parse_unreal_ini_file(ini_path)
        for sec_name, kv_map in sections.items():
            for key, val in kv_map.items():
                ep_id = f"ue_ini:{fname}:{sec_name}:{key}"
                if ep_id in seen_ids:
                    continue
                seen_ids.add(ep_id)
                ue_prop = parse_unreal_property_identifier(key, sec_name)
                resolved = resolve_endpoint_classification(
                    key,
                    engine="unreal",
                    reference_candidates=[key, f"{sec_name}.{key}"],
                    embedded_source=ue_prop["source"] if ue_prop["source"] == "unreal_metadata" else "",
                    embedded_context=f"{fname} {sec_name}",
                    embedded_label=ue_prop["simple_label"],
                    embedded_description=ue_prop["simple_description"],
                    embedded_value_type=ue_prop["value_type"],
                    embedded_options=ue_prop["options"],
                    embedded_default=str(val),
                    embedded_category=ue_prop["category"] if ue_prop["source"] == "unreal_metadata" else None,
                    embedded_confidence=ue_prop["confidence"],
                )
                endpoints.append(
                    _make_endpoint(
                        endpoint_id=ep_id,
                        category=resolved["category"],
                        name=resolved["simple_label"],
                        simple_description=resolved["simple_description"],
                        current_value=str(val),
                        default_value=str(resolved["default_value"] if resolved["default_value"] is not None else val),
                        delivery_type=resolved.get("delivery_type") or "Config",
                        can_edit=True,
                        value_type=resolved.get("value_type"),
                        options=resolved.get("options"),
                        min_val=resolved.get("min"),
                        max_val=resolved.get("max"),
                        step_val=resolved.get("step"),
                        classification_source=resolved["source"],
                        confidence=resolved["confidence"],
                        advanced_details={
                            "source_kind": "unreal_ini",
                            "file_path": str(ini_path),
                            "file_name": fname,
                            "section": sec_name,
                            "key": key,
                        },
                    )
                )

    # Curated Unreal Engine CVars from bundled reference dictionary (Engine.ini targets)
    eng_target = targets.get("Engine.ini")
    if eng_target is not None:
        eng_sections = parse_unreal_ini_file(eng_target) if eng_target.is_file() else {}
        for ref_entry in load_engine_reference_entries():
            if ref_entry.get("engine") != "unreal":
                continue
            cvar = str(ref_entry.get("pattern") or "")
            if not cvar.startswith("r.") or "*" in cvar:
                continue
            section_name = str(ref_entry.get("default_section") or (
                "/Script/Engine.RendererSettings" if ref_entry.get("category") == "qol" else "SystemSettings"
            ))
            ep_id = f"ue_cvar:{cvar}"
            if any(ep["advanced_details"].get("key") == cvar for ep in endpoints):
                continue
            def_val = str(ref_entry.get("default_value") if ref_entry.get("default_value") is not None else "1")
            cur_val = str((eng_sections.get(section_name) or {}).get(cvar) or def_val)
            endpoints.append(
                _make_endpoint(
                    endpoint_id=ep_id,
                    category=str(ref_entry.get("category") or "engine_settings"),
                    name=f"{ref_entry['simple_label']} ({cvar})",
                    simple_description=str(ref_entry["simple_description"]),
                    current_value=cur_val,
                    default_value=def_val,
                    delivery_type=str(ref_entry.get("delivery_type") or "Config"),
                    can_edit=True,
                    value_type=ref_entry.get("value_type"),
                    options=ref_entry.get("options"),
                    min_val=ref_entry.get("safe_min"),
                    max_val=ref_entry.get("safe_max"),
                    step_val=ref_entry.get("step"),
                    classification_source="reference_dict",
                    confidence="high",
                    advanced_details={
                        "source_kind": "unreal_cvar",
                        "file_path": str(eng_target),
                        "file_name": "Engine.ini",
                        "section": section_name,
                        "key": cvar,
                    },
                )
            )

    # Unreal DataTable assets (DT_Weapons, DT_EnemyStats, DT_LootDrops, DT_PlayerStats, etc.)
    for dt_asset in scan_unreal_datatable_assets(game_dir):
        dt_stem = dt_asset["asset_stem"]
        endpoints.append(
            _make_endpoint(
                endpoint_id=f"ue_datatable:{dt_stem}",
                category=dt_asset["category"],
                name=dt_asset["simple_label"],
                simple_description=dt_asset["simple_description"],
                current_value="Detected",
                default_value="Detected",
                delivery_type="Mod File",
                can_edit=False,
                value_type="string",
                classification_source=dt_asset["source"],
                confidence=dt_asset["confidence"],
                advanced_details={
                    "source_kind": "unreal_datatable",
                    "file_path": dt_asset["file_path"],
                    "file_name": dt_asset["file_name"],
                    "asset_stem": dt_stem,
                },
            )
        )

    # UE4SS Lua hooks & mods
    for lua_file in list(game_dir.glob("*/Binaries/Win64/Mods/*/Scripts/*.lua")) + list(game_dir.glob("*/Binaries/Win64/ue4ss/Mods/*/Scripts/*.lua")):
        if not lua_file.is_file():
            continue
        mod_name = lua_file.parent.parent.name
        txt = lua_file.read_text(encoding="utf-8", errors="ignore")
        hooks = re.findall(r'RegisterHook\(\s*["\']([^"\']+)["\']', txt)
        enabled = (lua_file.parent.parent / "enabled.txt").is_file()
        for h_idx, hook_sig in enumerate(hooks or [f"{mod_name}:main.lua"], start=1):
            resolved_hook = resolve_endpoint_classification(
                hook_sig.split(":")[-1],
                engine="unreal",
                reference_candidates=[hook_sig, mod_name],
                embedded_context=f"{mod_name} {hook_sig}",
                embedded_label=f"UE4SS Hook: {mod_name} ({hook_sig.split(':')[-1]})",
                embedded_description=f"Runtime Lua hook detected in UE4SS mod '{mod_name}'.",
            )
            ep_id = f"ue4ss_hook:{mod_name}:{h_idx}"
            endpoints.append(
                _make_endpoint(
                    endpoint_id=ep_id,
                    category=resolved_hook["category"],
                    name=resolved_hook["simple_label"],
                    simple_description=resolved_hook["simple_description"],
                    current_value="Enabled" if enabled else "Disabled",
                    default_value="Disabled",
                    delivery_type="Mod File",
                    can_edit=True,
                    value_type="select",
                    options=["Enabled", "Disabled"],
                    classification_source=resolved_hook["source"],
                    confidence=resolved_hook["confidence"],
                    advanced_details={
                        "source_kind": "ue4ss_lua_hook",
                        "file_path": str(lua_file),
                        "mod_name": mod_name,
                        "method_signature": hook_sig,
                    },
                )
            )

    return endpoints


_DEFAULT_APP_DATA_ROOT: Path = APP_DATA_ROOT
_DEFAULT_TOOLS_ROOT: Path = TOOLS_ROOT

_EXCLUDED_BOILERPLATE_CFG_FILES: frozenset[str] = frozenset({
    "bepinex.cfg",
    "bepinexmcp.il2cpp.cfg",
})
_EXCLUDED_BOILERPLATE_CFG_SECTIONS: frozenset[str] = frozenset({
    "chainloader",
    "preloader",
    "logging.console",
    "logging.disk",
    "harmony.logger",
    "caching",
    "bepinexmcp",
})
_UNITY_GAMEPLAY_TYPE_KEYWORDS: tuple[str, ...] = (
    "Hero",
    "Character",
    "Player",
    "Party",
    "Unit",
    "Champion",
    "Stats",
    "Health",
    "Armor",
    "Shield",
    "Damage",
    "ActionPoint",
    "Mana",
    "Energy",
    "Gold",
    "Currency",
    "Card",
    "Hand",
    "Loot",
    "Enemy",
    "Quest",
)
_NESTED_CONTAINER_KEYWORDS: tuple[str, ...] = (
    "stats",
    "state",
    "data",
    "attributes",
    "sheet",
    "resources",
    "inventory",
    "hand",
    "deck",
    "party",
    "combat",
)
_INSTANCE_NAME_FIELDS: tuple[str, ...] = ("HeroName", "CharacterName", "DisplayName", "UnitType")
_STATIC_ROOT_PRIORITY_TOKENS: tuple[str, ...] = ("context", "manager", "party", "player", "game", "combat")
_SCRIPTABLE_TEMPLATE_KEYWORDS: tuple[str, ...] = (
    "hero",
    "character",
    "piece",
    "card",
    "ability",
    "weapon",
    "enemy",
    "item",
    "stats",
)
_PATCHABLE_HIGH_SIGNAL_KEYWORDS: tuple[str, ...] = (
    "damage",
    "health",
    "hp",
    "actionpoint",
    "cost",
    "cooldown",
    "spawn",
    "loot",
    "gold",
    "currency",
    "xp",
    "armor",
    "shield",
    "mana",
    "energy",
    "crit",
    "heal",
    "range",
    "move",
)
_EXCLUDED_DATA_DIR_NAMES: frozenset[str] = frozenset({
    "aa",
    "addressables",
    "analytics",
    "unity",
    "il2cpp",
    "crashes",
    "cache",
    "shader",
    "shaders",
    "mono",
})
_EXCLUDED_DATA_FILE_PREFIXES: tuple[str, ...] = (
    "catalog",
    "settings",
    "unity",
    "runtime",
    "manifest",
    "prefs",
    "output_log",
)
_EXCLUDED_DATA_EXACT_NAMES: frozenset[str] = frozenset({
    "link.xml",
    "boot.config",
    "player.log",
    "player-prev.log",
    "steam_appid.txt",
})


def _parse_bepinex_cfg(cfg_path: Path) -> list[dict[str, Any]]:
    return _parse_bepinex_cfg_rich(cfg_path)


def _is_primitive_member_type(type_name: str) -> bool:
    clean = str(type_name or "").strip().lower().split(".")[-1]
    return clean in ("int", "int32", "int64", "uint", "uint32", "float", "single", "double", "bool", "boolean", "byte", "sbyte", "string", "decimal")


def _is_numeric_or_bool_type(type_name: str) -> bool:
    clean = str(type_name or "").strip().lower().split(".")[-1]
    return clean in ("int", "int32", "int64", "uint", "uint32", "float", "single", "double", "bool", "boolean", "byte", "sbyte", "decimal")


def _is_collection_type(type_name: str) -> bool:
    low = str(type_name or "").lower()
    return (
        "dictionary<" in low
        or "dictionary`" in low
        or "list<" in low
        or "list`" in low
        or "ilist" in low
        or "idictionary" in low
        or low.endswith("[]")
    )


def _build_enum_reverse_maps(
    il2cpp_index: dict[str, Any] | None,
) -> tuple[dict[str, dict[int, str]], dict[str, dict[int, str]]]:
    full_map: dict[str, dict[int, str]] = {}
    short_map: dict[str, dict[int, str]] = {}
    if not isinstance(il2cpp_index, dict):
        return (full_map, short_map)
    enums = il2cpp_index.get("enums")
    if not isinstance(enums, dict):
        return (full_map, short_map)
    for enum_full, members in enums.items():
        if not isinstance(members, dict):
            continue
        rev: dict[int, str] = {}
        for m_name, m_val in members.items():
            try:
                rev[int(m_val)] = str(m_name)
            except (TypeError, ValueError):
                continue
        if rev:
            full_map[str(enum_full)] = rev
            short_nm = str(enum_full).rsplit(".", 1)[-1]
            short_map.setdefault(short_nm, rev)
    return (full_map, short_map)


def _decode_collection_key(
    raw_key: Any,
    collection_type: str,
    enum_maps: tuple[dict[str, dict[int, str]], dict[str, dict[int, str]]],
) -> tuple[str, str]:
    """Return `(readable_label, raw_key_str)` by decoding integer dictionary keys via `il2cpp_index` enums."""
    raw_str = str(raw_key if raw_key is not None else "").strip()
    if not raw_str or not re.fullmatch(r"-?\d+", raw_str):
        return (raw_str, raw_str)

    int_key = int(raw_str)
    full_map, short_map = enum_maps
    key_type_cand = ""
    m_gen = re.search(r"<([^,>]+)", str(collection_type or ""))
    if m_gen:
        key_type_cand = m_gen.group(1).strip()
    else:
        m_br = re.search(r"\[\[?([^,\]]+)", str(collection_type or ""))
        if m_br:
            key_type_cand = m_br.group(1).strip()

    rev: dict[int, str] | None = None
    if key_type_cand:
        rev = full_map.get(key_type_cand) or short_map.get(key_type_cand.rsplit(".", 1)[-1])
    if rev and int_key in rev:
        return (rev[int_key], raw_str)
    return (raw_str, raw_str)


def _categorize_scriptable_or_gameplay(text: str) -> str:
    low = text.lower()
    player_toks = ("hero", "character", "player", "piece", "party", "avatar", "champion", "unit", "xp", "level", "gold", "currency", "health", "hp", "mana", "stamina")
    if any(t in low for t in player_toks):
        return "player_character"
    return "game_systems"


def _is_nested_container(member_name: str, member_type: str) -> bool:
    if _is_primitive_member_type(member_type) or is_excluded_unity_namespace(member_type):
        return False
    combined = f"{member_name} {member_type}".lower()
    return any(kw in combined for kw in _NESTED_CONTAINER_KEYWORDS)


def _humanize_member_label(member_path: str) -> str:
    if "." in member_path:
        # Preserve readable container prefix while spacing leaf tokens (e.g. `Stats.Armor`, `State.ActionPoints`)
        parts = member_path.split(".")
        formatted_tail = " ".join(
            t.upper() if t in ("ap", "hp", "xp", "ui", "hud", "fov") else t.capitalize()
            for t in split_identifier_tokens(parts[-1])
        ) or parts[-1]
        return ".".join(parts[:-1] + [formatted_tail])
    toks = split_identifier_tokens(member_path)
    return " ".join(t.upper() if t in ("ap", "hp", "xp", "ui", "hud", "fov") else t.capitalize() for t in toks) or member_path


def _extract_bridge_scalar(raw_val: Any) -> str | None:
    if raw_val is None:
        return None
    if isinstance(raw_val, dict):
        for k in ("value", "Value", "member_value", "current_value", "result"):
            if k in raw_val and raw_val[k] is not None and not isinstance(raw_val[k], (dict, list)):
                return str(raw_val[k])
        return None
    if not isinstance(raw_val, (list, dict)):
        return str(raw_val)
    return None


def discover_live_bepinex_via_reflection(
    call_fn: Callable[[str, dict[str, Any] | None], Any],
    *,
    max_types: int = 24,
    max_instances: int = 20,
    il2cpp_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Query live BepInExMCP reflection, static singletons, ScriptableObjects, and collections."""
    discovered_types: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    enum_maps = _build_enum_reverse_maps(il2cpp_index)

    for kw in _UNITY_GAMEPLAY_TYPE_KEYWORDS:
        if len(discovered_types) >= max_types:
            break
        try:
            raw_search = call_fn("search_runtime_types", {"query": kw, "offset": 0, "limit": 100})
        except Exception:
            continue
        type_list = raw_search if isinstance(raw_search, list) else (raw_search.get("types") or raw_search.get("results") or [] if isinstance(raw_search, dict) else [])
        for t_item in type_list:
            if not isinstance(t_item, dict):
                continue
            full_nm = str(t_item.get("fullName") or t_item.get("FullName") or t_item.get("full_name") or t_item.get("name") or t_item.get("Name") or "").strip()
            if not full_nm or full_nm in seen_types or is_excluded_unity_namespace(full_nm):
                continue
            seen_types.add(full_nm)
            discovered_types.append(t_item)
            if len(discovered_types) >= max_types:
                break

    # Prioritize MonoBehaviour / Component types so live object lookup succeeds first
    discovered_types.sort(key=lambda t: bool(t.get("isComponent") or t.get("IsComponent") or t.get("is_component")), reverse=True)

    type_desc_cache: dict[str, dict[str, Any]] = {}

    def _get_type_desc(tname: str) -> dict[str, Any] | None:
        if tname in type_desc_cache:
            return type_desc_cache[tname]
        try:
            desc = call_fn("describe_runtime_type", {"typeName": tname, "offset": 0, "limit": 200})
        except Exception:
            desc = None
        if isinstance(desc, dict):
            type_desc_cache[tname] = desc
            return desc
        return None

    bridge_items: list[dict[str, Any]] = []
    seen_item_ids: set[str] = set()

    for t_item in discovered_types:
        full_nm = str(t_item.get("fullName") or t_item.get("FullName") or t_item.get("full_name") or t_item.get("name") or "").strip()
        short_nm = str(t_item.get("name") or t_item.get("Name") or full_nm.rsplit(".", 1)[-1]).strip()
        ns_nm = full_nm.rsplit(".", 1)[0] if "." in full_nm else ""
        desc = _get_type_desc(full_nm)
        if not desc:
            continue

        raw_members = desc.get("members") or desc.get("Members") or []
        inspectable_members: list[dict[str, Any]] = []
        name_member_candidates: list[str] = []

        for mem in raw_members:
            if not isinstance(mem, dict):
                continue
            if mem.get("isStatic") or mem.get("IsStatic") or mem.get("is_static"):
                continue
            m_name = str(mem.get("name") or mem.get("Name") or "").strip()
            m_type = str(mem.get("type") or mem.get("Type") or "").strip()
            readable = mem.get("readable", mem.get("Readable", True))
            if not m_name or not readable:
                continue

            if m_name in _INSTANCE_NAME_FIELDS or any(nf.lower() == m_name.lower() for nf in _INSTANCE_NAME_FIELDS):
                name_member_candidates.append(m_name)

            if _is_primitive_member_type(m_type):
                inspectable_members.append({
                    "member_path": m_name,
                    "member_type": m_type,
                })
            elif _is_nested_container(m_name, m_type):
                # Level-2 traversal into container structs/classes (Stats, State, Data, Inventory, etc.)
                sub_desc = _get_type_desc(m_type)
                if sub_desc:
                    for sub_mem in (sub_desc.get("members") or sub_desc.get("Members") or []):
                        if not isinstance(sub_mem, dict):
                            continue
                        if sub_mem.get("isStatic") or sub_mem.get("IsStatic") or sub_mem.get("is_static"):
                            continue
                        sm_name = str(sub_mem.get("name") or sub_mem.get("Name") or "").strip()
                        sm_type = str(sub_mem.get("type") or sub_mem.get("Type") or "").strip()
                        sm_readable = sub_mem.get("readable", sub_mem.get("Readable", True))
                        if not sm_name or not sm_readable or not _is_primitive_member_type(sm_type):
                            continue
                        inspectable_members.append({
                            "member_path": f"{m_name}.{sm_name}",
                            "member_type": sm_type,
                        })

        if not inspectable_members:
            continue

        matched_objects: list[dict[str, Any]] = []
        try:
            raw_objs = call_fn("find_objects_with_component", {"componentName": full_nm})
            if isinstance(raw_objs, list):
                matched_objects = [o for o in raw_objs if isinstance(o, dict)][:max_instances]
            elif isinstance(raw_objs, dict):
                arr = raw_objs.get("objects") or raw_objs.get("matches") or []
                if isinstance(arr, list):
                    matched_objects = [o for o in arr if isinstance(o, dict)][:max_instances]
        except Exception:
            matched_objects = []

        if matched_objects:
            for obj in matched_objects:
                obj_id = str(obj.get("id") or obj.get("Id") or obj.get("object_id") or obj.get("instance_id") or "").strip()
                raw_go_name = re.sub(r"\(Clone\)$", "", str(obj.get("name") or obj.get("Name") or "").strip(), flags=re.IGNORECASE).strip()

                inst_name = ""
                for nf in name_member_candidates:
                    try:
                        nv = _extract_bridge_scalar(call_fn(
                            "get_component_member",
                            {"id": obj_id, "game_object_id": obj_id, "componentName": full_nm, "component_name": full_nm, "memberName": nf, "member_name": nf},
                        ))
                        if nv and nv.strip():
                            inst_name = nv.strip()
                            break
                    except Exception:
                        continue
                if not inst_name:
                    inst_name = raw_go_name or (f"Instance {obj_id}" if obj_id else short_nm)

                for minfo in inspectable_members:
                    m_path = minfo["member_path"]
                    m_type = minfo["member_type"]
                    ep_id = f"live_mcp:{short_nm}:{obj_id}:{m_path}" if obj_id else f"live_mcp:{short_nm}:{m_path}"
                    if ep_id in seen_item_ids:
                        continue
                    seen_item_ids.add(ep_id)

                    live_val: str | None = None
                    try:
                        live_val = _extract_bridge_scalar(call_fn(
                            "get_component_member",
                            {"id": obj_id, "game_object_id": obj_id, "componentName": full_nm, "component_name": full_nm, "memberName": m_path, "member_name": m_path},
                        ))
                    except Exception:
                        live_val = None

                    lbl = _humanize_member_label(m_path)
                    display_title = f"{inst_name} — {lbl}" if inst_name else f"{short_nm}.{m_path}"
                    bridge_items.append({
                        "id": ep_id,
                        "name": display_title,
                        "namespace": ns_nm,
                        "class_name": short_nm,
                        "component": full_nm,
                        "member": m_path,
                        "member_path": m_path,
                        "signature": f"{full_nm}.{m_path}".lstrip("."),
                        "object_id": obj_id,
                        "game_object_id": obj_id,
                        "instance_name": inst_name,
                        "current_value": live_val if live_val is not None else "Live",
                        "default_value": live_val if live_val is not None else "Live",
                        "value_type": "bool" if "bool" in m_type.lower() else ("number" if _is_primitive_member_type(m_type) and "string" not in m_type.lower() else "string"),
                        "description": f"Live runtime field '{m_path}' ({m_type}) on '{inst_name}' ({full_nm}).",
                    })
        else:
            # Menu / static class-level reflection when no active scene instances exist yet
            for minfo in inspectable_members:
                m_path = minfo["member_path"]
                m_type = minfo["member_type"]
                ep_id = f"live_mcp:{short_nm}:{m_path}"
                if ep_id in seen_item_ids:
                    continue
                seen_item_ids.add(ep_id)
                bridge_items.append({
                    "id": ep_id,
                    "name": f"{short_nm}.{m_path}",
                    "namespace": ns_nm,
                    "class_name": short_nm,
                    "component": full_nm,
                    "member": m_path,
                    "member_path": m_path,
                    "signature": f"{full_nm}.{m_path}".lstrip("."),
                    "object_id": "",
                    "game_object_id": "",
                    "instance_name": "",
                    "current_value": "Menu / Runtime",
                    "default_value": "Menu / Runtime",
                    "value_type": "bool" if "bool" in m_type.lower() else ("number" if _is_primitive_member_type(m_type) and "string" not in m_type.lower() else "string"),
                    "description": f"Reflected IL2CPP field '{m_path}' ({m_type}) on {full_nm}.",
                })

    def _emit_collection_entries(
        *,
        owner_object_id: str,
        owner_label: str,
        owner_ns: str,
        owner_cls: str,
        owner_full: str,
        field_path: str,
        col_payload: Any,
        depth: int = 1,
    ) -> None:
        if not isinstance(col_payload, dict):
            return
        col_type = str(col_payload.get("collection_type") or col_payload.get("collectionType") or "")
        entries = col_payload.get("entries") or []
        if not isinstance(entries, list):
            return
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            raw_key = str(ent.get("key") if ent.get("key") is not None else "")
            elem_val = str(ent.get("value") if ent.get("value") is not None else "")
            elem_type = str(ent.get("element_type") or ent.get("elementType") or "")
            elem_obj_id = str(ent.get("object_id") or ent.get("objectId") or "").strip()
            key_label, raw_key_str = _decode_collection_key(raw_key, col_type, enum_maps)

            # Depth-2 walk into non-primitive collection elements (e.g. Party -> Piece -> Stats)
            if elem_obj_id and depth < 2 and elem_type and not is_excluded_unity_namespace(elem_type):
                sub_desc = _get_type_desc(elem_type)
                if sub_desc:
                    for sm in (sub_desc.get("members") or sub_desc.get("Members") or []):
                        if not isinstance(sm, dict) or sm.get("isStatic") or sm.get("IsStatic"):
                            continue
                        sm_name = str(sm.get("name") or sm.get("Name") or "").strip()
                        sm_type = str(sm.get("type") or sm.get("Type") or "").strip()
                        if sm_name and (_is_collection_type(sm_type) or _is_nested_container(sm_name, sm_type)):
                            try:
                                sub_exp = call_fn("expand_collection", {"object_id": elem_obj_id, "objectId": elem_obj_id, "field_path": sm_name, "fieldPath": sm_name, "limit": 50})
                                _emit_collection_entries(
                                    owner_object_id=elem_obj_id,
                                    owner_label=f"{owner_label} — {key_label}",
                                    owner_ns=owner_ns,
                                    owner_cls=owner_cls,
                                    owner_full=elem_type,
                                    field_path=sm_name,
                                    col_payload=sub_exp,
                                    depth=depth + 1,
                                )
                            except Exception:
                                continue

            if elem_obj_id and not _is_primitive_member_type(elem_type):
                continue

            ep_id = f"live_col:{owner_cls}:{owner_object_id}:{field_path}:{raw_key_str}"
            if ep_id in seen_item_ids:
                continue
            seen_item_ids.add(ep_id)
            pretty_field = _humanize_member_label(field_path) if field_path else ""
            item_title = f"{owner_label} — {pretty_field} [{key_label}]" if pretty_field else f"{owner_label} — {key_label}"
            if pretty_field and key_label and pretty_field.lower() == key_label.lower():
                item_title = f"{owner_label} — {key_label}"
            bridge_items.append({
                "id": ep_id,
                "name": item_title,
                "source_kind": "live_bepinex_collection",
                "namespace": owner_ns,
                "class_name": owner_cls,
                "component": owner_full,
                "member": f"{field_path}[{raw_key_str}]" if field_path else raw_key_str,
                "member_path": field_path,
                "field_path": field_path,
                "collection_key": raw_key_str,
                "collection_key_label": key_label,
                "collection_type": col_type,
                "element_type": elem_type,
                "signature": f"{owner_full}.{field_path}[{key_label}]".lstrip("."),
                "object_id": owner_object_id,
                "game_object_id": owner_object_id,
                "instance_name": owner_label,
                "current_value": elem_val,
                "default_value": elem_val,
                "value_type": "bool" if "bool" in elem_type.lower() else ("number" if _is_numeric_or_bool_type(elem_type) else "string"),
                "description": f"Live collection entry '{key_label}' (key={raw_key_str}) in '{owner_full}.{field_path or 'items'}' ({elem_type or 'scalar'}).",
            })

    # Source 1: Indexed static singletons & depth 1-2 collections (`inspect_static_type` + `expand_collection`)
    static_roots = il2cpp_index.get("static_roots") if isinstance(il2cpp_index, dict) else None
    if isinstance(static_roots, dict) and static_roots:
        def _static_score(rname: str) -> tuple[int, str]:
            low = rname.lower()
            hits = sum(1 for tok in _STATIC_ROOT_PRIORITY_TOKENS if tok in low)
            return (-hits, low)

        for root_full in sorted(static_roots.keys(), key=_static_score)[:16]:
            if is_excluded_unity_namespace(root_full):
                continue
            try:
                static_info = call_fn("inspect_static_type", {"type_name": root_full, "typeName": root_full})
            except Exception:
                continue
            if not isinstance(static_info, dict):
                continue
            root_short = root_full.rsplit(".", 1)[-1]
            root_ns = root_full.rsplit(".", 1)[0] if "." in root_full else ""
            for sm in (static_info.get("static_members") or static_info.get("staticMembers") or []):
                if not isinstance(sm, dict):
                    continue
                sm_name = str(sm.get("name") or "").strip()
                sm_type = str(sm.get("member_type") or sm.get("memberType") or "").strip()
                sm_val = str(sm.get("value") if sm.get("value") is not None else "")
                sm_obj_id = str(sm.get("object_id") or sm.get("objectId") or "").strip()
                if not sm_name or sm_val == "null" or is_excluded_unity_namespace(sm_type):
                    continue

                if sm_obj_id:
                    if _is_collection_type(sm_type):
                        try:
                            exp = call_fn("expand_collection", {"object_id": sm_obj_id, "objectId": sm_obj_id, "field_path": "", "fieldPath": "", "limit": 50})
                            _emit_collection_entries(
                                owner_object_id=sm_obj_id,
                                owner_label=f"{root_short}.{sm_name}",
                                owner_ns=root_ns,
                                owner_cls=root_short,
                                owner_full=root_full,
                                field_path="",
                                col_payload=exp,
                                depth=1,
                            )
                        except Exception:
                            pass
                    else:
                        s_desc = _get_type_desc(sm_type)
                        if s_desc:
                            for sub in (s_desc.get("members") or s_desc.get("Members") or []):
                                if not isinstance(sub, dict) or sub.get("isStatic") or sub.get("IsStatic"):
                                    continue
                                sub_name = str(sub.get("name") or sub.get("Name") or "").strip()
                                sub_type = str(sub.get("type") or sub.get("Type") or "").strip()
                                if not sub_name:
                                    continue
                                if _is_collection_type(sub_type) or _is_nested_container(sub_name, sub_type):
                                    try:
                                        exp = call_fn("expand_collection", {"object_id": sm_obj_id, "objectId": sm_obj_id, "field_path": sub_name, "fieldPath": sub_name, "limit": 50})
                                        _emit_collection_entries(
                                            owner_object_id=sm_obj_id,
                                            owner_label=root_short,
                                            owner_ns=root_ns,
                                            owner_cls=root_short,
                                            owner_full=sm_type,
                                            field_path=sub_name,
                                            col_payload=exp,
                                            depth=1,
                                        )
                                    except Exception:
                                        continue
                elif _is_numeric_or_bool_type(sm_type) and sm_val:
                    ep_id = f"live_static:{root_short}:{sm_name}"
                    if ep_id not in seen_item_ids:
                        seen_item_ids.add(ep_id)
                        bridge_items.append({
                            "id": ep_id,
                            "name": f"{root_short} — {_humanize_member_label(sm_name)}",
                            "source_kind": "live_bepinex_static",
                            "namespace": root_ns,
                            "class_name": root_short,
                            "component": root_full,
                            "member": sm_name,
                            "member_path": sm_name,
                            "signature": f"{root_full}.{sm_name}".lstrip("."),
                            "object_id": "",
                            "game_object_id": "",
                            "instance_name": root_short,
                            "current_value": sm_val,
                            "default_value": sm_val,
                            "value_type": "bool" if "bool" in sm_type.lower() else "number",
                            "description": f"Static singleton field '{sm_name}' ({sm_type}) on {root_full}.",
                        })

    # Source 2: Loaded ScriptableObject gameplay templates (`list_scriptable_objects`)
    try:
        so_list = call_fn("list_scriptable_objects", {"filter": "", "limit": 100})
    except Exception:
        so_list = []
    if isinstance(so_list, list):
        idx_classes = il2cpp_index.get("classes") if isinstance(il2cpp_index, dict) and isinstance(il2cpp_index.get("classes"), dict) else {}
        matched_sos = [
            so for so in so_list
            if isinstance(so, dict)
            and any(kw in f"{so.get('name', '')} {so.get('type_name', '')}".lower() for kw in _SCRIPTABLE_TEMPLATE_KEYWORDS)
            and not is_excluded_unity_namespace(str(so.get("type_name") or ""))
        ][:25]
        for so in matched_sos:
            so_id = str(so.get("object_id") or "").strip()
            so_name = str(so.get("name") or "").strip()
            so_type = str(so.get("type_name") or "").strip()
            if not so_type:
                continue
            so_short = so_type.rsplit(".", 1)[-1]
            so_ns = so_type.rsplit(".", 1)[0] if "." in so_type else ""
            so_label = so_name or so_short
            so_cat = _categorize_scriptable_or_gameplay(f"{so_label} {so_type}")

            so_desc = _get_type_desc(so_type)
            idx_meta = idx_classes.get(so_type) or next((v for k, v in idx_classes.items() if k.rsplit(".", 1)[-1] == so_short), {})
            idx_field_map = {str(f.get("name")): f for f in (idx_meta.get("fields") or []) if isinstance(f, dict) and f.get("name")} if isinstance(idx_meta, dict) else {}

            raw_so_members = (so_desc.get("members") or so_desc.get("Members") or []) if isinstance(so_desc, dict) else []
            if not raw_so_members and idx_field_map:
                raw_so_members = [
                    {"name": fname, "type": fdict.get("type", ""), "readable": True, "isStatic": False}
                    for fname, fdict in idx_field_map.items()
                ]

            for mem in raw_so_members:
                if not isinstance(mem, dict) or mem.get("isStatic") or mem.get("IsStatic"):
                    continue
                m_name = str(mem.get("name") or mem.get("Name") or "").strip()
                m_type = str(mem.get("type") or mem.get("Type") or "").strip()
                if not m_name or not mem.get("readable", mem.get("Readable", True)):
                    continue

                if so_id and _is_collection_type(m_type):
                    try:
                        exp = call_fn("expand_collection", {"object_id": so_id, "objectId": so_id, "field_path": m_name, "fieldPath": m_name, "limit": 50})
                        _emit_collection_entries(
                            owner_object_id=so_id,
                            owner_label=so_label,
                            owner_ns=so_ns,
                            owner_cls=so_short,
                            owner_full=so_type,
                            field_path=m_name,
                            col_payload=exp,
                            depth=1,
                        )
                    except Exception:
                        pass
                    continue

                if not _is_numeric_or_bool_type(m_type):
                    continue

                live_val: str | None = None
                if so_id:
                    try:
                        live_val = _extract_bridge_scalar(call_fn(
                            "get_component_member",
                            {"id": so_id, "game_object_id": so_id, "componentName": so_type, "component_name": so_type, "memberName": m_name, "member_name": m_name},
                        ))
                    except Exception:
                        live_val = None

                ep_id = f"live_so:{so_short}:{so_id or so_label}:{m_name}"
                if ep_id in seen_item_ids:
                    continue
                seen_item_ids.add(ep_id)
                f_meta = idx_field_map.get(m_name) or {}
                item_cat = _categorize_scriptable_or_gameplay(f"{so_label} {so_type} {m_name}") or so_cat
                bridge_items.append({
                    "id": ep_id,
                    "name": f"{so_label} — {_humanize_member_label(m_name)}",
                    "category": item_cat,
                    "source_kind": "live_bepinex_scriptable",
                    "namespace": so_ns,
                    "class_name": so_short,
                    "component": so_type,
                    "member": m_name,
                    "member_path": m_name,
                    "signature": f"{so_type}.{m_name}".lstrip("."),
                    "object_id": so_id,
                    "game_object_id": so_id,
                    "instance_name": so_label,
                    "template_name": so_label,
                    "current_value": live_val if live_val is not None else "Runtime",
                    "default_value": live_val if live_val is not None else "Runtime",
                    "value_type": "bool" if "bool" in m_type.lower() else "number",
                    "min": f_meta.get("min"),
                    "max": f_meta.get("max"),
                    "description": str(f_meta.get("tooltip") or f"ScriptableObject template '{so_label}' ({so_short}) field '{m_name}' ({m_type})."),
                })

    return bridge_items


def _probe_live_bepinex_bridge(
    game: dict[str, Any],
    info: dict[str, Any],
    *,
    reflection_call_fn: Callable[[str, dict[str, Any] | None], Any] | None = None,
    il2cpp_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Non-blocking probe of a running local BepInExMCP HTTP endpoint (returns empty list if offline)."""
    if reflection_call_fn is not None:
        return discover_live_bepinex_via_reflection(reflection_call_fn, il2cpp_index=il2cpp_index)

    port = int(game.get("bepinex_port") or info.get("mcp_port") or (8080 if info.get("mcp_bridge_installed") else 0))
    if port <= 0:
        return []
    for path in ("/mcp/endpoints", "/api/endpoints", "/health"):
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=0.35) as resp:
                if resp.status != 200:
                    continue
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(payload, dict) and isinstance(payload.get("endpoints"), list):
                    return payload["endpoints"]
                if isinstance(payload, list):
                    return payload
        except Exception:
            continue

    # Check native BepInExMCP plugin HTTP endpoints (/mcp/system/ping -> /mcp/type/search + describe + find_objects + static/SO/collection)
    ping_url = f"http://127.0.0.1:{port}/mcp/system/ping"
    try:
        with urllib.request.urlopen(ping_url, timeout=0.3) as ping_resp:
            if ping_resp.status != 200:
                return []
    except Exception:
        return []

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
        qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None and k not in ("game_object_id", "component_name", "member_name", "type_name", "object_id", "field_path")})
        target_url = f"http://127.0.0.1:{port}/mcp/{ep}" + (f"?{qs}" if qs else "")
        with urllib.request.urlopen(target_url, timeout=1.0) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))

    return discover_live_bepinex_via_reflection(_http_call, il2cpp_index=il2cpp_index)


def _is_excluded_data_file(path: Path, root: Path) -> bool:
    try:
        rel_parts = [p.lower() for p in path.relative_to(root).parts[:-1]]
    except ValueError:
        rel_parts = []
    if any(part in _EXCLUDED_DATA_DIR_NAMES for part in rel_parts):
        return True
    low_name = path.name.lower()
    if low_name in _EXCLUDED_DATA_EXACT_NAMES:
        return True
    if any(low_name.startswith(pref) for pref in _EXCLUDED_DATA_FILE_PREFIXES):
        return True
    try:
        if path.stat().st_size > 512 * 1024:
            return True
    except OSError:
        return True
    return False


def _flatten_json_scalars(node: Any, prefix: str = "", depth: int = 0, out: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    if out is None:
        out = []
    if len(out) >= 25 or depth > 3:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            if len(out) >= 25:
                break
            ks = str(k).strip()
            if not ks or ks.startswith(("$", "m_", "_")):
                continue
            path = f"{prefix}.{ks}" if prefix else ks
            if isinstance(v, (bool, int, float, str)):
                if isinstance(v, str) and len(v) > 120:
                    continue
                out.append((path, str(v)))
            elif isinstance(v, dict):
                _flatten_json_scalars(v, path, depth + 1, out)
    return out


def _flatten_xml_scalars(xml_text: str) -> list[tuple[str, str]]:
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", xml_text, re.IGNORECASE):
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[tuple[str, str]] = []

    def _walk(elem: ET.Element, prefix: str, depth: int) -> None:
        if len(out) >= 25 or depth > 3:
            return
        tag = str(elem.tag).rsplit("}", 1)[-1].strip()
        path = f"{prefix}.{tag}" if prefix else tag
        for ak, av in elem.attrib.items():
            if len(out) >= 25:
                break
            out.append((f"{path}.@{ak}", str(av)))
        txt = (elem.text or "").strip()
        if txt and not list(elem) and len(txt) <= 120:
            out.append((path, txt))
        for child in elem:
            _walk(child, path, depth + 1)

    for child in root:
        _walk(child, "", 0)
    return out


def _resolve_unity_locallow_dir(game: dict[str, Any], game_dir: Path, info: dict[str, Any]) -> Path | None:
    explicit = str(game.get("locallow_dir") or "").strip()
    if explicit:
        p = Path(explicit)
        return p if p.is_dir() else None

    company = str(game.get("company_name") or info.get("company_name") or "").strip()
    product = str(game.get("product_name") or info.get("product_name") or "").strip()
    if not (company and product) and game_dir.is_dir():
        for data_dir in sorted(game_dir.glob("*_Data"), key=lambda p: p.name.lower()):
            app_info = data_dir / "app.info"
            if app_info.is_file():
                lines = [ln.strip() for ln in app_info.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
                if len(lines) >= 2:
                    company = company or lines[0]
                    product = product or lines[1]
                    break
    if not (company and product):
        return None
    user_prof = os.environ.get("USERPROFILE")
    base = (Path(user_prof) if user_prof else Path.home()) / "AppData" / "LocalLow"
    cand = base / company / product
    return cand if cand.is_dir() else None


def _scan_unity_streaming_and_locallow_files(
    game: dict[str, Any],
    game_dir: Path,
    info: dict[str, Any],
) -> list[dict[str, Any]]:
    """Scan `<install_dir>/*_Data/StreamingAssets` and `%USERPROFILE%/AppData/LocalLow/<Company>/<Product>` for gameplay configs."""
    roots: list[tuple[str, Path]] = []
    if game_dir.is_dir():
        for data_dir in sorted(game_dir.glob("*_Data"), key=lambda p: p.name.lower()):
            sa = data_dir / "StreamingAssets"
            if sa.is_dir():
                roots.append(("streaming_assets_data", sa))
    locallow = _resolve_unity_locallow_dir(game, game_dir, info)
    if locallow is not None and locallow.is_dir():
        roots.append(("locallow_data", locallow))

    found: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for src_kind, root_dir in roots:
        files_checked = 0
        for ext in ("*.json", "*.xml", "*.cfg"):
            for fp in sorted(root_dir.rglob(ext), key=lambda p: str(p).lower()):
                if files_checked >= 12:
                    break
                if not fp.is_file() or _is_excluded_data_file(fp, root_dir):
                    continue
                files_checked += 1
                suf = fp.suffix.lower()
                pairs: list[tuple[str, str, dict[str, Any]]] = []
                if suf == ".cfg":
                    for entry in _parse_bepinex_cfg(fp):
                        sec = entry["section"]
                        if sec.strip().lower() in _EXCLUDED_BOILERPLATE_CFG_SECTIONS:
                            continue
                        pairs.append((
                            f"{sec}:{entry['key']}",
                            str(entry["value"]),
                            {"data_format": "cfg", "section": sec, "key": entry["key"], "default_value": str(entry["default_value"])},
                        ))
                elif suf == ".json":
                    try:
                        parsed = json.loads(fp.read_text(encoding="utf-8-sig", errors="replace"))
                    except (ValueError, OSError):
                        continue
                    for kpath, val in _flatten_json_scalars(parsed):
                        pairs.append((kpath, val, {"data_format": "json", "section": "json", "key": kpath}))
                elif suf == ".xml":
                    try:
                        raw_xml = fp.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    for kpath, val in _flatten_xml_scalars(raw_xml):
                        pairs.append((kpath, val, {"data_format": "xml", "section": "xml", "key": kpath}))

                for key_id, raw_val, extra in pairs[:25]:
                    ep_id = f"unity_data:{src_kind}:{fp.name}:{key_id}"
                    if ep_id in seen_ids:
                        continue
                    seen_ids.add(ep_id)
                    leaf_key = extra.get("key") or key_id
                    resolved = resolve_endpoint_classification(
                        leaf_key,
                        engine="unity",
                        reference_candidates=[leaf_key, f"{fp.stem}.{leaf_key}"],
                        embedded_source="cfg_metadata" if suf == ".cfg" else "unity_data",
                        embedded_context=f"{fp.stem} {src_kind} {key_id}",
                        embedded_label=f"{_humanize_member_label(leaf_key)} ({fp.stem})",
                        embedded_description=f"Gameplay data entry '{leaf_key}' in {fp.name} ({src_kind.replace('_', ' ')}).",
                    )
                    found.append(
                        _make_endpoint(
                            endpoint_id=ep_id,
                            category=resolved["category"],
                            name=resolved["simple_label"],
                            simple_description=resolved["simple_description"],
                            current_value=raw_val,
                            default_value=str(extra.get("default_value") or raw_val),
                            delivery_type="Config",
                            can_edit=True,
                            value_type=resolved.get("value_type"),
                            options=resolved.get("options"),
                            min_val=resolved.get("min"),
                            max_val=resolved.get("max"),
                            classification_source=resolved["source"],
                            confidence=resolved["confidence"],
                            advanced_details={
                                "source_kind": src_kind,
                                "file_path": str(fp),
                                "file_name": fp.name,
                                **extra,
                            },
                        )
                    )
    return found


def _scan_unity_endpoints(
    game: dict[str, Any],
    game_dir: Path,
    info: dict[str, Any],
    *,
    live_bridge_fn: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]] | None = None,
    bridge_call_fn: Callable[[str, dict[str, Any] | None], Any] | None = None,
) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []

    # 1. BepInEx/config/*.cfg entries (excluding BepInEx & BepInExMCP framework boilerplate)
    cfg_dir = game_dir / "BepInEx" / "config"
    if cfg_dir.is_dir():
        for cfg_file in sorted(cfg_dir.glob("*.cfg"), key=lambda p: p.name.lower()):
            if cfg_file.name.lower() in _EXCLUDED_BOILERPLATE_CFG_FILES:
                continue
            for entry in _parse_bepinex_cfg(cfg_file):
                if entry["section"].strip().lower() in _EXCLUDED_BOILERPLATE_CFG_SECTIONS:
                    continue
                ep_id = f"bep_cfg:{cfg_file.name}:{entry['section']}:{entry['key']}"
                choices = entry.get("choices") or []
                resolved = resolve_endpoint_classification(
                    entry["key"],
                    engine="unity",
                    reference_candidates=[f"{entry['section']}.{entry['key']}", entry["key"]],
                    embedded_source=entry.get("metadata_source") or "cfg_metadata",
                    embedded_context=f"{cfg_file.stem} {entry['section']} {entry['simple_description']}",
                    embedded_label=f"{entry['key']} ({cfg_file.stem})",
                    embedded_description=entry["simple_description"],
                    embedded_value_type=entry.get("value_type"),
                    embedded_options=list(choices) if choices else None,
                    embedded_min=entry.get("min_value"),
                    embedded_max=entry.get("max_value"),
                    embedded_default=entry["default_value"],
                )
                endpoints.append(
                    _make_endpoint(
                        endpoint_id=ep_id,
                        category=resolved["category"],
                        name=resolved["simple_label"],
                        simple_description=resolved["simple_description"],
                        current_value=entry["value"],
                        default_value=str(resolved["default_value"] if resolved["default_value"] is not None else entry["default_value"]),
                        delivery_type=resolved.get("delivery_type") or "Config",
                        can_edit=True,
                        value_type=resolved.get("value_type"),
                        options=resolved.get("options"),
                        min_val=resolved.get("min"),
                        max_val=resolved.get("max"),
                        step_val=resolved.get("step"),
                        classification_source=resolved["source"],
                        confidence=resolved["confidence"],
                        advanced_details={
                            "source_kind": "bepinex_cfg",
                            "file_path": str(cfg_file),
                            "file_name": cfg_file.name,
                            "section": entry["section"],
                            "key": entry["key"],
                            "setting_type": entry["setting_type"],
                            "acceptable_values": entry.get("acceptable_values", ""),
                        },
                    )
                )

    game_slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    il2cpp_idx = load_il2cpp_index(game_slug, app_data_root=APP_DATA_ROOT)

    # 2. Live BepInExMCP bridge components/methods (multi-instance + nested fields + static singletons + ScriptableObjects + collections)
    bridge_items = (
        live_bridge_fn(game, info)
        if live_bridge_fn is not None
        else _probe_live_bepinex_bridge(game, info, reflection_call_fn=bridge_call_fn, il2cpp_index=il2cpp_idx)
    )
    bridge_online = bool(bridge_items)
    for idx, item in enumerate(bridge_items, start=1):
        if not isinstance(item, dict):
            continue
        ns = str(item.get("namespace") or "")
        cls = str(item.get("class_name") or "UnknownComponent")
        comp = str(item.get("component") or f"{ns}.{cls}".strip(".") or cls)
        if is_excluded_unity_namespace(comp):
            continue
        member = str(item.get("member_path") or item.get("member") or item.get("method") or f"member_{idx}")
        sig = str(item.get("signature") or f"{ns}.{cls}.{member}".lstrip("."))
        inst_name = str(item.get("instance_name") or "").strip()
        obj_id = str(item.get("object_id") or item.get("game_object_id") or "").strip()
        src_kind = str(item.get("source_kind") or "live_bepinex_mcp")
        default_label = str(item.get("name") or (f"{inst_name} — {_humanize_member_label(member)}" if inst_name else f"{cls}.{member}"))

        resolved = resolve_endpoint_classification(
            str(item.get("collection_key_label") or member),
            engine="unity",
            reference_candidates=[sig, f"{cls}.{member}", f"*.{member.rsplit('.', 1)[-1]}", member],
            embedded_context=f"{cls} {inst_name} {item.get('collection_key_label', '')} {item.get('description', '')}",
            embedded_label=default_label,
            embedded_description=str(item.get("description") or f"Live BepInExMCP bridge member '{member}' on {cls}."),
            embedded_category=str(item.get("category")) if item.get("category") else None,
            embedded_min=item.get("min"),
            embedded_max=item.get("max"),
        )
        # Preserve instance prefix when reference_dict supplied a generic simple_label
        final_label = str(item.get("name") or (f"{inst_name} — {resolved['simple_label']}" if inst_name else resolved["simple_label"]))
        cur_v = str(item.get("current_value") if item.get("current_value") is not None else item.get("value", "Live"))
        def_v = str(item.get("default_value") if item.get("default_value") is not None else cur_v)
        fallback_ep_id = f"live_mcp:{cls}:{obj_id}:{member}" if obj_id else f"live_mcp:{cls}:{member}"
        adv_payload: dict[str, Any] = {
            "source_kind": src_kind,
            "namespace": ns,
            "class_name": cls,
            "component": comp,
            "member": member,
            "member_path": member,
            "method_signature": sig,
            "object_id": obj_id,
            "game_object_id": obj_id,
            "instance_name": inst_name,
        }
        for extra_key in ("field_path", "collection_key", "collection_key_label", "collection_type", "element_type", "template_name"):
            if extra_key in item and item[extra_key] is not None:
                adv_payload[extra_key] = item[extra_key]
        endpoints.append(
            _make_endpoint(
                endpoint_id=str(item.get("id") or fallback_ep_id),
                category=str(item.get("category") or resolved["category"]),
                name=final_label,
                simple_description=resolved["simple_description"],
                current_value=cur_v,
                default_value=def_v,
                delivery_type="Bridge",
                can_edit=True,
                value_type=item.get("value_type") or resolved.get("value_type"),
                options=resolved.get("options"),
                min_val=item.get("min") if item.get("min") is not None else resolved.get("min"),
                max_val=item.get("max") if item.get("max") is not None else resolved.get("max"),
                step_val=resolved.get("step"),
                classification_source=resolved["source"],
                confidence=resolved["confidence"],
                advanced_details=adv_payload,
            )
        )

    # 3. Curated & parsed IL2CPP/Mono dump signatures (dump.cs / script.json / interop / Managed)
    interop_dir = game_dir / "BepInEx" / "interop"
    managed_dirs = [d / "Managed" for d in game_dir.glob("*_Data") if (d / "Managed").is_dir()]
    has_dump_surface = interop_dir.is_dir() or bool(managed_dirs)
    if has_dump_surface:
        assembly_names: set[str] = set()
        if interop_dir.is_dir():
            assembly_names.update(p.stem.lower() for p in interop_dir.glob("*.dll"))
        for md in managed_dirs:
            assembly_names.update(p.stem.lower() for p in md.glob("*.dll"))

        if "jade" in " ".join(assembly_names) or make_slug(str(game.get("id") or "")) == "battleground":
            curated_sigs = [
                ("player_character", "Hero Max Health", "Jade.GameView.CameraTargetCharacter", "targetPiece.stats.Health.maxValue", "22", "Adjusts the selected hero's maximum HP."),
                ("player_character", "Hero Action Points Per Turn", "Jade.GameView.CameraTargetCharacter", "targetPiece.actionPointsPerTurn", "2", "Adjusts how many Action Points a hero receives each turn."),
                ("game_systems", "Base Attack Damage", "Jade.GameView.CameraTargetCharacter", "targetPiece.stats.AttackDamage.value", "3", "Adjusts melee/base attack damage on the target piece."),
                ("qol", "Camera Field-of-View Distance", "Jade.GameView.StandaloneAvatarNonVR", "fovInitialCameraDistance", "12.0", "Adjusts default board camera zoom distance."),
            ]
            for cat, title, comp, mem, d_val, desc in curated_sigs:
                ep_id = f"dump_sig:{comp}:{mem}"
                if any(e["id"] == ep_id for e in endpoints):
                    continue
                endpoints.append(
                    _make_endpoint(
                        endpoint_id=ep_id,
                        category=cat,
                        name=title,
                        simple_description=desc,
                        current_value=d_val,
                        default_value=d_val,
                        delivery_type="Bridge",
                        can_edit=True,
                        classification_source="reference_dict",
                        confidence="high",
                        advanced_details={
                            "source_kind": "il2cpp_dump_signature",
                            "namespace": comp.rsplit(".", 1)[0],
                            "class_name": comp.rsplit(".", 1)[-1],
                            "member": mem,
                            "method_signature": f"{comp}.{mem}",
                        },
                    )
                )

    # Scan Il2CppDumper dump.cs / script.json in game interop or local tools folder (works from main menu before match loads)
    eff_tools_root = (
        TOOLS_ROOT
        if TOOLS_ROOT != _DEFAULT_TOOLS_ROOT
        else ((APP_DATA_ROOT / "tools") if APP_DATA_ROOT != _DEFAULT_APP_DATA_ROOT else TOOLS_ROOT)
    )
    dump_records = parse_unity_dump_metadata(game_dir, tools_root=eff_tools_root)
    for rec in dump_records:
        cls_nm = rec["class_name"]
        tok = rec["member"]
        ep_id = f"il2cpp_dump:{cls_nm}:{tok}"
        if any(e["id"] == ep_id for e in endpoints):
            continue
        ctx = f"{rec.get('namespace', '')} {cls_nm} {rec.get('header', '')} {rec.get('tooltip', '')}"
        resolved = resolve_endpoint_classification(
            tok,
            engine="unity",
            reference_candidates=[rec["signature"], f"{cls_nm}.{tok}", tok],
            embedded_source="unity_dump",
            embedded_context=ctx,
            embedded_label=f"{rec['simple_label']} (Dump)",
            embedded_description=rec["simple_description"],
            embedded_value_type=rec.get("value_type"),
            embedded_min=rec.get("min_value"),
            embedded_max=rec.get("max_value"),
            embedded_default="Runtime",
        )
        endpoints.append(
            _make_endpoint(
                endpoint_id=ep_id,
                category=resolved["category"],
                name=resolved["simple_label"],
                simple_description=resolved["simple_description"],
                current_value="Runtime",
                default_value="Runtime",
                delivery_type="Bridge",
                can_edit=True,
                value_type=resolved.get("value_type"),
                min_val=resolved.get("min"),
                max_val=resolved.get("max"),
                classification_source=resolved["source"],
                confidence=resolved["confidence"],
                advanced_details={
                    "source_kind": "il2cpp_dump_cs",
                    "file_path": rec["file_path"],
                    "namespace": rec.get("namespace", ""),
                    "class_name": cls_nm,
                    "member": tok,
                    "method_signature": rec["signature"],
                    "header": rec.get("header", ""),
                    "tooltip": rec.get("tooltip", ""),
                },
            )
        )

    if has_dump_surface:
        # Standalone compiled BepInEx Mod File generator endpoint for Unity
        endpoints.append(
            _make_endpoint(
                endpoint_id="unity_mod:bepinex_custom_balance",
                category="game_systems",
                name="BepInEx Balance Plugin (.dll)",
                simple_description="Compiles & deploys a standalone BepInEx C# balance plugin DLL into BepInEx/plugins.",
                current_value="23",
                default_value="22",
                delivery_type="Mod File",
                can_edit=True,
                value_type="number",
                classification_source="reference_dict",
                confidence="high",
                advanced_details={
                    "source_kind": "bepinex_compiled_dll",
                    "rule_type": "member_set",
                    "entity_name": "Piece_DragonbornPaladin(Clone)",
                    "component": "Jade.GameView.CameraTargetCharacter",
                    "member": "targetPiece.stats.Health.maxValue",
                },
            )
        )

    # 4. Cached Il2CppDumper symbol/enum/method index (%LOCALAPPDATA%\TheModdingTool\dumps\<slug>\il2cpp_index.json)
    if isinstance(il2cpp_idx, dict):
        enums_map = il2cpp_idx.get("enums") if isinstance(il2cpp_idx.get("enums"), dict) else {}
        classes_map = il2cpp_idx.get("classes") if isinstance(il2cpp_idx.get("classes"), dict) else {}
        added_idx = 0
        for cls_full, cls_info in classes_map.items():
            if added_idx >= 40:
                break
            cls_short = str(cls_info.get("class_name") or cls_full.rsplit(".", 1)[-1])
            ns = str(cls_info.get("namespace") or "")
            for fld in cls_info.get("fields", []):
                if added_idx >= 40:
                    break
                f_name = str(fld.get("name") or "").strip()
                f_type = str(fld.get("type") or "").strip()
                if not f_name:
                    continue
                ep_id = f"il2cpp_idx:{cls_short}:{f_name}"
                if any(e["id"] == ep_id or e["id"] == f"il2cpp_dump:{cls_short}:{f_name}" for e in endpoints):
                    continue
                enum_match = enums_map.get(f_type) or next(
                    (ev for ek, ev in enums_map.items() if ek.rsplit(".", 1)[-1] == f_type.rsplit(".", 1)[-1]),
                    None,
                )
                enum_opts = list(enum_match.keys()) if isinstance(enum_match, dict) and enum_match else None
                ft_low = f_type.lower().rsplit(".", 1)[-1]
                v_type = (
                    "select"
                    if enum_opts
                    else ("bool" if ft_low in ("bool", "boolean") else ("number" if ft_low in ("int", "int32", "int64", "float", "single", "double", "byte") or fld.get("min") is not None else "string"))
                )
                desc = str(fld.get("tooltip") or "").strip() or (
                    f"{fld['header']}: Compiled IL2CPP field '{f_name}' ({f_type}) on {cls_full}."
                    if fld.get("header")
                    else f"Indexed IL2CPP compiled field '{f_name}' ({f_type}) on {cls_full}."
                )
                ctx = f"{ns} {cls_short} {fld.get('header', '')} {fld.get('tooltip', '')} {'scriptableobject' if cls_info.get('is_scriptable_object') else ''}"
                resolved = resolve_endpoint_classification(
                    f_name,
                    engine="unity",
                    reference_candidates=[f"{cls_full}.{f_name}", f"{cls_short}.{f_name}", f_name],
                    embedded_source="unity_dump",
                    embedded_context=ctx,
                    embedded_label=f"{cls_short}.{f_name}",
                    embedded_description=desc,
                    embedded_value_type=v_type,
                    embedded_options=enum_opts,
                    embedded_min=fld.get("min"),
                    embedded_max=fld.get("max"),
                    embedded_default="Runtime",
                )
                endpoints.append(
                    _make_endpoint(
                        endpoint_id=ep_id,
                        category=resolved["category"],
                        name=resolved["simple_label"],
                        simple_description=resolved["simple_description"],
                        current_value="Runtime",
                        default_value="Runtime",
                        delivery_type="Bridge",
                        can_edit=True,
                        value_type=resolved.get("value_type"),
                        options=resolved.get("options"),
                        min_val=resolved.get("min"),
                        max_val=resolved.get("max"),
                        classification_source=resolved["source"],
                        confidence=resolved["confidence"],
                        advanced_details={
                            "source_kind": "il2cpp_index_field",
                            "namespace": ns,
                            "class_name": cls_short,
                            "component": cls_full,
                            "member": f_name,
                            "field_type": f_type,
                            "enum_values": enum_match if isinstance(enum_match, dict) else None,
                            "method_signature": f"{cls_full}.{f_name}",
                        },
                    )
                )
                added_idx += 1

        raw_methods = [m for m in (il2cpp_idx.get("patchable_methods") or []) if isinstance(m, dict)]
        raw_methods.sort(
            key=lambda m: (
                -sum(1 for kw in _PATCHABLE_HIGH_SIGNAL_KEYWORDS if kw in f"{m.get('name', '')} {m.get('class_full_name', '')}".lower()),
                str(m.get("class_full_name", "")),
                str(m.get("name", "")),
            )
        )
        for meth in raw_methods[:25]:
            cls_full = str(meth.get("class_full_name") or "")
            cls_short = str(meth.get("class_name") or cls_full.rsplit(".", 1)[-1])
            m_name = str(meth.get("name") or "")
            if not cls_full or not m_name:
                continue
            ep_id = f"il2cpp_method:{cls_short}:{m_name}"
            if any(e["id"] == ep_id for e in endpoints):
                continue

            ret_type = str(meth.get("return_type") or "").strip()
            ret_tail = ret_type.rsplit(".", 1)[-1].lower()
            is_bool_ret = ret_tail in ("bool", "boolean")
            is_num_ret = ret_tail in ("int", "int32", "int64", "uint", "uint32", "float", "single", "double", "byte", "decimal")

            if is_bool_ret:
                mod_kind = "bool_override"
                def_val = "vanilla"
                val_type = "select"
                opts: list[str] | None = ["vanilla", "always_true", "always_false"]
                min_v, max_v, step_v = None, None, None
            elif is_num_ret:
                mod_kind = "numeric_multiplier"
                def_val = "1.0"
                val_type = "number"
                opts = None
                min_v, max_v, step_v = 0.0, 10.0, 0.1
            else:
                mod_kind = "log_hook"
                def_val = "vanilla"
                val_type = "string"
                opts = None
                min_v, max_v, step_v = None, None, None

            method_delivery = "Bridge" if bridge_online else "Mod File"
            params_raw = str(meth.get("parameters") or "").strip()
            param_types = ",".join(
                seg.strip().rsplit(" ", 1)[0].strip()
                for seg in params_raw.split(",")
                if seg.strip()
            )
            resolved = resolve_endpoint_classification(
                m_name,
                engine="unity",
                reference_candidates=[f"{cls_full}.{m_name}", m_name],
                embedded_source="unity_dump",
                embedded_context=f"{cls_full} {params_raw}",
                embedded_label=f"{cls_short}.{m_name}()",
                embedded_description=(
                    f"Harmony {'multiplier' if is_num_ret else ('boolean override' if is_bool_ret else 'hook')} "
                    f"for {cls_full}.{m_name}() returning '{ret_type or 'void'}'."
                ),
                embedded_value_type=val_type,
                embedded_options=opts,
                embedded_min=min_v,
                embedded_max=max_v,
                embedded_default=def_val,
            )
            endpoints.append(
                _make_endpoint(
                    endpoint_id=ep_id,
                    category=resolved["category"],
                    name=resolved["simple_label"],
                    simple_description=resolved["simple_description"],
                    current_value=def_val,
                    default_value=def_val,
                    delivery_type=method_delivery,
                    can_edit=True,
                    value_type=val_type,
                    options=opts,
                    min_val=min_v,
                    max_val=max_v,
                    step_val=step_v,
                    classification_source=resolved["source"],
                    confidence=resolved["confidence"],
                    advanced_details={
                        "source_kind": "il2cpp_index_method",
                        "rule_type": "harmony_patch",
                        "invoke_kind": "method",
                        "modifier_kind": mod_kind,
                        "hook_kind": "Postfix",
                        "namespace": str(meth.get("namespace") or ""),
                        "class_name": cls_short,
                        "component": cls_full,
                        "member": m_name,
                        "method_signature": m_name,
                        "full_signature": str(meth.get("signature") or m_name),
                        "parameter_types": param_types,
                        "return_type": ret_type,
                        "rva": str(meth.get("rva") or ""),
                    },
                )
            )

    # 5. StreamingAssets & LocalLow plaintext gameplay configs (.json / .xml / .cfg)
    for data_ep in _scan_unity_streaming_and_locallow_files(game, game_dir, info):
        if not any(e["id"] == data_ep["id"] for e in endpoints):
            endpoints.append(data_ep)

    return endpoints


def _error_log_endpoints(game_dir: Path, info: dict[str, Any]) -> list[dict[str, Any]]:
    proj = str(info.get("detected_project_name") or "")
    tail_errs = read_game_log_tail(game_dir, lines=25, errors_only=True, detected_project_name=proj)
    log_path = tail_errs.get("log_path") or "No game log file detected yet"
    err_cnt = int(tail_errs.get("error_count") or 0)
    last_err = (tail_errs.get("lines") or [""])[-1] if tail_errs.get("lines") else "No recent errors detected."
    return [
        _make_endpoint(
            endpoint_id="err_log:game_error_count",
            category="error_logs",
            name="Active Game / Loader Error Stream",
            simple_description=f"Latest diagnostic error match: {last_err[:140]}",
            current_value=f"{err_cnt} error(s)",
            default_value="0 error(s)",
            delivery_type="Bridge",
            can_edit=False,
            value_type="diagnostic",
            advanced_details={
                "source_kind": "log_tail",
                "file_path": log_path,
                "error_count": err_cnt,
                "latest_error": last_err,
            },
        ),
        _make_endpoint(
            endpoint_id="err_log:modding_readiness",
            category="error_logs",
            name="Loader & Bridge Bootstrap Diagnostics",
            simple_description="Tracks BepInEx/UE4SS bootstrap presence and bridge readiness.",
            current_value=str(info.get("modding_readiness") or "Unknown"),
            default_value="Ready",
            delivery_type="Bridge",
            can_edit=False,
            value_type="diagnostic",
            advanced_details={
                "source_kind": "bootstrap_health",
                "engine_category": info.get("mcp_category") or "Unknown",
                "file_path": log_path,
            },
        ),
    ]


def _sync_game_defaults(
    slug: str,
    endpoints: list[dict[str, Any]],
    *,
    defaults_dir: Path | None = None,
    reset_defaults: bool = False,
) -> tuple[list[dict[str, Any]], Path]:
    safe_slug = make_slug(slug) or "game"
    defaults_path = get_game_defaults_dir(defaults_dir) / f"{safe_slug}.json"
    existing: dict[str, Any] = {}
    if defaults_path.is_file() and not reset_defaults:
        try:
            loaded = load_json_sanitized(defaults_path, persist=True)
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

    stored_map: dict[str, str] = dict(existing.get("defaults") or {})
    stored_snapshot: dict[str, Any] = dict(existing.get("endpoints_snapshot") or {})
    changed = not defaults_path.is_file() or reset_defaults
    now_iso = datetime.now(timezone.utc).isoformat()

    for ep in endpoints:
        eid = ep["id"]
        if eid not in stored_map or reset_defaults:
            stored_map[eid] = str(ep["default_value"])
            changed = True
        else:
            # Preserve first captured baseline default across rescans!
            ep["default_value"] = str(stored_map[eid])
            ep["is_modified"] = str(ep["current_value"]).strip() != str(ep["default_value"]).strip()
        if ep.get("category") != "error_logs":
            stored_snapshot[eid] = ep
            changed = True

    if changed:
        payload = {
            "slug": safe_slug,
            "captured_at": existing.get("captured_at") or now_iso,
            "updated_at": now_iso,
            "defaults": stored_map,
            "endpoints_snapshot": stored_snapshot,
        }
        atomic_write_json(defaults_path, payload)

    return endpoints, defaults_path


def scan_game_endpoints(
    game: dict[str, Any],
    *,
    defaults_dir: Path | None = None,
    reset_defaults: bool = False,
    live_bridge_fn: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]] | None = None,
    bridge_call_fn: Callable[[str, dict[str, Any] | None], Any] | None = None,
) -> dict[str, Any]:
    """Scan a managed game, discover & categorize modding endpoints, and persist baseline defaults."""
    slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    game_name = str(game.get("name") or slug)
    game_dir = Path(str(game.get("game_dir") or "").strip())

    cached = game.get("cached_modding_info") if isinstance(game.get("cached_modding_info"), dict) else None
    if cached and cached.get("mcp_category"):
        info = dict(cached)
    elif game_dir.is_dir():
        info = inspect_game_modding_info(game_dir, game_name)
    else:
        info = {"mcp_category": game.get("runtime") or "Unknown"}

    cat_low = str(info.get("mcp_category") or game.get("runtime") or "").lower()
    tech_low = str(info.get("sheet_engine_tech") or "").lower()
    rt_low = str(game.get("runtime") or "").lower()
    is_unreal = bool(info.get("is_unreal_engine") or "unreal" in cat_low or "unreal" in tech_low or "unreal" in rt_low)
    is_unity = (not is_unreal) and (
        "unity" in cat_low
        or "il2cpp" in str(info.get("scripting_backend") or "").lower()
        or "mono" in str(info.get("scripting_backend") or "").lower()
        or (game_dir.is_dir() and ((game_dir / "GameAssembly.dll").is_file() or (game_dir / "BepInEx").is_dir() or bool(list(game_dir.glob("*_Data")))))
    )

    discovered: list[dict[str, Any]] = []
    if game_dir.is_dir():
        if is_unreal:
            discovered.extend(_scan_unreal_endpoints(game_dir, game_name, info))
        elif is_unity:
            discovered.extend(_scan_unity_endpoints(game, game_dir, info, live_bridge_fn=live_bridge_fn, bridge_call_fn=bridge_call_fn))
        else:
            # Scan any standalone .cfg/.ini configs for generic games
            for cfg in sorted(list(game_dir.glob("*.ini")) + list(game_dir.glob("*.cfg")), key=lambda p: p.name.lower())[:5]:
                for sec, kv in parse_unreal_ini_file(cfg).items():
                    for k, v in list(kv.items())[:20]:
                        resolved_gen = resolve_endpoint_classification(
                            k,
                            engine="generic",
                            reference_candidates=[k, f"{sec}.{k}"],
                            embedded_context=f"{cfg.stem} {sec}",
                            embedded_description=f"Configuration setting '{k}' under [{sec}] in {cfg.name}.",
                        )
                        discovered.append(
                            _make_endpoint(
                                endpoint_id=f"generic_ini:{cfg.name}:{sec}:{k}",
                                category=resolved_gen["category"],
                                name=resolved_gen["simple_label"],
                                simple_description=resolved_gen["simple_description"],
                                current_value=str(v),
                                default_value=str(v),
                                delivery_type="Config",
                                can_edit=True,
                                value_type=resolved_gen.get("value_type"),
                                options=resolved_gen.get("options"),
                                min_val=resolved_gen.get("min"),
                                max_val=resolved_gen.get("max"),
                                classification_source=resolved_gen["source"],
                                confidence=resolved_gen["confidence"],
                                advanced_details={
                                    "source_kind": "generic_ini",
                                    "file_path": str(cfg),
                                    "file_name": cfg.name,
                                    "section": sec,
                                    "key": k,
                                },
                            )
                        )

    discovered.extend(_error_log_endpoints(game_dir, info))

    # Merge any community-imported endpoints persisted for this game
    seen_ep_ids = {str(ep.get("id")) for ep in discovered}
    for imp_ep in load_game_imported_endpoints(slug, app_data_root=APP_DATA_ROOT):
        if str(imp_ep.get("id")) not in seen_ep_ids:
            seen_ep_ids.add(str(imp_ep.get("id")))
            discovered.append(
                _make_endpoint(
                    endpoint_id=str(imp_ep["id"]),
                    category=str(imp_ep.get("category") or "uncategorized"),
                    name=str(imp_ep.get("name") or imp_ep["id"]),
                    simple_description=str(imp_ep.get("simple_description") or ""),
                    current_value=str(imp_ep.get("current_value") if imp_ep.get("current_value") is not None else imp_ep.get("default_value", "")),
                    default_value=str(imp_ep.get("default_value") if imp_ep.get("default_value") is not None else ""),
                    delivery_type=str(imp_ep.get("delivery_type") or "Config"),
                    can_edit=bool(imp_ep.get("can_edit", False)),
                    value_type=imp_ep.get("value_type"),
                    options=imp_ep.get("options"),
                    min_val=imp_ep.get("min"),
                    max_val=imp_ep.get("max"),
                    step_val=imp_ep.get("step"),
                    classification_source=str(imp_ep.get("classification_source") or "community_import"),
                    confidence=str(imp_ep.get("confidence") or "high"),
                    advanced_details=dict(imp_ep.get("advanced_details") or {}),
                )
            )

    # Check persistent self-learning endpoint_knowledge.json FIRST before finalized classification
    engine_name = "unreal" if is_unreal else ("unity" if is_unity else "generic")
    for ep in discovered:
        if ep.get("category") == "error_logs" and str(ep.get("id", "")).startswith("err_log:"):
            continue
        adv = ep.get("advanced_details") or {}
        cands = [
            extract_endpoint_symbol(ep),
            str(adv.get("symbol") or ""),
            str(adv.get("method_signature") or ""),
        ]
        if not adv.get("section") and adv.get("key"):
            cands.append(str(adv["key"]))
        k_hit = lookup_endpoint_knowledge(engine_name, cands, app_data_root=APP_DATA_ROOT)
        if k_hit is not None:
            new_cat = k_hit["category"] if k_hit["category"] in _ALL_CATEGORY_LABELS else "uncategorized"
            ep["category"] = new_cat
            ep["category_label"] = _ALL_CATEGORY_LABELS[new_cat]
            if k_hit.get("simple_description"):
                ep["simple_description"] = str(k_hit["simple_description"])
            ep["classification_source"] = "knowledge_cache"
            ep["confidence"] = str(k_hit.get("confidence") or "high")
            if isinstance(ep.get("advanced_details"), dict):
                ep["advanced_details"]["classification_source"] = "knowledge_cache"
                ep["advanced_details"]["classification_confidence"] = ep["confidence"]

    synced, defaults_path = _sync_game_defaults(slug, discovered, defaults_dir=defaults_dir, reset_defaults=reset_defaults)

    counts: dict[str, int] = {c["id"]: 0 for c in ENDPOINT_CATEGORIES}
    for ep in synced:
        counts[ep["category"]] = counts.get(ep["category"], 0) + 1

    # Keep Error Log Capture diagnostics separate from gameplay endpoint totals
    gameplay_eps = [e for e in synced if e.get("category") != "error_logs"]
    gameplay_total = len(gameplay_eps)
    gameplay_modified = sum(1 for e in gameplay_eps if e.get("is_modified"))

    append_audit_log(
        action="endpoints.scan",
        status="ok",
        summary=f"Scanned {gameplay_total} modding endpoint(s) for '{game_name}' ({'Unreal' if is_unreal else ('Unity' if is_unity else 'PC')})",
        details={"slug": slug, "count": gameplay_total, "diagnostics_count": counts.get("error_logs", 0), "defaults_path": str(defaults_path), "counts": counts},
    )
    return {
        "status": "ok",
        "slug": slug,
        "game_name": game_name,
        "engine": "unreal" if is_unreal else ("unity" if is_unity else "generic"),
        "defaults_path": str(defaults_path),
        "categories": [
            {**c, "count": counts.get(c["id"], 0)}
            for c in ENDPOINT_CATEGORIES
        ],
        "uncategorized_count": counts.get("uncategorized", 0),
        "diagnostics_count": counts.get("error_logs", 0),
        "total_count": gameplay_total,
        "modified_count": gameplay_modified,
        "endpoints": synced,
    }


def _update_ini_or_cfg_key(target_file: Path, section: str, key: str, new_value: str) -> None:
    """Update or insert *key* inside *section* of a UTF-8 `.ini` or BepInEx `.cfg` file."""
    lines = target_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines() if target_file.is_file() else []
    sep = " = " if target_file.suffix.lower() == ".cfg" else "="
    target_sec_norm = f"[{section}]".lower()
    in_section = False
    section_found = False
    section_last_line_idx = -1
    updated = False
    out: list[str] = []

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not updated:
                out.append(f"{key}{sep}{new_value}")
                updated = True
            in_section = stripped.lower() == target_sec_norm
            if in_section:
                section_found = True
            out.append(line)
            if in_section:
                section_last_line_idx = len(out) - 1
            continue

        if in_section and not stripped.startswith((";", "#")) and "=" in stripped:
            k_part = stripped.split("=", 1)[0].strip()
            if k_part == key:
                out.append(f"{key}{sep}{new_value}")
                updated = True
                section_last_line_idx = len(out) - 1
                continue

        out.append(line)
        if in_section and stripped:
            section_last_line_idx = len(out) - 1

    if not updated:
        if section_found and section_last_line_idx >= 0:
            out.insert(section_last_line_idx + 1, f"{key}{sep}{new_value}")
        else:
            if out and out[-1] != "":
                out.append("")
            out.append(f"[{section}]")
            out.append(f"{key}{sep}{new_value}")

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("\n".join(out) + "\n", encoding="utf-8")


def apply_or_reset_endpoint(
    slug: str,
    game: dict[str, Any],
    endpoint_id: str,
    *,
    value: str | None = None,
    reset_to_default: bool = False,
    defaults_dir: Path | None = None,
    snapshots_root: Path | None = None,
) -> dict[str, Any]:
    """Apply a new value or reset a single discovered Config endpoint back to its stored baseline default."""
    report = scan_game_endpoints(game, defaults_dir=defaults_dir)
    target_ep = next((e for e in report["endpoints"] if e["id"] == endpoint_id), None)
    if target_ep is None:
        raise KeyError(f"Endpoint not found: {endpoint_id}")
    if not target_ep.get("can_edit"):
        raise ValueError(f"Endpoint '{target_ep['name']}' ({target_ep['delivery_type']}) is read-only / diagnostic.")

    adv = target_ep.get("advanced_details") or {}
    file_path_str = str(adv.get("file_path") or "").strip()
    section = str(adv.get("section") or "General").strip()
    key = str(adv.get("key") or "").strip()
    if not file_path_str or not key:
        raise ValueError(f"Endpoint '{endpoint_id}' does not expose a writable file key.")

    target_file = Path(file_path_str)
    game_dir = Path(str(game.get("game_dir") or "").strip())
    if game_dir.is_dir():
        try:
            # Enforce confinement when config resides inside the game folder (allow %LOCALAPPDATA% Unreal profiles)
            if target_file.resolve().is_relative_to(game_dir.resolve()):
                ensure_within(target_file, game_dir)
        except OSError:
            pass

    target_val = str(target_ep["default_value"]) if reset_to_default else str(value if value is not None else target_ep["current_value"])

    snap_id = ""
    if game_dir.is_dir():
        try:
            snap = create_game_snapshot(
                make_slug(slug),
                game_dir,
                label=f"Pre-endpoint {'reset' if reset_to_default else 'edit'} ({key})",
                engine=report["engine"],
                extra_ini_paths=[target_file] if target_file.is_file() else None,
                snapshots_root=snapshots_root,
            )
            snap_id = str(snap.get("snapshot_id", ""))
        except ValueError:
            snap_id = ""

    _update_ini_or_cfg_key(target_file, section, key, target_val)

    action_name = "endpoints.reset_default" if reset_to_default else "endpoints.update"
    summary = (
        f"{'Reset' if reset_to_default else 'Updated'} '{target_ep['name']}' ({key}) to '{target_val}'"
        + (f" (snapshot: {snap_id})" if snap_id else "")
    )
    append_audit_log(
        action=action_name,
        status="ok",
        summary=summary,
        details={"slug": slug, "endpoint_id": endpoint_id, "key": key, "value": target_val, "snapshot_id": snap_id},
    )

    updated_report = scan_game_endpoints(game, defaults_dir=defaults_dir)
    updated_ep = next((e for e in updated_report["endpoints"] if e["id"] == endpoint_id), target_ep)
    return {
        "status": "ok",
        "summary": summary,
        "snapshot_id": snap_id,
        "endpoint": updated_ep,
        "report": updated_report,
    }

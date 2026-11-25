"""Contextual modding endpoint discovery, categorization, and baseline default persistence."""

from __future__ import annotations

import json
import re
import urllib.request
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
    load_engine_reference_entries,
    parse_bepinex_cfg as _parse_bepinex_cfg_rich,
    parse_unity_dump_metadata,
    parse_unreal_property_identifier,
    resolve_endpoint_classification,
    scan_unreal_datatable_assets,
)
from app.services.game_snapshots import create_game_snapshot, ensure_within
from app.services.game_store import inspect_game_modding_info
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


def _parse_bepinex_cfg(cfg_path: Path) -> list[dict[str, Any]]:
    return _parse_bepinex_cfg_rich(cfg_path)


def _probe_live_bepinex_bridge(game: dict[str, Any], info: dict[str, Any]) -> list[dict[str, Any]]:
    """Non-blocking probe of a running local BepInExMCP HTTP endpoint (returns empty list if offline)."""
    port = int(game.get("bepinex_port") or info.get("mcp_port") or 0)
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
    return []


def _scan_unity_endpoints(
    game: dict[str, Any],
    game_dir: Path,
    info: dict[str, Any],
    *,
    live_bridge_fn: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []

    # 1. BepInEx/config/*.cfg entries (waterfall: reference_dict -> cfg_metadata -> heuristic)
    cfg_dir = game_dir / "BepInEx" / "config"
    if cfg_dir.is_dir():
        for cfg_file in sorted(cfg_dir.glob("*.cfg"), key=lambda p: p.name.lower()):
            for entry in _parse_bepinex_cfg(cfg_file):
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

    # 2. Live BepInExMCP bridge components/methods (when game & bridge are running or injected in tests)
    bridge_items = (live_bridge_fn or _probe_live_bepinex_bridge)(game, info)
    for idx, item in enumerate(bridge_items, start=1):
        if not isinstance(item, dict):
            continue
        ns = str(item.get("namespace") or "")
        cls = str(item.get("class_name") or item.get("component") or "UnknownComponent")
        member = str(item.get("member") or item.get("method") or f"member_{idx}")
        sig = str(item.get("signature") or f"{ns}.{cls}.{member}".lstrip("."))
        resolved = resolve_endpoint_classification(
            member,
            engine="unity",
            reference_candidates=[sig, f"{cls}.{member}", member],
            embedded_context=f"{cls} {item.get('description', '')}",
            embedded_label=str(item.get("name") or f"{cls}.{member}"),
            embedded_description=str(item.get("description") or f"Live BepInExMCP bridge member '{member}' on {cls}."),
            embedded_category=str(item.get("category")) if item.get("category") else None,
        )
        cur_v = str(item.get("current_value") if item.get("current_value") is not None else item.get("value", "Live"))
        def_v = str(item.get("default_value") if item.get("default_value") is not None else cur_v)
        endpoints.append(
            _make_endpoint(
                endpoint_id=str(item.get("id") or f"live_mcp:{cls}:{member}"),
                category=str(item.get("category") or resolved["category"]),
                name=resolved["simple_label"],
                simple_description=resolved["simple_description"],
                current_value=cur_v,
                default_value=def_v,
                delivery_type="Bridge",
                can_edit=True,
                value_type=resolved.get("value_type"),
                options=resolved.get("options"),
                min_val=resolved.get("min"),
                max_val=resolved.get("max"),
                step_val=resolved.get("step"),
                classification_source=resolved["source"],
                confidence=resolved["confidence"],
                advanced_details={
                    "source_kind": "live_bepinex_mcp",
                    "namespace": ns,
                    "class_name": cls,
                    "member": member,
                    "method_signature": sig,
                    "game_object_id": str(item.get("game_object_id") or ""),
                },
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

    # Scan Il2CppDumper dump.cs / script.json for [Header], [Tooltip], [Range] attributes & fields
    dump_records = parse_unity_dump_metadata(game_dir, tools_root=TOOLS_ROOT if has_dump_surface else None)
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

    if changed:
        payload = {
            "slug": safe_slug,
            "captured_at": existing.get("captured_at") or now_iso,
            "updated_at": now_iso,
            "defaults": stored_map,
        }
        atomic_write_json(defaults_path, payload)

    return endpoints, defaults_path


def scan_game_endpoints(
    game: dict[str, Any],
    *,
    defaults_dir: Path | None = None,
    reset_defaults: bool = False,
    live_bridge_fn: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]] | None = None,
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
            discovered.extend(_scan_unity_endpoints(game, game_dir, info, live_bridge_fn=live_bridge_fn))
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

    append_audit_log(
        action="endpoints.scan",
        status="ok",
        summary=f"Scanned {len(synced)} modding endpoint(s) for '{game_name}' ({'Unreal' if is_unreal else ('Unity' if is_unity else 'PC')})",
        details={"slug": slug, "count": len(synced), "defaults_path": str(defaults_path), "counts": counts},
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
        "total_count": len(synced),
        "modified_count": sum(1 for e in synced if e.get("is_modified")),
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

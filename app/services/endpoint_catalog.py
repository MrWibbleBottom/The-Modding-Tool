"""Self-learning endpoint knowledge cache, community schema importers (.CT/.lua/.cfg/.json), and portable per-AppID profiles."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.audit import append_audit_log
from app.core.config import APP_DATA_ROOT, atomic_write_json, load_json_sanitized
from app.engines.unreal import parse_unreal_ini_file
from app.services.discovery import make_slug
from app.services.endpoint_parsers import (
    _VALID_WORKFLOW_CATEGORIES,
    parse_bepinex_cfg,
    resolve_endpoint_classification,
)
from app.services.game_snapshots import ensure_within

_ALLOWED_IMPORT_EXTENSIONS = {".ct", ".cfg", ".lua", ".json", ".ini"}
_DTD_OR_ENTITY_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_DRIVE_OR_USER_PATH_RE = re.compile(r"(?:^[A-Za-z]:[/\\]|^\\\\|[/\\](?:Users|home)[/\\])", re.IGNORECASE)


def get_endpoint_knowledge_path(knowledge_path: Path | None = None, *, app_data_root: Path | None = None) -> Path:
    if knowledge_path is not None:
        return Path(knowledge_path)
    root = Path(app_data_root) if app_data_root is not None else APP_DATA_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root / "endpoint_knowledge.json"


def get_imported_endpoints_dir(imported_dir: Path | None = None, *, app_data_root: Path | None = None) -> Path:
    if imported_dir is not None:
        root = Path(imported_dir)
    else:
        base = Path(app_data_root) if app_data_root is not None else APP_DATA_ROOT
        root = base / "imported_endpoints"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_portable_profiles_dir(profiles_dir: Path | None = None, *, app_data_root: Path | None = None) -> Path:
    if profiles_dir is not None:
        root = Path(profiles_dir)
    else:
        base = Path(app_data_root) if app_data_root is not None else APP_DATA_ROOT
        root = base / "game_profiles"
    root.mkdir(parents=True, exist_ok=True)
    return root


def normalize_knowledge_symbol(symbol: str) -> str:
    return re.sub(r"\s+", " ", str(symbol or "").strip().lower())


def make_knowledge_key(engine: str, symbol: str) -> str:
    eng = str(engine or "generic").strip().lower() or "generic"
    return f"{eng}::{normalize_knowledge_symbol(symbol)}"


def load_endpoint_knowledge(
    *,
    knowledge_path: Path | None = None,
    app_data_root: Path | None = None,
) -> dict[str, Any]:
    target = get_endpoint_knowledge_path(knowledge_path, app_data_root=app_data_root)
    if target.is_file():
        data = load_json_sanitized(target, persist=False)
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    return {"version": 1, "entries": {}}


def lookup_endpoint_knowledge(
    engine: str,
    candidates: list[str] | tuple[str, ...],
    *,
    knowledge_path: Path | None = None,
    app_data_root: Path | None = None,
) -> dict[str, Any] | None:
    """Return learned classification for the first matching normalized `(engine, symbol)` candidate."""
    clean_cands = [str(c).strip() for c in candidates if str(c or "").strip()]
    if not clean_cands:
        return None
    cache = load_endpoint_knowledge(knowledge_path=knowledge_path, app_data_root=app_data_root)
    entries: dict[str, Any] = cache.get("entries") or {}
    eng_low = str(engine or "generic").strip().lower() or "generic"
    for cand in clean_cands:
        for eng_try in (eng_low, "generic") if eng_low != "generic" else ("generic",):
            k = make_knowledge_key(eng_try, cand)
            hit = entries.get(k)
            if isinstance(hit, dict) and hit.get("category") in _VALID_WORKFLOW_CATEGORIES:
                return {**hit, "matched_key": k}
    return None


def record_endpoint_knowledge(
    items: list[dict[str, Any]],
    *,
    knowledge_path: Path | None = None,
    app_data_root: Path | None = None,
) -> Path:
    """Persist one or more `(engine, normalized_symbol)` endpoint classifications atomically."""
    target = get_endpoint_knowledge_path(knowledge_path, app_data_root=app_data_root)
    cache = load_endpoint_knowledge(knowledge_path=target)
    entries: dict[str, Any] = dict(cache.get("entries") or {})
    now_iso = datetime.now(timezone.utc).isoformat()

    for item in items:
        sym = str(item.get("symbol") or "").strip()
        cat = str(item.get("category") or "").strip().lower()
        eng = str(item.get("engine") or "generic").strip().lower() or "generic"
        if not sym or cat not in _VALID_WORKFLOW_CATEGORIES:
            continue
        k = make_knowledge_key(eng, sym)
        existing = entries.get(k) if isinstance(entries.get(k), dict) else {}
        entries[k] = {
            "engine": eng,
            "symbol": sym,
            "normalized_symbol": normalize_knowledge_symbol(sym),
            "category": cat,
            "simple_label": str(item.get("simple_label") or existing.get("simple_label") or sym),
            "simple_description": str(item.get("simple_description") or existing.get("simple_description") or ""),
            "value_type": item.get("value_type") or existing.get("value_type"),
            "delivery_type": item.get("delivery_type") or existing.get("delivery_type"),
            "min": item.get("min") if item.get("min") is not None else existing.get("min"),
            "max": item.get("max") if item.get("max") is not None else existing.get("max"),
            "source": str(item.get("source") or "knowledge_cache"),
            "confidence": str(item.get("confidence") or "high"),
            "updated_at": now_iso,
        }

    atomic_write_json(
        target,
        {
            "version": 1,
            "updated_at": now_iso,
            "entries": entries,
        },
    )
    return target


def extract_endpoint_symbol(ep: dict[str, Any]) -> str:
    """Extract canonical symbol name used for knowledge cache lookup & portable AppID profiles."""
    adv = ep.get("advanced_details") or {}
    sym = (
        adv.get("method_signature")
        or adv.get("symbol")
        or (f"{adv['section']}.{adv['key']}" if adv.get("section") and adv.get("key") else "")
        or adv.get("key")
        or adv.get("asset_stem")
        or adv.get("member")
        or ep.get("name")
        or ep.get("id")
        or ""
    )
    return str(sym).strip()


def _infer_ce_value_type(var_type: str, has_script: bool) -> tuple[str, str, str]:
    vt_low = str(var_type or "").strip().lower()
    if has_script or "assembler" in vt_low or "script" in vt_low:
        return ("bool", ["Enabled", "Disabled"], "Disabled", "Mod File")  # type: ignore[return-value]
    if any(t in vt_low for t in ("byte", "int", "float", "double", "binary", "4 bytes", "8 bytes", "2 bytes")):
        return ("number", [], "0", "Bridge")  # type: ignore[return-value]
    return ("string", [], "", "Bridge")  # type: ignore[return-value]


def parse_cheat_engine_ct(xml_text: str, *, file_name: str = "table.ct") -> list[dict[str, Any]]:
    """Safely parse Cheat Engine `.CT` XML (`<CheatEntry>`) while forbidding DTD/external entity declarations."""
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise ValueError("Cheat Engine .CT content is empty.")
    if _DTD_OR_ENTITY_RE.search(xml_text):
        raise ValueError("Rejected .CT file: XML <!DOCTYPE> / <!ENTITY> declarations are forbidden.")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed .CT XML: {exc}") from exc

    stem = make_slug(Path(file_name).stem or "ct_table")
    parsed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _clean_desc(raw_desc: str) -> str:
        txt = str(raw_desc or "").strip()
        if len(txt) >= 2 and txt.startswith('"') and txt.endswith('"'):
            txt = txt[1:-1].strip()
        return txt

    def _walk_entries(parent_node: ET.Element, group_stack: list[str]) -> None:
        container = parent_node.find("CheatEntries")
        entry_nodes = container.findall("CheatEntry") if container is not None else parent_node.findall("CheatEntry")
        for node in entry_nodes:
            desc_raw = _clean_desc(node.findtext("Description", default=""))
            var_type = str(node.findtext("VariableType", default="")).strip()
            address = str(node.findtext("Address", default="")).strip()
            asm_script = str(node.findtext("AssemblerScript", default="")).strip()
            child_container = node.find("CheatEntries")
            has_child_entries = child_container is not None and len(child_container) > 0
            is_group_header = str(node.findtext("GroupHeader", default="")).strip() == "1" or (
                has_child_entries and not var_type and not address and not asm_script
            )

            next_stack = group_stack + ([desc_raw] if desc_raw else [])
            if has_child_entries:
                _walk_entries(node, next_stack)
            if is_group_header or not desc_raw:
                continue

            has_script = bool(asm_script)
            if has_script or "assembler" in var_type.lower():
                v_type, opts, def_val, delivery = "select", ["Enabled", "Disabled"], "Disabled", "Mod File"
            elif any(t in var_type.lower() for t in ("byte", "float", "double", "int")):
                v_type, opts, def_val, delivery = "number", [], "0", "Bridge"
            else:
                v_type, opts, def_val, delivery = "string", [], "", "Bridge"

            group_label = " / ".join(group_stack)
            symbol_path = f"{group_label} > {desc_raw}" if group_label else desc_raw
            ep_base = f"ce_ct:{stem}:{make_slug(symbol_path)}"
            ep_id = ep_base
            suffix = 2
            while ep_id in seen_ids:
                ep_id = f"{ep_base}_{suffix}"
                suffix += 1
            seen_ids.add(ep_id)

            ctx = f"{group_label} {var_type} {address}".strip()
            resolved = resolve_endpoint_classification(
                desc_raw,
                engine="generic",
                reference_candidates=[desc_raw, symbol_path],
                embedded_source="cheat_engine_ct",
                embedded_context=ctx,
                embedded_label=desc_raw,
                embedded_description=(
                    f"Cheat Engine entry '{desc_raw}'"
                    + (f" (Group: {group_label})" if group_label else "")
                    + (f" [{var_type}]" if var_type else "")
                    + (f" @ {address}" if address else "")
                    + "."
                ),
                embedded_value_type=v_type,
                embedded_options=opts if opts else None,
                embedded_default=def_val,
            )

            parsed.append({
                "id": ep_id,
                "category": resolved["category"],
                "name": resolved["simple_label"] or desc_raw,
                "simple_description": resolved["simple_description"],
                "value_type": v_type,
                "options": opts,
                "min": resolved.get("min"),
                "max": resolved.get("max"),
                "step": resolved.get("step"),
                "default_value": def_val,
                "current_value": def_val,
                "delivery_type": delivery,
                "can_edit": False if not has_script else True,
                "classification_source": resolved["source"],
                "confidence": resolved["confidence"],
                "advanced_details": {
                    "source_kind": "cheat_engine_ct",
                    "file_name": Path(file_name).name,
                    "symbol": symbol_path,
                    "group_path": group_label,
                    "variable_type": var_type,
                    "address": address,
                    "has_assembler_script": has_script,
                    "assembler_script_preview": asm_script[:400] if asm_script else "",
                },
            })

    _walk_entries(root, [])
    return parsed


def _parse_ue4ss_lua_schema(lua_text: str, *, file_name: str = "main.lua", engine: str = "unreal") -> list[dict[str, Any]]:
    stem = make_slug(Path(file_name).stem or "ue4ss_mod")
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # 1. Config variables in config.lua / main.lua (e.g. `Config.DamageMultiplier = 1.5` or `local bGodMode = false`)
    lines = lua_text.splitlines()
    comment_buf: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            comment_buf.clear()
            continue
        if stripped.startswith("--"):
            comment_buf.append(stripped.lstrip("-").strip())
            continue
        m_var = re.match(
            r"^(?:local\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(true|false|-?\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*')\s*(?:--.*)?$",
            stripped,
            re.IGNORECASE,
        )
        if m_var:
            sym, raw_val = m_var.group(1), m_var.group(2).strip().strip("\"'")
            key_short = sym.rsplit(".", 1)[-1]
            doc = " ".join(comment_buf).strip()
            v_type = "bool" if raw_val.lower() in ("true", "false") else ("number" if re.fullmatch(r"-?\d+(?:\.\d+)?", raw_val) else "string")
            resolved = resolve_endpoint_classification(
                key_short,
                engine=engine,
                reference_candidates=[sym, key_short],
                embedded_source="ue4ss_lua_schema",
                embedded_context=f"{file_name} {sym} {doc}",
                embedded_label=key_short,
                embedded_description=doc or f"UE4SS Lua schema option '{sym}' from {Path(file_name).name}.",
                embedded_value_type=v_type,
                embedded_default=raw_val,
            )
            ep_id = f"ue4ss_schema:{stem}:{make_slug(sym)}"
            if ep_id not in seen_ids:
                seen_ids.add(ep_id)
                entries.append({
                    "id": ep_id,
                    "category": resolved["category"],
                    "name": resolved["simple_label"],
                    "simple_description": resolved["simple_description"],
                    "value_type": v_type,
                    "options": ["true", "false"] if v_type == "bool" else [],
                    "min": resolved.get("min"),
                    "max": resolved.get("max"),
                    "default_value": raw_val,
                    "current_value": raw_val,
                    "delivery_type": "Mod File",
                    "can_edit": False,
                    "classification_source": resolved["source"],
                    "confidence": resolved["confidence"],
                    "advanced_details": {
                        "source_kind": "ue4ss_lua_schema",
                        "file_name": Path(file_name).name,
                        "symbol": sym,
                        "key": key_short,
                    },
                })
        comment_buf.clear()

    # 2. RegisterHook("...") hooks in main.lua
    for h_idx, hook_sig in enumerate(re.findall(r'RegisterHook\(\s*["\']([^"\']+)["\']', lua_text), start=1):
        ep_id = f"ue4ss_schema:{stem}:hook_{h_idx}"
        if ep_id in seen_ids:
            continue
        seen_ids.add(ep_id)
        resolved = resolve_endpoint_classification(
            hook_sig.split(":")[-1],
            engine=engine,
            reference_candidates=[hook_sig],
            embedded_source="ue4ss_lua_schema",
            embedded_context=f"{file_name} {hook_sig}",
            embedded_label=f"Hook: {hook_sig.split(':')[-1]}",
            embedded_description=f"Imported UE4SS Lua hook '{hook_sig}' from {Path(file_name).name}.",
            embedded_value_type="select",
            embedded_options=["Enabled", "Disabled"],
            embedded_default="Enabled",
        )
        entries.append({
            "id": ep_id,
            "category": resolved["category"],
            "name": resolved["simple_label"],
            "simple_description": resolved["simple_description"],
            "value_type": "select",
            "options": ["Enabled", "Disabled"],
            "default_value": "Enabled",
            "current_value": "Enabled",
            "delivery_type": "Mod File",
            "can_edit": False,
            "classification_source": resolved["source"],
            "confidence": resolved["confidence"],
            "advanced_details": {
                "source_kind": "ue4ss_lua_schema",
                "file_name": Path(file_name).name,
                "symbol": hook_sig,
                "method_signature": hook_sig,
            },
        })
    return entries


def _parse_cfg_text(cfg_text: str, *, file_name: str, engine: str, tmp_hint_path: Path | None = None) -> list[dict[str, Any]]:
    import tempfile

    if tmp_hint_path is not None and tmp_hint_path.is_file():
        raw_entries = parse_bepinex_cfg(tmp_hint_path)
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", encoding="utf-8", delete=False) as tf:
            tf.write(cfg_text)
            temp_p = Path(tf.name)
        try:
            raw_entries = parse_bepinex_cfg(temp_p)
        finally:
            temp_p.unlink(missing_ok=True)

    stem = Path(file_name).stem
    out: list[dict[str, Any]] = []
    for entry in raw_entries:
        ep_id = f"imported_cfg:{make_slug(stem)}:{entry['section']}:{entry['key']}"
        choices = entry.get("choices") or []
        resolved = resolve_endpoint_classification(
            entry["key"],
            engine=engine,
            reference_candidates=[f"{entry['section']}.{entry['key']}", entry["key"]],
            embedded_source=entry.get("metadata_source") or "cfg_metadata",
            embedded_context=f"{stem} {entry['section']} {entry['simple_description']}",
            embedded_label=f"{entry['key']} ({stem})",
            embedded_description=entry["simple_description"],
            embedded_value_type=entry.get("value_type"),
            embedded_options=list(choices) if choices else None,
            embedded_min=entry.get("min_value"),
            embedded_max=entry.get("max_value"),
            embedded_default=entry["default_value"],
        )
        out.append({
            "id": ep_id,
            "category": resolved["category"],
            "name": resolved["simple_label"],
            "simple_description": resolved["simple_description"],
            "value_type": resolved.get("value_type") or "string",
            "options": resolved.get("options") or [],
            "min": resolved.get("min"),
            "max": resolved.get("max"),
            "step": resolved.get("step"),
            "default_value": str(resolved["default_value"] if resolved["default_value"] is not None else entry["default_value"]),
            "current_value": entry["value"],
            "delivery_type": resolved.get("delivery_type") or "Config",
            "can_edit": False,
            "classification_source": resolved["source"],
            "confidence": resolved["confidence"],
            "advanced_details": {
                "source_kind": "community_cfg_schema",
                "file_name": Path(file_name).name,
                "section": entry["section"],
                "key": entry["key"],
                "symbol": f"{entry['section']}.{entry['key']}",
                "setting_type": entry.get("setting_type", ""),
            },
        })
    return out


def _parse_json_schema_or_profile(
    json_text: str,
    *,
    file_name: str,
    engine: str,
    source_path: Path | None = None,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON schema file '{file_name}': {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in '{file_name}'.")

    entries: list[dict[str, Any]] = []
    # Case 1: Portable endpoint profile export (`{"endpoints": [...]}`)
    if isinstance(payload.get("endpoints"), list):
        for idx, item in enumerate(payload["endpoints"], start=1):
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or item.get("method_signature") or item.get("key") or item.get("name") or f"item_{idx}").strip()
            cat = str(item.get("category") or "").strip().lower()
            if cat not in _VALID_WORKFLOW_CATEGORIES:
                cat = resolve_endpoint_classification(sym, engine=engine)["category"]
            ep_id = str(item.get("id") or f"profile:{make_slug(sym)}")
            def_v = str(item.get("default_value") if item.get("default_value") is not None else "")
            entries.append({
                "id": ep_id,
                "category": cat,
                "name": str(item.get("name") or sym),
                "simple_description": str(item.get("simple_description") or f"Imported endpoint '{sym}' from profile."),
                "value_type": str(item.get("value_type") or "string"),
                "options": list(item.get("options") or []),
                "min": item.get("min"),
                "max": item.get("max"),
                "step": item.get("step"),
                "default_value": def_v,
                "current_value": def_v,
                "delivery_type": str(item.get("delivery_type") or "Config"),
                "can_edit": False,
                "classification_source": "community_profile",
                "confidence": "high",
                "advanced_details": {
                    "source_kind": "portable_profile",
                    "file_name": Path(file_name).name,
                    "symbol": sym,
                    "section": str(item.get("section") or ""),
                    "key": str(item.get("key") or ""),
                    "namespace": str(item.get("namespace") or ""),
                    "class_name": str(item.get("class_name") or ""),
                    "member": str(item.get("member") or ""),
                    "method_signature": str(item.get("method_signature") or sym),
                },
            })
        return entries

    # Case 2: Thunderstore / Nexus manifest.json
    mod_name = str(payload.get("name") or Path(file_name).stem).strip()
    mod_desc = str(payload.get("description") or "").strip()
    mod_ver = str(payload.get("version_number") or payload.get("version") or "1.0.0").strip()
    resolved = resolve_endpoint_classification(
        mod_name,
        engine=engine,
        reference_candidates=[mod_name],
        embedded_source="thunderstore_manifest",
        embedded_context=f"{mod_name} {mod_desc}",
        embedded_label=f"Mod Schema: {mod_name} v{mod_ver}",
        embedded_description=mod_desc or f"Imported mod manifest schema for '{mod_name}' ({mod_ver}).",
    )
    entries.append({
        "id": f"mod_manifest:{make_slug(mod_name)}",
        "category": resolved["category"],
        "name": resolved["simple_label"],
        "simple_description": resolved["simple_description"],
        "value_type": "string",
        "options": [],
        "default_value": mod_ver,
        "current_value": mod_ver,
        "delivery_type": "Mod File",
        "can_edit": False,
        "classification_source": resolved["source"],
        "confidence": resolved["confidence"],
        "advanced_details": {
            "source_kind": "thunderstore_manifest",
            "file_name": Path(file_name).name,
            "symbol": mod_name,
            "version": mod_ver,
        },
    })

    # If imported from a local mod directory containing a sibling .cfg, ingest that .cfg too
    if source_path is not None and source_path.parent.is_dir():
        for sib_cfg in sorted(source_path.parent.rglob("*.cfg"))[:5]:
            entries.extend(_parse_cfg_text(sib_cfg.read_text(encoding="utf-8", errors="ignore"), file_name=sib_cfg.name, engine=engine, tmp_hint_path=sib_cfg))

    return entries


def validate_community_import_path(raw_path: str, game_dir: Path | None) -> Path:
    """Resolve and validate a community import path safely, enforcing `ensure_within()` for game-relative or local paths."""
    txt = str(raw_path or "").strip()
    if not txt:
        raise ValueError("Empty file path provided.")
    p = Path(txt)
    if ".." in p.parts:
        # Enforce strict confinement check against game_dir (always fails on traversal outside root)
        anchor = game_dir if (game_dir is not None and game_dir.is_dir()) else Path.cwd()
        ensure_within((anchor / p) if not p.is_absolute() else p, anchor)

    if not p.is_absolute():
        if game_dir is None or not game_dir.is_dir():
            raise ValueError("Relative import paths require a valid active game directory.")
        resolved = ensure_within(game_dir / p, game_dir)
    else:
        resolved = p.resolve()
        if game_dir is not None and game_dir.is_dir() and resolved.is_relative_to(game_dir.resolve()):
            ensure_within(resolved, game_dir)

    if resolved.suffix.lower() not in _ALLOWED_IMPORT_EXTENSIONS:
        raise ValueError(f"Unsupported file extension '{resolved.suffix}'. Allowed: {', '.join(sorted(_ALLOWED_IMPORT_EXTENSIONS))}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Import file not found: {resolved}")
    return resolved


def import_community_artifact(
    game: dict[str, Any],
    *,
    file_path: str = "",
    content: str = "",
    filename: str = "",
    app_data_root: Path | None = None,
    knowledge_path: Path | None = None,
) -> dict[str, Any]:
    """Import `.ct`, `.cfg`, `.lua`, `.ini`, or `.json` community endpoints into the game catalog & knowledge cache."""
    slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    rt_low = str(game.get("runtime") or "").lower()
    engine = "unreal" if "unreal" in rt_low else ("unity" if ("unity" in rt_low or "bepinex" in rt_low) else "generic")
    game_dir_raw = str(game.get("game_dir") or "").strip()
    game_dir = Path(game_dir_raw) if game_dir_raw else None

    source_path: Path | None = None
    if str(file_path or "").strip():
        source_path = validate_community_import_path(file_path, game_dir)
        fname = source_path.name
        raw_text = source_path.read_text(encoding="utf-8-sig", errors="ignore")
    else:
        fname = Path(str(filename or "").strip()).name
        if not fname:
            raise ValueError("Provide either 'file_path' or 'filename' with 'content'.")
        if Path(fname).suffix.lower() not in _ALLOWED_IMPORT_EXTENSIONS:
            raise ValueError(f"Unsupported filename extension '{Path(fname).suffix}'.")
        raw_text = str(content or "")
        if not raw_text.strip():
            raise ValueError("Uploaded file content is empty.")

    ext = Path(fname).suffix.lower()
    if ext == ".ct":
        imported_items = parse_cheat_engine_ct(raw_text, file_name=fname)
    elif ext == ".lua":
        imported_items = _parse_ue4ss_lua_schema(raw_text, file_name=fname, engine=engine)
    elif ext == ".cfg":
        imported_items = _parse_cfg_text(raw_text, file_name=fname, engine=engine, tmp_hint_path=source_path)
    elif ext == ".json":
        imported_items = _parse_json_schema_or_profile(raw_text, file_name=fname, engine=engine, source_path=source_path)
    else:
        # Plain .ini schema
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".ini", encoding="utf-8", delete=False) as tf:
            tf.write(raw_text)
            temp_ini = Path(tf.name)
        try:
            sections = parse_unreal_ini_file(temp_ini)
        finally:
            temp_ini.unlink(missing_ok=True)
        imported_items = []
        for sec, kv in sections.items():
            for k, v in kv.items():
                res = resolve_endpoint_classification(k, engine=engine, reference_candidates=[k, f"{sec}.{k}"], embedded_context=f"{fname} {sec}")
                imported_items.append({
                    "id": f"imported_ini:{make_slug(Path(fname).stem)}:{sec}:{k}",
                    "category": res["category"],
                    "name": res["simple_label"],
                    "simple_description": res["simple_description"] or f"Imported INI entry '{k}' under [{sec}].",
                    "value_type": res.get("value_type") or "string",
                    "options": res.get("options") or [],
                    "min": res.get("min"),
                    "max": res.get("max"),
                    "default_value": str(v),
                    "current_value": str(v),
                    "delivery_type": "Config",
                    "can_edit": False,
                    "classification_source": res["source"],
                    "confidence": res["confidence"],
                    "advanced_details": {"source_kind": "community_ini_schema", "file_name": fname, "section": sec, "key": k, "symbol": f"{sec}.{k}"},
                })

    # Merge into game's imported_endpoints/<slug>.json
    imp_file = get_imported_endpoints_dir(app_data_root=app_data_root) / f"{slug}.json"
    existing_list: list[dict[str, Any]] = []
    if imp_file.is_file():
        loaded = load_json_sanitized(imp_file, persist=False)
        if isinstance(loaded, dict) and isinstance(loaded.get("endpoints"), list):
            existing_list = [e for e in loaded["endpoints"] if isinstance(e, dict)]

    by_id: dict[str, dict[str, Any]] = {str(e["id"]): e for e in existing_list if e.get("id")}
    for item in imported_items:
        by_id[str(item["id"])] = item

    atomic_write_json(
        imp_file,
        {
            "slug": slug,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "endpoints": list(by_id.values()),
        },
    )

    # Record classified imported items into self-learning knowledge cache
    knowledge_records = []
    for item in imported_items:
        if item.get("category") and item.get("category") != "uncategorized":
            sym = extract_endpoint_symbol(item)
            knowledge_records.append({
                "engine": engine,
                "symbol": sym,
                "category": item["category"],
                "simple_label": item.get("name"),
                "simple_description": item.get("simple_description"),
                "value_type": item.get("value_type"),
                "delivery_type": item.get("delivery_type"),
                "min": item.get("min"),
                "max": item.get("max"),
                "source": "community_import",
                "confidence": item.get("confidence") or "high",
            })
    k_path = record_endpoint_knowledge(knowledge_records, knowledge_path=knowledge_path, app_data_root=app_data_root)

    append_audit_log(
        action="endpoints.import_community",
        status="ok",
        summary=f"Imported {len(imported_items)} endpoint(s) from '{fname}' into '{slug}'",
        details={"slug": slug, "file_name": fname, "imported_count": len(imported_items), "knowledge_path": str(k_path)},
    )
    return {
        "status": "ok",
        "slug": slug,
        "file_name": fname,
        "imported_count": len(imported_items),
        "imported_endpoints": imported_items,
        "knowledge_path": str(k_path),
    }


def load_game_imported_endpoints(
    slug: str,
    *,
    app_data_root: Path | None = None,
) -> list[dict[str, Any]]:
    imp_file = get_imported_endpoints_dir(app_data_root=app_data_root) / f"{make_slug(slug)}.json"
    if not imp_file.is_file():
        return []
    loaded = load_json_sanitized(imp_file, persist=False)
    if isinstance(loaded, dict) and isinstance(loaded.get("endpoints"), list):
        return [e for e in loaded["endpoints"] if isinstance(e, dict)]
    return []


def categorize_endpoints_in_catalog(
    game: dict[str, Any],
    updates: list[dict[str, Any]],
    report_endpoints: list[dict[str, Any]],
    *,
    app_data_root: Path | None = None,
    knowledge_path: Path | None = None,
) -> dict[str, Any]:
    """Persist user/triage category & description overrides into endpoint_knowledge.json and imported endpoints."""
    slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    rt_low = str(game.get("runtime") or "").lower()
    engine = "unreal" if "unreal" in rt_low else ("unity" if ("unity" in rt_low or "bepinex" in rt_low) else "generic")
    ep_by_id = {str(ep.get("id")): ep for ep in report_endpoints}

    knowledge_items: list[dict[str, Any]] = []
    updated_ids: list[str] = []

    imp_file = get_imported_endpoints_dir(app_data_root=app_data_root) / f"{slug}.json"
    imported_list = load_game_imported_endpoints(slug, app_data_root=app_data_root)
    imported_map = {str(e.get("id")): e for e in imported_list if e.get("id")}
    imp_changed = False

    for upd in updates:
        ep_id = str(upd.get("endpoint_id") or "").strip()
        new_cat = str(upd.get("category") or "").strip().lower()
        new_desc = upd.get("simple_description")
        new_label = upd.get("simple_label")
        if not ep_id:
            continue
        if new_cat not in _VALID_WORKFLOW_CATEGORIES:
            raise ValueError(f"Unsupported endpoint category '{new_cat}'.")
        ep = ep_by_id.get(ep_id) or imported_map.get(ep_id)
        if ep is None:
            raise KeyError(f"Endpoint not found: {ep_id}")

        sym = str(upd.get("symbol") or extract_endpoint_symbol(ep)).strip()
        knowledge_items.append({
            "engine": engine,
            "symbol": sym,
            "category": new_cat,
            "simple_label": str(new_label).strip() if new_label else ep.get("name"),
            "simple_description": str(new_desc).strip() if new_desc else ep.get("simple_description"),
            "value_type": ep.get("value_type"),
            "delivery_type": ep.get("delivery_type"),
            "min": ep.get("min"),
            "max": ep.get("max"),
            "source": "knowledge_cache",
            "confidence": "high",
        })
        # Also record bare key/member alias if sym is dotted (e.g. `Internal.Xyz` -> also match `Xyz`? No: keep exact scoped sym so unrelated keys don't collide; and if adv.get("key") differs from sym and ep_id is unique, key is only needed when section.key matches!)
        updated_ids.append(ep_id)
        if ep_id in imported_map:
            imported_map[ep_id]["category"] = new_cat
            if new_desc:
                imported_map[ep_id]["simple_description"] = str(new_desc).strip()
            imported_map[ep_id]["classification_source"] = "knowledge_cache"
            imported_map[ep_id]["confidence"] = "high"
            imp_changed = True

    k_path = record_endpoint_knowledge(knowledge_items, knowledge_path=knowledge_path, app_data_root=app_data_root)
    if imp_changed:
        atomic_write_json(
            imp_file,
            {
                "slug": slug,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "endpoints": list(imported_map.values()),
            },
        )

    append_audit_log(
        action="endpoints.categorize",
        status="ok",
        summary=f"Categorized {len(updated_ids)} endpoint(s) into knowledge cache for '{slug}'",
        details={"slug": slug, "updated_ids": updated_ids, "knowledge_path": str(k_path)},
    )
    return {
        "status": "ok",
        "slug": slug,
        "updated_ids": updated_ids,
        "knowledge_path": str(k_path),
    }


def _strip_local_paths(val: Any) -> Any:
    if isinstance(val, str):
        if _DRIVE_OR_USER_PATH_RE.search(val):
            return ""
        return val
    return val


def export_portable_endpoint_profile(
    game: dict[str, Any],
    report: dict[str, Any],
    *,
    save_to_profiles_dir: bool = True,
    app_data_root: Path | None = None,
) -> dict[str, Any]:
    """Build a sanitized portable endpoint profile keyed by AppID/slug with zero local filesystem paths."""
    slug = make_slug(str(game.get("id") or report.get("slug") or game.get("name") or "game"))
    app_id = str(game.get("steam_appid") or slug).strip() or slug
    clean_endpoints: list[dict[str, Any]] = []

    for ep in report.get("endpoints", []):
        if ep.get("category") == "error_logs":
            continue
        adv = ep.get("advanced_details") or {}
        sym = _strip_local_paths(extract_endpoint_symbol(ep)) or str(ep.get("name") or ep.get("id"))
        clean_endpoints.append({
            "id": str(ep.get("id") or ""),
            "symbol": sym,
            "category": str(ep.get("category") or "uncategorized"),
            "name": _strip_local_paths(str(ep.get("name") or sym)),
            "simple_description": _strip_local_paths(str(ep.get("simple_description") or "")),
            "value_type": str(ep.get("value_type") or "string"),
            "options": list(ep.get("options") or []),
            "min": ep.get("min"),
            "max": ep.get("max"),
            "step": ep.get("step"),
            "default_value": _strip_local_paths(str(ep.get("default_value") or "")),
            "delivery_type": str(ep.get("delivery_type") or "Config"),
            "section": _strip_local_paths(str(adv.get("section") or "")),
            "key": _strip_local_paths(str(adv.get("key") or "")),
            "namespace": _strip_local_paths(str(adv.get("namespace") or "")),
            "class_name": _strip_local_paths(str(adv.get("class_name") or "")),
            "member": _strip_local_paths(str(adv.get("member") or "")),
            "method_signature": _strip_local_paths(str(adv.get("method_signature") or "")),
            "classification_source": str(ep.get("classification_source") or "heuristic"),
            "confidence": str(ep.get("confidence") or "medium"),
        })

    profile = {
        "schema": "themoddingtool.endpoint_profile.v1",
        "app_id": app_id,
        "slug": slug,
        "game_name": str(game.get("name") or report.get("game_name") or slug),
        "engine": str(report.get("engine") or "generic"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_count": len(clean_endpoints),
        "endpoints": clean_endpoints,
    }
    if save_to_profiles_dir:
        safe_id = make_slug(app_id) or slug
        prof_path = get_portable_profiles_dir(app_data_root=app_data_root) / f"{safe_id}.json"
        atomic_write_json(prof_path, profile)
    return profile

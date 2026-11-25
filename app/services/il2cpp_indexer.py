"""Automated Il2CppDumper runner and structured IL2CPP symbol, enum, static-root, and method indexer."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.audit import append_audit_log
from app.core.config import APP_DATA_ROOT, TOOLS_ROOT, atomic_write_json, load_json_sanitized
from app.services.discovery import make_slug
from app.services.endpoint_parsers import _parse_numeric_bound, is_excluded_unity_namespace

_PRIMITIVE_PATCH_RETURN_TYPES: frozenset[str] = frozenset({
    "int",
    "int32",
    "int64",
    "float",
    "single",
    "double",
    "bool",
    "boolean",
})

_GAMEPLAY_METHOD_KEYWORDS: tuple[str, ...] = (
    "damage",
    "health",
    "hp",
    "cost",
    "actionpoint",
    "cooldown",
    "spawn",
    "loot",
    "gold",
    "armor",
    "move",
    "shield",
    "mana",
    "energy",
    "currency",
    "xp",
)

_SINGLETON_MEMBER_NAMES: frozenset[str] = frozenset({
    "instance",
    "current",
    "context",
    "main",
    "active",
})

_BOILERPLATE_METHOD_NAMES: frozenset[str] = frozenset({
    ".ctor",
    ".cctor",
    "finalize",
    "tostring",
    "gethashcode",
    "equals",
    "gettype",
    "memberwiseclone",
})


def get_il2cpp_dump_dir(slug: str, *, app_data_root: Path | None = None) -> Path:
    safe_slug = make_slug(slug) or "game"
    base = Path(app_data_root) if app_data_root is not None else APP_DATA_ROOT
    return base / "dumps" / safe_slug


def get_il2cpp_index_path(slug: str, *, app_data_root: Path | None = None) -> Path:
    return get_il2cpp_dump_dir(slug, app_data_root=app_data_root) / "il2cpp_index.json"


def load_il2cpp_index(slug: str, *, app_data_root: Path | None = None) -> dict[str, Any] | None:
    idx_path = get_il2cpp_index_path(slug, app_data_root=app_data_root)
    if not idx_path.is_file():
        return None
    loaded = load_json_sanitized(idx_path, persist=False)
    return loaded if isinstance(loaded, dict) else None


def find_il2cpp_dumper_exe(*, tools_root: Path | None = None) -> Path:
    root = Path(tools_root) if tools_root is not None else TOOLS_ROOT
    direct_candidates = [
        root / "Il2CppDumper" / "Il2CppDumper.exe",
        root / "Il2CppDumper.exe",
    ]
    for cand in direct_candidates:
        if cand.is_file():
            return cand
    if root.is_dir():
        for found in sorted(root.rglob("Il2CppDumper.exe"), key=lambda p: str(p).lower()):
            if found.is_file():
                return found
    raise FileNotFoundError(
        f"Il2CppDumper.exe not found under {root}. Install Il2CppDumper in the Setup tab first."
    )


def locate_unity_il2cpp_inputs(game_dir: Path) -> tuple[Path, Path]:
    if not game_dir.is_dir():
        raise FileNotFoundError(f"Game directory does not exist: {game_dir}")

    ga = game_dir / "GameAssembly.dll"
    if not ga.is_file():
        raise FileNotFoundError(f"GameAssembly.dll not found in {game_dir} (game may not be Unity IL2CPP).")

    metadata_path: Path | None = None
    for data_dir in sorted(game_dir.glob("*_Data"), key=lambda p: p.name.lower()):
        cand = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        if cand.is_file():
            metadata_path = cand
            break
    if metadata_path is None:
        for cand in sorted(game_dir.rglob("global-metadata.dat"), key=lambda p: str(p).lower()):
            if cand.is_file():
                metadata_path = cand
                break
    if metadata_path is None:
        raise FileNotFoundError(f"global-metadata.dat not found under {game_dir}\\*_Data\\il2cpp_data\\Metadata\\.")
    return (ga, metadata_path)


def _is_excluded_index_namespace(full_or_ns: str) -> bool:
    clean = str(full_or_ns or "").strip().lower()
    if not clean:
        return False
    if clean == "mono":
        return True
    return is_excluded_unity_namespace(clean)


def build_il2cpp_index(
    slug: str,
    dump_dir: Path | str,
    *,
    app_data_root: Path | None = None,
) -> dict[str, Any]:
    """Parse `dump.cs` (and optional `script.json`) into a structured, non-engine IL2CPP symbol/enum/method index."""
    d_dir = Path(dump_dir)
    dump_cs = d_dir / "dump.cs"
    if not dump_cs.is_file():
        raise FileNotFoundError(f"dump.cs not found in {d_dir}")

    enums: dict[str, dict[str, int]] = {}
    static_roots: dict[str, dict[str, Any]] = {}
    classes: dict[str, dict[str, Any]] = {}
    patchable_methods: list[dict[str, Any]] = []
    seen_method_sigs: set[str] = set()

    cur_ns = ""
    cur_type_name = ""
    cur_type_full = ""
    cur_type_kind = ""
    cur_base_types: list[str] = []
    cur_is_serializable = False
    cur_is_scriptable = False
    cur_excluded = False
    in_enum = False
    brace_depth = 0
    type_start_depth = 0

    pending_type_serializable = False
    pending_header = ""
    pending_tooltip = ""
    pending_range: tuple[float | int | None, float | int | None] = (None, None)
    pending_rva = ""

    class_decl_re = re.compile(
        r"^(?:public|internal|private|protected|sealed|abstract|static|\s)*(class|struct)\s+([A-Za-z0-9_`]+)(?:<[^>]+>)?(?:\s*:\s*([^/{]+))?"
    )
    enum_decl_re = re.compile(
        r"^(?:public|internal|private|protected|\s)*enum\s+([A-Za-z0-9_`]+)"
    )
    enum_member_re = re.compile(
        r"^public\s+const\s+[A-Za-z0-9_.`<>]+\s+([A-Za-z0-9_]+)\s*=\s*(-?(?:0x[0-9a-fA-F]+|\d+))\s*;"
    )
    field_decl_re = re.compile(
        r"^(public|private|protected|internal)\s+(?:(static)\s+)?(?:(readonly|const)\s+)?([A-Za-z0-9_.<>\[\],?\s]+?)\s+([A-Za-z0-9_<>]+)\s*(?:;|=|\{)"
    )
    prop_decl_re = re.compile(
        r"^(public|private|protected|internal)\s+(?:(static|virtual|override|new)\s+)*([A-Za-z0-9_.<>\[\],?\s]+?)\s+([A-Za-z0-9_]+)\s*\{\s*get\s*;"
    )
    method_decl_re = re.compile(
        r"^(public|private|protected|internal)\s+(?:(?:static|virtual|override|abstract|sealed|extern|new)\s+)*([A-Za-z0-9_.<>\[\],?]+)\s+([A-Za-z0-9_.]+)\s*\(([^)]*)\)"
    )

    def _clear_member_attrs() -> None:
        nonlocal pending_header, pending_tooltip, pending_range, pending_rva
        pending_header = ""
        pending_tooltip = ""
        pending_range = (None, None)
        pending_rva = ""

    def _ensure_static_root() -> dict[str, Any]:
        if cur_type_full not in static_roots:
            static_roots[cur_type_full] = {
                "namespace": cur_ns,
                "class_name": cur_type_name,
                "kind": cur_type_kind,
                "static_fields": [],
                "singleton_members": [],
            }
        return static_roots[cur_type_full]

    def _ensure_class_entry() -> dict[str, Any]:
        if cur_type_full not in classes:
            classes[cur_type_full] = {
                "namespace": cur_ns,
                "class_name": cur_type_name,
                "kind": cur_type_kind or "class",
                "base_types": list(cur_base_types),
                "is_serializable": cur_is_serializable,
                "is_scriptable_object": cur_is_scriptable,
                "fields": [],
            }
        return classes[cur_type_full]

    raw_text = dump_cs.read_text(encoding="utf-8-sig", errors="ignore")
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("// Namespace:"):
            cur_ns = line.split(":", 1)[1].strip()
            cur_type_name = ""
            cur_type_full = ""
            cur_excluded = _is_excluded_index_namespace(cur_ns)
            in_enum = False
            pending_type_serializable = False
            _clear_member_attrs()
            continue

        m_rva = re.match(r"^//\s*RVA:\s*(0x[0-9a-fA-F]+)", line)
        if m_rva:
            pending_rva = m_rva.group(1)
            continue

        if line.startswith("["):
            if re.search(r"Serializable\b", line):
                pending_type_serializable = True
            m_hdr = re.search(r'Header\(\s*"([^"]+)"\s*\)', line)
            if m_hdr:
                pending_header = m_hdr.group(1).strip()
            m_tip = re.search(r'Tooltip\(\s*"([^"]+)"\s*\)', line)
            if m_tip:
                pending_tooltip = m_tip.group(1).strip()
            m_rng = re.search(r"Range\(\s*(-?\d+(?:\.\d+)?)f?\s*,\s*(-?\d+(?:\.\d+)?)f?\s*\)", line, re.IGNORECASE)
            if m_rng:
                pending_range = (_parse_numeric_bound(m_rng.group(1)), _parse_numeric_bound(m_rng.group(2)))
            continue

        m_enum = enum_decl_re.match(line)
        if m_enum and not in_enum and not cur_type_name:
            cur_type_name = m_enum.group(1).split("`", 1)[0].strip()
            cur_type_full = f"{cur_ns}.{cur_type_name}".lstrip(".")
            cur_type_kind = "enum"
            in_enum = True
            cur_excluded = _is_excluded_index_namespace(cur_type_full)
            if not cur_excluded:
                enums.setdefault(cur_type_full, {})
            type_start_depth = brace_depth
            pending_type_serializable = False
            _clear_member_attrs()
            if "{" in line:
                brace_depth += line.count("{") - line.count("}")
            continue

        m_cls = class_decl_re.match(line)
        if m_cls and not cur_type_name:
            cur_type_kind = m_cls.group(1)
            cur_type_name = m_cls.group(2).split("`", 1)[0].strip()
            cur_type_full = f"{cur_ns}.{cur_type_name}".lstrip(".")
            base_raw = m_cls.group(3) or ""
            cur_base_types = [b.strip().split("<", 1)[0].strip() for b in base_raw.split(",") if b.strip()]
            cur_is_serializable = pending_type_serializable
            cur_is_scriptable = any("scriptableobject" in b.lower() for b in cur_base_types)
            cur_excluded = _is_excluded_index_namespace(cur_type_full)
            in_enum = False
            type_start_depth = brace_depth
            pending_type_serializable = False
            _clear_member_attrs()
            if not cur_excluded and (cur_is_serializable or cur_is_scriptable):
                _ensure_class_entry()
            if "{" in line:
                brace_depth += line.count("{") - line.count("}")
            continue

        # Track braces so we exit enum/class scope cleanly
        if line == "{":
            brace_depth += 1
            continue
        if line == "}":
            brace_depth = max(0, brace_depth - 1)
            if cur_type_name and brace_depth <= type_start_depth:
                cur_type_name = ""
                cur_type_full = ""
                cur_type_kind = ""
                in_enum = False
                cur_excluded = False
            _clear_member_attrs()
            continue

        if cur_excluded or not cur_type_name:
            if not line.startswith("//"):
                _clear_member_attrs()
            continue

        if in_enum:
            m_em = enum_member_re.match(line)
            if m_em:
                mem_name, raw_num = m_em.group(1).strip(), m_em.group(2).strip()
                if mem_name != "value__":
                    try:
                        enums[cur_type_full][mem_name] = int(raw_num, 0)
                    except ValueError:
                        pass
            _clear_member_attrs()
            continue

        # Inside class / struct: check property (`{ get; set; }`), method, and field declarations
        m_prop = prop_decl_re.match(line)
        if m_prop:
            modifiers_txt = line.split(m_prop.group(3), 1)[0].lower()
            is_static = "static" in modifiers_txt.split()
            p_type = m_prop.group(3).strip()
            p_name = m_prop.group(4).strip()
            has_set = bool(re.search(r"\bset\s*;", line))
            if is_static or p_name.lower() in _SINGLETON_MEMBER_NAMES:
                sr = _ensure_static_root()
                info_obj = {"name": p_name, "type": p_type, "kind": "property"}
                if is_static:
                    sr["static_fields"].append(info_obj)
                if p_name.lower() in _SINGLETON_MEMBER_NAMES:
                    sr["singleton_members"].append(info_obj)
            elif has_set:
                cls_ent = _ensure_class_entry()
                cls_ent["fields"].append({
                    "name": p_name,
                    "type": p_type,
                    "kind": "property",
                    "header": pending_header,
                    "tooltip": pending_tooltip,
                    "min": pending_range[0],
                    "max": pending_range[1],
                })
            _clear_member_attrs()
            continue

        m_meth = method_decl_re.match(line)
        if m_meth and not line.endswith(";"):
            ret_type = m_meth.group(2).strip()
            m_name = m_meth.group(3).strip()
            params_raw = m_meth.group(4).strip()
            if m_name.lower() not in _BOILERPLATE_METHOD_NAMES and not m_name.startswith(("get_", "set_", "add_", "remove_")):
                ret_tail = ret_type.rsplit(".", 1)[-1].lower()
                is_prim_ret = ret_tail in _PRIMITIVE_PATCH_RETURN_TYPES
                text_check = f"{m_name} {params_raw}".lower()
                has_kw = any(kw in text_check for kw in _GAMEPLAY_METHOD_KEYWORDS)
                if is_prim_ret or has_kw:
                    sig = f"{cur_type_full}.{m_name}({params_raw})"
                    if sig not in seen_method_sigs:
                        seen_method_sigs.add(sig)
                        patchable_methods.append({
                            "class_full_name": cur_type_full,
                            "class_name": cur_type_name,
                            "namespace": cur_ns,
                            "name": m_name,
                            "return_type": ret_type,
                            "parameters": params_raw,
                            "signature": sig,
                            "rva": pending_rva,
                        })
            _clear_member_attrs()
            continue

        m_fld = field_decl_re.match(line)
        if m_fld:
            is_static = bool(m_fld.group(2))
            is_ro = bool(m_fld.group(3))
            f_type = m_fld.group(4).strip()
            f_name = m_fld.group(5).strip()
            if f_name.endswith("k__BackingField") or "<" in f_name:
                _clear_member_attrs()
                continue
            if is_static:
                sr = _ensure_static_root()
                f_info = {"name": f_name, "type": f_type, "kind": "field"}
                sr["static_fields"].append(f_info)
                if f_name.lower() in _SINGLETON_MEMBER_NAMES:
                    sr["singleton_members"].append(f_info)
            elif not is_ro:
                cls_ent = _ensure_class_entry()
                cls_ent["fields"].append({
                    "name": f_name,
                    "type": f_type,
                    "kind": "field",
                    "header": pending_header,
                    "tooltip": pending_tooltip,
                    "min": pending_range[0],
                    "max": pending_range[1],
                })
            _clear_member_attrs()
            continue

        if not line.startswith("//"):
            _clear_member_attrs()

    # Optional enrichment from Il2CppDumper script.json (adds ScriptMethod entries if not already in dump.cs)
    script_json = d_dir / "script.json"
    if script_json.is_file():
        try:
            if script_json.stat().st_size <= 25 * 1024 * 1024:
                sdata = json.loads(script_json.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(sdata, dict):
                    for item in sdata.get("ScriptMethod", []):
                        if not isinstance(item, dict):
                            continue
                        raw_nm = str(item.get("Name") or "").strip()
                        if "$$" not in raw_nm:
                            continue
                        cls_part, meth_nm = raw_nm.split("$$", 1)
                        if _is_excluded_index_namespace(cls_part):
                            continue
                        if meth_nm.lower() in _BOILERPLATE_METHOD_NAMES:
                            continue
                        if any(kw in meth_nm.lower() for kw in _GAMEPLAY_METHOD_KEYWORDS):
                            if not any(m["class_full_name"] == cls_part and m["name"] == meth_nm for m in patchable_methods):
                                ns_part, _, cls_only = cls_part.rpartition(".")
                                patchable_methods.append({
                                    "class_full_name": cls_part,
                                    "class_name": cls_only or cls_part,
                                    "namespace": ns_part,
                                    "name": meth_nm,
                                    "return_type": "unknown",
                                    "parameters": str(item.get("Signature") or ""),
                                    "signature": f"{cls_part}.{meth_nm}",
                                    "rva": hex(item["Address"]) if isinstance(item.get("Address"), int) else "",
                                })
        except Exception:
            pass

    safe_slug = make_slug(slug) or "game"
    index_path = get_il2cpp_index_path(safe_slug, app_data_root=app_data_root)
    counts = {
        "enums": len(enums),
        "static_roots": len(static_roots),
        "classes": len(classes),
        "patchable_methods": len(patchable_methods),
    }
    payload = {
        "schema": "themoddingtool.il2cpp_index.v1",
        "slug": safe_slug,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dump_dir": str(d_dir),
        "counts": counts,
        "enums": enums,
        "static_roots": static_roots,
        "classes": classes,
        "patchable_methods": patchable_methods,
    }
    atomic_write_json(index_path, payload)
    return payload


def run_il2cpp_dumper(
    game: dict[str, Any],
    *,
    tools_root: Path | None = None,
    app_data_root: Path | None = None,
) -> dict[str, Any]:
    """Run headless `Il2CppDumper.exe` for a Unity IL2CPP game and build `dumps/<slug>/il2cpp_index.json`."""
    slug = make_slug(str(game.get("id") or game.get("name") or "game"))
    game_dir = Path(str(game.get("game_dir") or "").strip())

    dumper_exe = find_il2cpp_dumper_exe(tools_root=tools_root)
    ga_dll, metadata_dat = locate_unity_il2cpp_inputs(game_dir)

    dump_dir = get_il2cpp_dump_dir(slug, app_data_root=app_data_root)
    dump_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [str(dumper_exe), str(ga_dll), str(metadata_dat), str(dump_dir)],
        cwd=str(dumper_exe.parent),
        input="\n\n\n",
        text=True,
        capture_output=True,
        timeout=240,
    )

    dump_cs = dump_dir / "dump.cs"
    script_json = dump_dir / "script.json"
    if proc.returncode != 0 and not dump_cs.is_file():
        err_out = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        append_audit_log(
            action="il2cpp.dump",
            status="error",
            summary=f"Il2CppDumper failed for '{slug}': {err_out[:200]}",
            details={"slug": slug, "returncode": proc.returncode},
        )
        raise RuntimeError(f"Il2CppDumper exited with code {proc.returncode}: {err_out[-600:]}")

    if not dump_cs.is_file():
        raise FileNotFoundError(f"Il2CppDumper finished without producing {dump_cs}")

    index = build_il2cpp_index(slug, dump_dir, app_data_root=app_data_root)
    index_path = get_il2cpp_index_path(slug, app_data_root=app_data_root)
    counts = index["counts"]
    append_audit_log(
        action="il2cpp.dump",
        status="ok",
        summary=(
            f"Indexed IL2CPP dump for '{slug}': {counts['classes']} class(es), "
            f"{counts['enums']} enum(s), {counts['static_roots']} static root(s), "
            f"{counts['patchable_methods']} method(s)"
        ),
        details={"slug": slug, "dump_dir": str(dump_dir), "counts": counts},
    )
    return {
        "status": "ok",
        "slug": slug,
        "dump_dir": str(dump_dir),
        "dump_cs_path": str(dump_cs),
        "script_json_path": str(script_json) if script_json.is_file() else "",
        "index_path": str(index_path),
        "counts": counts,
        "index": index,
    }

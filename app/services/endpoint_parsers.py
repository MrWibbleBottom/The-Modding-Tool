"""Offline deterministic metadata parsers, reference lookup, and token+type heuristic classifier."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Any

ENGINE_REF_JSON: Path = Path(__file__).resolve().parent.parent / "data" / "engine_endpoints_reference.json"

_VALID_WORKFLOW_CATEGORIES = {
    "game_systems",
    "player_character",
    "engine_settings",
    "cheats",
    "qol",
    "error_logs",
    "uncategorized",
}

_CATEGORY_WEIGHTED_KEYWORDS: dict[str, dict[str, float]] = {
    "error_logs": {
        "error": 3.0,
        "errors": 3.0,
        "exception": 3.0,
        "crash": 3.0,
        "stacktrace": 3.0,
        "log": 2.2,
        "logs": 2.2,
        "logfile": 3.0,
        "logging": 2.5,
        "logoutput": 3.0,
        "diagnostic": 2.5,
        "diagnostics": 2.5,
        "console": 1.6,
        "warn": 2.0,
        "fatal": 2.5,
        "ue4ss": 1.2,
    },
    "cheats": {
        "cheat": 3.5,
        "cheats": 3.5,
        "god": 2.6,
        "godmode": 3.5,
        "noclip": 3.5,
        "invincible": 3.0,
        "achieve": 2.6,
        "achievement": 3.0,
        "achievements": 3.0,
        "mission": 2.4,
        "quest": 2.4,
        "quests": 2.4,
        "reveal": 2.5,
        "fogofwar": 3.0,
        "mapreveal": 3.0,
        "unlock": 2.4,
        "allowconsole": 3.0,
        "teleport": 2.8,
        "infinite": 2.5,
    },
    "player_character": {
        "hero": 2.4,
        "player": 2.0,
        "character": 2.2,
        "champion": 2.2,
        "party": 2.0,
        "health": 2.6,
        "maxhealth": 2.8,
        "hp": 2.5,
        "armor": 2.8,
        "shield": 2.8,
        "block": 2.4,
        "mana": 2.6,
        "energy": 2.5,
        "stamina": 2.6,
        "stat": 2.0,
        "stats": 2.2,
        "money": 2.8,
        "gold": 2.8,
        "currency": 2.8,
        "coin": 2.5,
        "coins": 2.5,
        "xp": 2.6,
        "exp": 2.2,
        "experience": 2.5,
        "level": 2.2,
        "actionpoint": 2.8,
        "actionpoints": 2.8,
        "ap": 2.2,
        "movement": 2.4,
        "move": 1.8,
        "initiative": 2.4,
        "playerpiece": 2.5,
    },
    "game_systems": {
        "damage": 2.6,
        "attack": 2.2,
        "ammo": 2.8,
        "loot": 2.8,
        "drop": 2.2,
        "drops": 2.4,
        "spawn": 2.4,
        "spawns": 2.4,
        "spawnrate": 2.8,
        "enemy": 2.4,
        "enemies": 2.4,
        "unit": 1.8,
        "inventory": 2.6,
        "day": 1.8,
        "night": 1.8,
        "timeofday": 2.8,
        "cooldown": 2.6,
        "range": 2.2,
        "crit": 2.6,
        "critical": 2.6,
        "card": 2.5,
        "cards": 2.5,
        "hand": 2.3,
        "deck": 2.5,
        "turn": 2.2,
        "combat": 2.5,
        "weapon": 2.4,
        "weapons": 2.4,
        "multiplier": 1.4,
        "difficulty": 2.4,
        "balance": 2.2,
        "gameplay": 2.0,
    },
    "qol": {
        "fov": 3.0,
        "hud": 2.8,
        "ui": 2.2,
        "camera": 2.2,
        "vsync": 2.8,
        "hdr": 2.5,
        "blur": 2.6,
        "motionblur": 3.0,
        "dof": 2.8,
        "fullscreen": 2.8,
        "mouse": 2.4,
        "smoothing": 2.2,
        "sensitivity": 2.6,
        "subtitle": 2.8,
        "subtitles": 2.8,
        "aspect": 2.4,
        "crosshair": 2.6,
        "input": 1.8,
    },
    "engine_settings": {
        "lod": 2.8,
        "foliage": 2.8,
        "draw": 2.0,
        "distance": 1.8,
        "viewdistance": 3.0,
        "shadow": 2.6,
        "shadows": 2.6,
        "texture": 2.5,
        "textures": 2.5,
        "shader": 2.6,
        "cache": 2.2,
        "pool": 2.2,
        "fps": 2.8,
        "framerate": 2.8,
        "frame": 1.8,
        "scalability": 3.0,
        "quality": 2.4,
        "render": 2.4,
        "renderer": 2.6,
        "streaming": 2.5,
        "vram": 2.8,
        "gamma": 2.4,
        "resolution": 2.4,
        "fog": 2.2,
        "volumetric": 2.4,
        "antialiasing": 2.8,
        "particle": 2.4,
        "particles": 2.4,
        "visual": 2.0,
        "visuals": 2.0,
        "preset": 1.8,
        "density": 2.0,
    },
}

_UNREAL_SECTION_RULES: dict[str, tuple[str, str, str]] = {
    "/script/engine.renderersettings": (
        "engine_settings",
        "Renderer Pipeline Settings",
        "Unreal Engine renderer default setting in Engine.ini [/Script/Engine.RendererSettings].",
    ),
    "systemsettings": (
        "engine_settings",
        "Engine System CVar",
        "Unreal runtime console variable under [SystemSettings].",
    ),
    "consolevariables": (
        "engine_settings",
        "Console Variable Override",
        "Unreal runtime console variable override under [ConsoleVariables].",
    ),
    "/script/engine.inputsettings": (
        "qol",
        "Input & Control Settings",
        "Unreal Engine mouse/controller input preference in [/Script/Engine.InputSettings].",
    ),
    "/script/engine.gameusersettings": (
        "engine_settings",
        "Display & Quality User Setting",
        "Unreal GameUserSettings preference.",
    ),
    "scalabilitygroups": (
        "engine_settings",
        "Scalability Group Preset",
        "Unreal ScalabilityGroups graphics tier setting (0 Low to 4 Cinematic).",
    ),
    "/script/engine.localplayer": (
        "qol",
        "Viewport & FOV Setting",
        "Unreal local viewport/FOV scaling setting in [/Script/Engine.LocalPlayer].",
    ),
}

_UNREAL_DATATABLE_RULES: dict[str, tuple[str, str, str]] = {
    "dt_weapons": (
        "game_systems",
        "Weapon Stats DataTable",
        "Unreal DataTable asset defining weapon damage, fire rate, range, and recoil rows.",
    ),
    "dt_enemystats": (
        "game_systems",
        "Enemy Stats DataTable",
        "Unreal DataTable asset defining enemy HP, damage, speed, and spawn scaling rows.",
    ),
    "dt_lootdrops": (
        "game_systems",
        "Loot Drop Table Asset",
        "Unreal DataTable asset defining item drop weights, rarities, and loot pool tables.",
    ),
    "dt_playerstats": (
        "player_character",
        "Player Stats DataTable",
        "Unreal DataTable asset defining hero base health, stamina, leveling curves, and stats.",
    ),
    "dt_inventory": (
        "game_systems",
        "Inventory & Item DataTable",
        "Unreal DataTable asset defining item stack limits, slot capacity, and equipment definitions.",
    ),
    "dt_quests": (
        "cheats",
        "Quest & Mission Progression DataTable",
        "Unreal DataTable asset defining mission objectives, rewards, and quest unlock stages.",
    ),
}


@lru_cache(maxsize=2)
def load_engine_reference_entries(ref_path: Path | None = None) -> list[dict[str, Any]]:
    """Load curated engine endpoint definitions from `app/data/engine_endpoints_reference.json`."""
    target = Path(ref_path) if ref_path is not None else ENGINE_REF_JSON
    if not target.is_file():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        return [dict(e) for e in raw["entries"] if isinstance(e, dict) and e.get("pattern")]
    if isinstance(raw, list):
        return [dict(e) for e in raw if isinstance(e, dict) and e.get("pattern")]
    return []


def lookup_engine_reference(
    candidates: list[str] | tuple[str, ...],
    *,
    engine: str | None = None,
    ref_path: Path | None = None,
) -> dict[str, Any] | None:
    """Resolve an identifier against `engine_endpoints_reference.json` (exact match first, then wildcard)."""
    clean_cands = [str(c).strip() for c in candidates if str(c or "").strip()]
    if not clean_cands:
        return None
    entries = load_engine_reference_entries(ref_path)
    eng_low = str(engine or "").strip().lower()

    def _engine_matches(entry: dict[str, Any]) -> bool:
        ent_eng = str(entry.get("engine") or "").strip().lower()
        return not eng_low or not ent_eng or ent_eng == eng_low or eng_low == "generic"

    # Pass 1: exact case-insensitive match
    for cand in clean_cands:
        c_low = cand.lower()
        for entry in entries:
            if not _engine_matches(entry):
                continue
            pat = str(entry["pattern"]).strip()
            if "*" not in pat and "?" not in pat and pat.lower() == c_low:
                return {**entry, "match_mode": "exact", "confidence": "high", "source": "reference_dict"}

    # Pass 2: wildcard pattern match (ordered most-specific first by non-wildcard chars)
    wildcard_entries = sorted(
        (e for e in entries if ("*" in str(e["pattern"]) or "?" in str(e["pattern"])) and _engine_matches(e)),
        key=lambda e: len(str(e["pattern"]).replace("*", "").replace("?", "")),
        reverse=True,
    )
    for cand in clean_cands:
        c_low = cand.lower()
        for entry in wildcard_entries:
            pat = str(entry["pattern"]).strip().lower()
            if fnmatchcase(c_low, pat):
                return {**entry, "match_mode": "wildcard", "confidence": "high", "source": "reference_dict"}
    return None


def split_identifier_tokens(identifier: str) -> list[str]:
    """Split PascalCase, camelCase, snake_case, dotted, or Hungarian-prefixed identifiers into lowercase tokens."""
    raw = str(identifier or "").strip()
    if not raw:
        return []
    # Normalize common separators into spaces first
    parts = re.split(r"[.\-:/\\\[\]\(\)\s_]+", raw)
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        # Strip Unreal single-letter Hungarian prefix when directly followed by an uppercase word (e.g. bGodMode, fDamageScale)
        cleaned = part
        if len(cleaned) >= 3 and cleaned[0] in ("b", "f", "i", "n", "s") and cleaned[1].isupper() and cleaned[2:].isascii():
            # Keep single-word acronyms like iOS out; valid property starts with [A-Z][a-z]
            if cleaned[2].islower():
                cleaned = cleaned[1:]
        sub = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", cleaned)
        sub = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", sub)
        for tok in sub.split():
            low = tok.lower().strip()
            if low:
                tokens.append(low)
    return tokens


def classify_identifier_heuristic(
    identifier: str,
    *,
    context_text: str = "",
    value_type: str | None = None,
) -> dict[str, Any]:
    """Score tokenized words + value_type against category rules, routing low-confidence unknowns to 'uncategorized'."""
    id_tokens = split_identifier_tokens(identifier)
    ctx_tokens = split_identifier_tokens(context_text)
    scores: dict[str, float] = {cat: 0.0 for cat in _CATEGORY_WEIGHTED_KEYWORDS}
    matched_tokens: dict[str, list[str]] = {cat: [] for cat in _CATEGORY_WEIGHTED_KEYWORDS}

    def _apply_tokens(toks: list[str], weight_mult: float) -> None:
        # Check single tokens and joined adjacent bigrams (e.g. view + distance -> viewdistance)
        all_grams = list(toks) + [f"{toks[i]}{toks[i + 1]}" for i in range(len(toks) - 1)]
        seen_per_cat: set[tuple[str, str]] = set()
        for gram in all_grams:
            for cat, kw_map in _CATEGORY_WEIGHTED_KEYWORDS.items():
                if gram in kw_map and (cat, gram) not in seen_per_cat:
                    scores[cat] += kw_map[gram] * weight_mult
                    matched_tokens[cat].append(gram)
                    seen_per_cat.add((cat, gram))

    _apply_tokens(id_tokens, 1.0)
    if ctx_tokens:
        _apply_tokens(ctx_tokens, 0.7)

    # Subtle value-type nudges only as tie-breakers when keywords already matched
    vt_low = str(value_type or "").lower()
    if vt_low == "diagnostic":
        scores["error_logs"] += 1.5

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_cat, best_score = ranked[0]

    if best_score >= 4.0 or (best_score >= 2.5 and len(matched_tokens[best_cat]) >= 2):
        return {
            "category": best_cat,
            "confidence": "high",
            "score": round(best_score, 2),
            "tokens": id_tokens,
            "matched_keywords": matched_tokens[best_cat],
            "source": "heuristic",
        }
    if best_score >= 2.0:
        return {
            "category": best_cat,
            "confidence": "medium",
            "score": round(best_score, 2),
            "tokens": id_tokens,
            "matched_keywords": matched_tokens[best_cat],
            "source": "heuristic",
        }
    return {
        "category": "uncategorized",
        "confidence": "low",
        "score": round(best_score, 2),
        "tokens": id_tokens,
        "matched_keywords": [],
        "source": "heuristic",
    }


def _parse_numeric_bound(raw: str) -> float | int | None:
    txt = str(raw or "").strip().rstrip("fF")
    if not txt:
        return None
    try:
        fval = float(txt)
        return int(fval) if fval.is_integer() and "." not in txt else fval
    except ValueError:
        return None


def parse_bepinex_cfg(cfg_path: Path) -> list[dict[str, Any]]:
    """Parse BepInEx `.cfg` sections, `##` comments, Setting type, Default value, Acceptable range/values."""
    entries: list[dict[str, Any]] = []
    section = "General"
    desc_lines: list[str] = []
    setting_type = ""
    default_val = ""
    acceptable_vals = ""
    min_val: float | int | None = None
    max_val: float | int | None = None
    choices: list[str] = []

    def _reset_pending() -> None:
        nonlocal setting_type, default_val, acceptable_vals, min_val, max_val, choices
        desc_lines.clear()
        setting_type = ""
        default_val = ""
        acceptable_vals = ""
        min_val = None
        max_val = None
        choices = []

    for raw in cfg_path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            _reset_pending()
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip() or "General"
            _reset_pending()
            continue
        if line.startswith("##"):
            desc_lines.append(line.lstrip("#").strip())
            continue
        if line.startswith("#"):
            meta_txt = line.lstrip("#").strip()
            low_meta = meta_txt.lower()
            if low_meta.startswith("setting type:"):
                setting_type = meta_txt.split(":", 1)[1].strip()
            elif low_meta.startswith("default value:"):
                default_val = meta_txt.split(":", 1)[1].strip()
            elif low_meta.startswith("acceptable value range:"):
                range_str = meta_txt.split(":", 1)[1].strip()
                acceptable_vals = range_str
                m_rng = re.search(
                    r"from\s+(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)",
                    range_str,
                    re.IGNORECASE,
                ) or re.search(r"(-?\d+(?:\.\d+)?)\s*(?:\.\.|to|-)\s*(-?\d+(?:\.\d+)?)", range_str, re.IGNORECASE)
                if m_rng:
                    min_val = _parse_numeric_bound(m_rng.group(1))
                    max_val = _parse_numeric_bound(m_rng.group(2))
            elif low_meta.startswith("acceptable values:"):
                acc_str = meta_txt.split(":", 1)[1].strip()
                acceptable_vals = acc_str
                m_rng = re.search(
                    r"from\s+(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)",
                    acc_str,
                    re.IGNORECASE,
                )
                if m_rng:
                    min_val = _parse_numeric_bound(m_rng.group(1))
                    max_val = _parse_numeric_bound(m_rng.group(2))
                elif not low_meta.endswith("multiple values can be set at the same time."):
                    parsed_choices = [c.strip() for c in acc_str.split(",") if c.strip()]
                    if len(parsed_choices) >= 2:
                        choices = parsed_choices
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            key_clean = k.strip()
            val_clean = v.strip()
            doc_text = " ".join(desc_lines).strip()
            st_low = setting_type.lower()
            v_type: str | None = None
            if choices:
                v_type = "select"
            elif "bool" in st_low:
                v_type = "bool"
            elif any(t in st_low for t in ("int", "single", "float", "double", "byte", "decimal")):
                v_type = "number"
            elif st_low:
                v_type = "string"

            has_cfg_meta = bool(doc_text or setting_type or default_val or min_val is not None or choices)
            entries.append({
                "section": section,
                "key": key_clean,
                "value": val_clean,
                "default_value": default_val if default_val != "" else val_clean,
                "setting_type": setting_type,
                "value_type": v_type,
                "acceptable_values": acceptable_vals,
                "min_value": min_val,
                "max_value": max_val,
                "choices": list(choices),
                "simple_description": doc_text or f"BepInEx config '{key_clean}' under [{section}].",
                "description": doc_text or f"BepInEx config '{key_clean}' under [{section}].",
                "has_explicit_doc": bool(doc_text),
                "metadata_source": "cfg_metadata" if has_cfg_meta else "",
            })
            _reset_pending()
    return entries


def parse_unreal_property_identifier(key: str, section: str = "") -> dict[str, Any]:
    """Inspect Unreal Hungarian-prefixed property names (`bGodMode`, `fDamageMultiplier`, `iMaxAmmo`) & `.ini` sections."""
    clean_key = str(key or "").strip()
    sec_low = str(section or "").strip().lower()
    inferred_type: str | None = None
    inferred_opts: list[str] | None = None
    hungarian_hit = False

    if len(clean_key) >= 3 and clean_key[1].isupper() and clean_key[2].islower():
        prefix = clean_key[0]
        if prefix == "b":
            inferred_type = "bool"
            inferred_opts = ["True", "False"]
            hungarian_hit = True
        elif prefix == "f":
            inferred_type = "number"
            hungarian_hit = True
        elif prefix in ("i", "n"):
            inferred_type = "number"
            hungarian_hit = True
        elif prefix == "s":
            inferred_type = "string"
            hungarian_hit = True

    tokens = split_identifier_tokens(clean_key)
    human_label = " ".join(tok.upper() if tok in ("fov", "fps", "lod", "hdr", "xp", "hp", "ui", "hud") else tok.capitalize() for tok in tokens) or clean_key

    sec_rule = _UNREAL_SECTION_RULES.get(sec_low)
    heur = classify_identifier_heuristic(clean_key, context_text=section, value_type=inferred_type)

    if heur["confidence"] in ("high", "medium"):
        category = heur["category"]
        confidence = "high" if (hungarian_hit or heur["confidence"] == "high") else "medium"
        source = "unreal_metadata" if (hungarian_hit or sec_rule is not None) else "heuristic"
    elif sec_rule is not None:
        category = sec_rule[0]
        confidence = "medium"
        source = "unreal_metadata"
    else:
        category = "uncategorized"
        confidence = "low"
        source = "heuristic"

    if sec_rule:
        desc = f"{sec_rule[1]} '{clean_key}' ({human_label}) under [{section}]."
    elif hungarian_hit:
        desc = f"Unreal typed property '{clean_key}' ({human_label})."
    else:
        desc = f"Unreal INI setting '{clean_key}' under [{section}]." if section else f"Unreal setting '{clean_key}'."

    return {
        "category": category,
        "confidence": confidence,
        "source": source,
        "simple_label": human_label,
        "simple_description": desc,
        "value_type": inferred_type,
        "options": inferred_opts,
        "hungarian_typed": hungarian_hit,
    }


def classify_unreal_datatable(asset_name_or_stem: str) -> dict[str, Any]:
    """Classify an Unreal `DataTable` asset (`DT_Weapons`, `DT_EnemyStats`, `DT_LootDrops`, `DT_PlayerStats`, etc.)."""
    stem = Path(str(asset_name_or_stem)).stem
    low = stem.lower()
    if low in _UNREAL_DATATABLE_RULES:
        cat, label, desc = _UNREAL_DATATABLE_RULES[low]
        return {
            "category": cat,
            "confidence": "high",
            "source": "unreal_metadata",
            "simple_label": f"{label} ({stem})",
            "simple_description": desc,
            "asset_stem": stem,
        }
    stripped = re.sub(r"^dt_", "", stem, flags=re.IGNORECASE)
    heur = classify_identifier_heuristic(stripped or stem, context_text="datatable asset")
    tokens = split_identifier_tokens(stripped or stem)
    human = " ".join(t.capitalize() for t in tokens) or stem
    return {
        "category": heur["category"],
        "confidence": heur["confidence"],
        "source": "unreal_metadata" if heur["confidence"] != "low" else "heuristic",
        "simple_label": f"DataTable: {human} ({stem})",
        "simple_description": f"Unreal DataTable asset '{stem}' detected in game content.",
        "asset_stem": stem,
    }


def scan_unreal_datatable_assets(game_dir: Path, *, limit: int = 30) -> list[dict[str, Any]]:
    """Discover Unreal `DT_*` DataTable files (.uasset/.json/.csv) under `Content/` or game root."""
    if not game_dir.is_dir():
        return []
    found: list[dict[str, Any]] = []
    seen_stems: set[str] = set()
    search_roots = list(game_dir.glob("*/Content"))
    if not search_roots:
        search_roots = [game_dir]

    for root in search_roots:
        if not root.is_dir():
            continue
        for ext in ("*.uasset", "*.json", "*.csv"):
            for fp in sorted(root.rglob(f"DT_{ext}"), key=lambda p: str(p).lower()):
                if len(found) >= limit:
                    return found
                stem_key = fp.stem.lower()
                if stem_key in seen_stems:
                    continue
                seen_stems.add(stem_key)
                meta = classify_unreal_datatable(fp.stem)
                found.append({**meta, "file_path": str(fp), "file_name": fp.name})
    return found


_EXCLUDED_UNITY_NAMESPACE_PREFIXES: tuple[str, ...] = (
    "unityengine",
    "unity.",
    "system",
    "tmpro",
    "il2cppinterop",
    "bepinex",
    "mono.",
    "microsoft.",
)


def is_excluded_unity_namespace(type_or_namespace: str) -> bool:
    low = str(type_or_namespace or "").strip().lower()
    if not low:
        return False
    return any(low == pref.rstrip(".") or low.startswith(pref + ".") or low.startswith(pref) for pref in _EXCLUDED_UNITY_NAMESPACE_PREFIXES)


def parse_unity_dump_metadata(
    game_dir: Path,
    *,
    tools_root: Path | None = None,
    max_entries: int = 60,
) -> list[dict[str, Any]]:
    """Parse `dump.cs` (`[Header]`, `[Tooltip]`, `[Range]`, fields) and `script.json` across game interop and tools dirs."""
    records: list[dict[str, Any]] = []
    seen_sigs: set[str] = set()

    dump_candidates = [
        game_dir / "dump.cs",
        game_dir / "BepInEx" / "interop" / "dump.cs",
        game_dir / "Il2CppDumper" / "dump.cs",
    ]
    if tools_root is not None:
        tr = Path(tools_root)
        dump_candidates.extend([
            tr / "Il2CppDumper" / "dump.cs",
            tr / "dump.cs",
        ])

    interesting_tok_re = re.compile(
        r"(Health|MaxHealth|Armor|Shield|Block|ActionPoint|Mana|Energy|Stamina|Cooldown|Movement|Crit|"
        r"MaxAmmo|Damage|Currency|Gold|Coin|Experience|XP|Level|Card|Hand|Deck|Initiative|Turn|"
        r"RevealMap|SpawnRate|Multiplier|Loot|Stats)",
        re.IGNORECASE,
    )

    for dump_file in dump_candidates:
        if not dump_file.is_file():
            continue
        text = dump_file.read_bytes()[: 1536 * 1024].decode("utf-8", errors="ignore")
        cur_ns = ""
        cur_cls = ""
        pending_header = ""
        pending_tooltip = ""
        pending_range: tuple[float | int | None, float | int | None] = (None, None)

        for raw_line in text.splitlines():
            if len(records) >= max_entries:
                break
            line = raw_line.strip()
            if not line:
                continue

            m_ns = re.match(r"^//\s*Namespace:\s*(.*)$", line)
            if m_ns:
                cur_ns = m_ns.group(1).strip()
                pending_header = ""
                pending_tooltip = ""
                pending_range = (None, None)
                continue

            m_cls = re.match(r"^(?:public|internal|private|protected|sealed|abstract|\s)*(?:class|struct)\s+([A-Za-z0-9_`]+)", line)
            if m_cls:
                cur_cls = m_cls.group(1).split("`", 1)[0].strip()
                pending_header = ""
                pending_tooltip = ""
                pending_range = (None, None)
                continue

            if is_excluded_unity_namespace(cur_ns):
                continue

            if line.startswith("["):
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

            # Field or property declaration (e.g. `public float attackSpeed; // 0x18` or `public int MaxHP { get; set; }`)
            m_fld = re.match(
                r"^(?:public|private|protected|internal)\s+(?:static\s+|readonly\s+)?([A-Za-z0-9_.\<\>\[\]]+)\s+([A-Za-z0-9_]+)\s*(?:;|=|\{)",
                line,
            )
            if not m_fld:
                # Reset pending attributes if a non-attribute, non-field declaration line is reached
                if not line.startswith("//") and line not in ("{", "}"):
                    pending_header = ""
                    pending_tooltip = ""
                    pending_range = (None, None)
                continue

            fld_type, fld_name = m_fld.group(1).strip(), m_fld.group(2).strip()
            has_attr = bool(pending_header or pending_tooltip or pending_range[0] is not None)
            if not has_attr and not (cur_cls and interesting_tok_re.search(fld_name)):
                pending_header = ""
                pending_tooltip = ""
                pending_range = (None, None)
                continue

            cls_full = f"{cur_ns}.{cur_cls}".lstrip(".") if cur_cls else "Global"
            sig = f"{cls_full}.{fld_name}".lstrip(".")
            if sig in seen_sigs:
                pending_header = ""
                pending_tooltip = ""
                pending_range = (None, None)
                continue
            seen_sigs.add(sig)

            ft_low = fld_type.lower()
            v_type = (
                "bool"
                if ft_low in ("bool", "boolean")
                else ("number" if ft_low in ("int", "int32", "int64", "float", "single", "double", "byte") or pending_range[0] is not None else "string")
            )
            desc = pending_tooltip or (f"{pending_header}: Unity field '{fld_name}' ({fld_type}) on {cls_full}." if pending_header else f"Discovered IL2CPP dump field/property '{fld_name}' ({fld_type}) on '{cls_full}'.")
            records.append({
                "file_path": str(dump_file),
                "namespace": cur_ns,
                "class_name": cur_cls or "Global",
                "member": fld_name,
                "field_type": fld_type,
                "signature": sig,
                "header": pending_header,
                "tooltip": pending_tooltip,
                "min_value": pending_range[0],
                "max_value": pending_range[1],
                "value_type": v_type,
                "simple_label": f"{cur_cls or 'Dump'}.{fld_name}",
                "simple_description": desc,
                "has_attributes": has_attr,
                "metadata_source": "unity_dump",
            })
            pending_header = ""
            pending_tooltip = ""
            pending_range = (None, None)

    # Also check Il2CppDumper script.json metadata if present
    script_candidates = [
        game_dir / "script.json",
        game_dir / "BepInEx" / "interop" / "script.json",
        game_dir / "Il2CppDumper" / "script.json",
    ]
    if tools_root is not None:
        tr = Path(tools_root)
        script_candidates.extend([
            tr / "Il2CppDumper" / "script.json",
            tr / "script.json",
        ])

    for s_file in script_candidates:
        if len(records) >= max_entries or not s_file.is_file():
            continue
        try:
            if s_file.stat().st_size > 6 * 1024 * 1024:
                continue
            data = json.loads(s_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("ScriptMethod", []):
            if len(records) >= max_entries:
                break
            if not isinstance(item, dict):
                continue
            raw_name = str(item.get("Name") or "").strip()
            if "$$" not in raw_name:
                continue
            cls_part, meth_part = raw_name.split("$$", 1)
            if is_excluded_unity_namespace(cls_part):
                continue
            if not interesting_tok_re.search(meth_part):
                continue
            sig = f"{cls_part}.{meth_part}"
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            ns_part, _, cls_only = cls_part.rpartition(".")
            records.append({
                "file_path": str(s_file),
                "namespace": ns_part,
                "class_name": cls_only or cls_part,
                "member": meth_part,
                "field_type": "method",
                "signature": sig,
                "header": "",
                "tooltip": "",
                "min_value": None,
                "max_value": None,
                "value_type": "string",
                "simple_label": f"{cls_only or cls_part}.{meth_part} (Script)",
                "simple_description": f"Discovered Il2CppDumper script.json method '{meth_part}' on '{cls_part}'.",
                "has_attributes": False,
                "metadata_source": "unity_dump",
            })

    return records


def resolve_endpoint_classification(
    identifier: str,
    *,
    engine: str = "generic",
    reference_candidates: list[str] | tuple[str, ...] = (),
    embedded_source: str = "",
    embedded_context: str = "",
    embedded_label: str = "",
    embedded_description: str = "",
    embedded_value_type: str | None = None,
    embedded_options: list[str] | None = None,
    embedded_min: float | int | None = None,
    embedded_max: float | int | None = None,
    embedded_default: str | None = None,
    embedded_category: str | None = None,
    embedded_confidence: str | None = None,
) -> dict[str, Any]:
    """Strict 3-stage offline classification waterfall: reference_dict -> file metadata -> token+type heuristic."""
    # 1. Exact or wildcard match in engine_endpoints_reference.json
    cands = list(reference_candidates) + [identifier]
    ref_hit = lookup_engine_reference(cands, engine=engine)
    if ref_hit is not None:
        return {
            "category": ref_hit["category"] if ref_hit["category"] in _VALID_WORKFLOW_CATEGORIES else "engine_settings",
            "confidence": "high",
            "source": "reference_dict",
            "simple_label": ref_hit.get("simple_label") or embedded_label or identifier,
            "simple_description": ref_hit.get("simple_description") or embedded_description,
            "value_type": ref_hit.get("value_type") or embedded_value_type,
            "options": ref_hit.get("options") if ref_hit.get("options") is not None else embedded_options,
            "min": ref_hit.get("safe_min") if ref_hit.get("safe_min") is not None else embedded_min,
            "max": ref_hit.get("safe_max") if ref_hit.get("safe_max") is not None else embedded_max,
            "step": ref_hit.get("step"),
            "default_value": ref_hit.get("default_value") if ref_hit.get("default_value") is not None else embedded_default,
            "delivery_type": ref_hit.get("delivery_type"),
            "reference_pattern": ref_hit.get("pattern"),
        }

    # 2. File-embedded metadata (BepInEx .cfg doc-comments, Unreal structural rules, Unity dump attributes)
    heur = classify_identifier_heuristic(
        identifier,
        context_text=embedded_context,
        value_type=embedded_value_type,
    )
    if embedded_source:
        if embedded_category in _VALID_WORKFLOW_CATEGORIES and embedded_category != "uncategorized":
            chosen_cat = embedded_category
            chosen_conf = embedded_confidence or "high"
            chosen_src = embedded_source
        elif heur["confidence"] in ("high", "medium"):
            chosen_cat = heur["category"]
            chosen_conf = "high" if (heur["confidence"] == "high" or embedded_description) else "medium"
            chosen_src = embedded_source
        else:
            # Low-confidence opaque symbol must route to 'uncategorized' even when generic .cfg/.ini file metadata exists
            chosen_cat = "uncategorized"
            chosen_conf = "low"
            chosen_src = "heuristic"

        return {
            "category": chosen_cat,
            "confidence": chosen_conf,
            "source": chosen_src,
            "simple_label": embedded_label or identifier,
            "simple_description": embedded_description,
            "value_type": embedded_value_type,
            "options": embedded_options,
            "min": embedded_min,
            "max": embedded_max,
            "step": None,
            "default_value": embedded_default,
            "delivery_type": None,
            "reference_pattern": None,
        }

    # 3. Token + type heuristic classifier
    return {
        "category": heur["category"],
        "confidence": heur["confidence"],
        "source": "heuristic",
        "simple_label": embedded_label or identifier,
        "simple_description": embedded_description,
        "value_type": embedded_value_type,
        "options": embedded_options,
        "min": embedded_min,
        "max": embedded_max,
        "step": None,
        "default_value": embedded_default,
        "delivery_type": None,
        "reference_pattern": None,
    }

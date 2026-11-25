"""Per-game Mod Vault storage and non-executing static Mod Auditor."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.audit import append_audit_log
from app.core.config import (
    APP_DATA_ROOT,
    atomic_write_json,
    load_json_sanitized,
    load_managed_games,
    save_managed_games,
    sha256_file,
    sha256_text,
)
from app.core.constants import (
    LARGE_BINARY_STREAM_THRESHOLD_BYTES,
    MAX_MOD_UNCOMPRESSED_BYTES,
    MAX_MOD_UPLOAD_BYTES,
    MAX_ZIP_BOMB_RATIO,
)
from app.core.security import ensure_within
from app.engines.unreal import parse_unreal_ini_file
from app.services.discovery import make_slug
from app.services.endpoint_catalog import (
    _DTD_OR_ENTITY_RE,
    extract_endpoint_symbol,
    get_imported_endpoints_dir,
    load_game_imported_endpoints,
    parse_cheat_engine_ct,
    record_endpoint_knowledge,
)
from app.services.endpoint_parsers import (
    _VALID_WORKFLOW_CATEGORIES,
    is_excluded_unity_namespace,
    parse_bepinex_cfg,
    resolve_endpoint_classification,
)
from app.services.endpoints import _ALL_CATEGORY_LABELS, scan_game_endpoints
from app.services.game_snapshots import create_game_snapshot

DEFAULT_MAX_MOD_UPLOAD_BYTES = MAX_MOD_UPLOAD_BYTES
_STREAM_CHUNK_BYTES = 1024 * 1024  # 1 MiB
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = MAX_MOD_UNCOMPRESSED_BYTES
_ZIP_BOMB_CHECK_MIN_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB
_MAX_ARCHIVE_ENTRIES = 5000
_LARGE_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".pak",
    ".ucas",
    ".utoc",
    ".ubulk",
    ".bundle",
    ".assets",
})
_MAX_ZLIB_BLOCK_BYTES = 4 * 1024 * 1024
_MAX_ZLIB_BLOCKS_PER_PAK = 96
_NEXUS_ARCHIVE_NAME_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<nexus_id>\d+)\s+(?P<version>[\w.-]+)\s+\d{4}-\d{2}-\d{2}T[\d-]+Z\s+[A-Za-z0-9]+$"
)
_PAK_ACTIVE_EXTENSIONS: frozenset[str] = frozenset({
    ".uasset",
    ".umap",
    ".uexp",
    ".ubulk",
    ".ini",
    ".locres",
})
_PAK_BACKUP_RE = re.compile(r"(?:\.bak|\.orig|\.mod[a-z0-9_-]*)$", re.IGNORECASE)
_ZLIB_HEADERS: tuple[bytes, ...] = (b"\x78\x9c", b"\x78\xda", b"\x78\x01")
_UE_FNAME_BOILERPLATE: frozenset[str] = frozenset({
    "none",
    "byteproperty",
    "intproperty",
    "int8property",
    "int16property",
    "uint16property",
    "uint32property",
    "int64property",
    "uint64property",
    "floatproperty",
    "doubleproperty",
    "boolproperty",
    "strproperty",
    "nameproperty",
    "textproperty",
    "objectproperty",
    "classproperty",
    "structproperty",
    "arrayproperty",
    "mapproperty",
    "setproperty",
    "softobjectproperty",
    "softclassproperty",
    "weakobjectproperty",
    "lazyobjectproperty",
    "enumproperty",
    "delegateproperty",
    "multicastdelegateproperty",
    "multicastinlinedelegateproperty",
    "multicastsparsedelegateproperty",
    "interfaceproperty",
    "fieldpathproperty",
    "uobject",
    "object",
    "class",
    "package",
    "function",
    "metadata",
    "scenecomponent",
    "uscenecomponent",
    "actorcomponent",
    "uactorcomponent",
    "ubergraphframe",
    "executeubergraph",
    "receivebeginplay",
    "receivetick",
    "transient",
    "coreuobject",
    "blueprintgeneratedclass",
    "simpleconstructionscript",
    "scs_node",
})
_ALLOWED_UPLOAD_EXTENSIONS: frozenset[str] = frozenset({
    ".zip",
    ".dll",
    ".pak",
    ".lua",
    ".cfg",
    ".ini",
    ".json",
    ".ct",
    ".7z",
})
_UNPACKED_SAFE_EXTENSIONS: frozenset[str] = _ALLOWED_UPLOAD_EXTENSIONS | frozenset({
    ".txt",
    ".md",
    ".xml",
    ".ucas",
    ".utoc",
    ".uasset",
    ".umap",
    ".uexp",
    ".ubulk",
    ".locres",
})
_SAFE_MOD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class UploadTooLargeError(ValueError):
    """Raised when a streamed upload exceeds the configured byte cap."""


def parse_nexus_filename(filename: str) -> dict[str, str]:
    """Extract Nexus Mods title, nexus_id, and version from Nexus download naming conventions."""
    stem = Path(str(filename or "").strip()).stem.strip()
    m = _NEXUS_ARCHIVE_NAME_RE.match(stem)
    if not m:
        return {}
    return {
        "title": m.group("title").strip(),
        "nexus_id": m.group("nexus_id").strip(),
        "version": m.group("version").strip(),
    }


def _extract_readme_description(root_dir: Path, rel_files: list[str]) -> str:
    for rel in rel_files:
        name_low = Path(rel).name.lower()
        if name_low in ("readme.txt", "readme.md", "readme"):
            try:
                p = ensure_within(root_dir / rel, root_dir)
                if p.is_file():
                    raw = p.read_text(encoding="utf-8-sig", errors="ignore").strip()
                    clean = re.sub(r"\s+", " ", raw)
                    return clean[:1500]
            except Exception:
                continue
    return ""


async def stream_upload_to_path(
    upload_file: Any,
    dest_path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_MOD_UPLOAD_BYTES,
) -> tuple[int, str]:
    """Stream an incoming UploadFile to *dest_path* in chunks and compute SHA-256 without buffering in RAM."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with dest_path.open("wb") as out:
        while True:
            chunk = await upload_file.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UploadTooLargeError(
                    f"Upload size exceeds maximum allowed limit ({max_bytes} bytes)."
                )
            digest.update(chunk)
            out.write(chunk)
    if total == 0:
        raise ValueError("Uploaded mod file is empty.")
    return total, digest.hexdigest()


def get_mod_vault_root(app_data_root: Path | None = None) -> Path:
    root = (Path(app_data_root) if app_data_root is not None else APP_DATA_ROOT) / "mod_vault"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _clean_slug(slug: str) -> str:
    clean = make_slug(slug)
    if not clean or ".." in clean:
        raise ValueError(f"Invalid game slug: '{slug}'")
    return clean


def _validate_mod_id(mod_id: str) -> str:
    clean = str(mod_id or "").strip()
    if not clean or ".." in clean or not _SAFE_MOD_ID_RE.match(clean):
        raise ValueError(f"Invalid mod ID: '{mod_id}'")
    return clean


def _sanitize_upload_filename(filename: str) -> str:
    raw = str(filename or "").replace("\\", "/").strip()
    if not raw or ".." in raw.split("/") or _WIN_DRIVE_RE.match(raw):
        raise ValueError(f"Unsafe upload filename rejected: '{filename}'")
    name = Path(raw).name.strip()
    if not name or name in (".", "..") or ".." in name:
        raise ValueError(f"Unsafe upload filename rejected: '{filename}'")
    ext = Path(name).suffix.lower()
    if ext not in _ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(
            f"Unsupported mod upload extension '{ext or '(none)'}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_UPLOAD_EXTENSIONS))}"
        )
    return name


def _validate_archive_entry_path(entry_name: str, extracted_root: Path) -> Path:
    norm = str(entry_name or "").replace("\\", "/").strip()
    if not norm:
        raise ValueError("Empty archive entry path rejected.")
    if norm.startswith("/") or _WIN_DRIVE_RE.match(norm):
        raise ValueError(f"Absolute archive entry path blocked: '{entry_name}'")
    parts = [p for p in norm.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ValueError(f"Zip-Slip path traversal blocked: '{entry_name}'")
    target = extracted_root.joinpath(*parts)
    return ensure_within(target, extracted_root)


def _safe_extract_zip_selective(
    archive_path: Path,
    extracted_root: Path,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Safely extract small/editable archive entries and keep huge binaries streamed inside the archive."""
    extracted_rel: list[str] = []
    archived_large_entries: list[dict[str, Any]] = []
    total_uncompressed = 0
    total_compressed = 0
    with zipfile.ZipFile(archive_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise ValueError(f"Archive contains too many entries ({len(infos)} > {_MAX_ARCHIVE_ENTRIES}).")
        # First pass: validate EVERY entry path & sizes before writing any files
        validated: list[tuple[zipfile.ZipInfo, Path, str]] = []
        for zinfo in infos:
            is_symlink = ((zinfo.external_attr >> 16) & 0o170000) == 0o120000
            if is_symlink:
                raise ValueError(f"Symlink archive entry rejected: '{zinfo.filename}'")
            if zinfo.is_dir():
                _validate_archive_entry_path(zinfo.filename, extracted_root)
                continue
            target = _validate_archive_entry_path(zinfo.filename, extracted_root)
            uncomp_sz = max(0, int(zinfo.file_size))
            comp_sz = max(0, int(zinfo.compress_size))
            total_uncompressed += uncomp_sz
            total_compressed += comp_sz
            if total_uncompressed > MAX_MOD_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"Archive uncompressed size ({total_uncompressed} bytes) exceeds safety cap ({MAX_MOD_UNCOMPRESSED_BYTES} bytes)."
                )
            rel = target.relative_to(extracted_root.resolve()).as_posix()
            validated.append((zinfo, target, rel))

        if total_uncompressed > _ZIP_BOMB_CHECK_MIN_UNCOMPRESSED_BYTES:
            ratio = total_uncompressed / max(total_compressed, 1)
            if ratio > MAX_ZIP_BOMB_RATIO:
                raise ValueError(
                    f"Zip-bomb rejected: uncompressed size ({total_uncompressed} bytes) vs compressed size "
                    f"({total_compressed} bytes) ratio {ratio:.1f}:1 exceeds max safe ratio ({MAX_ZIP_BOMB_RATIO:.0f}:1)."
                )

        for zinfo, target, rel in validated:
            ext = Path(rel).suffix.lower()
            entry_size = max(0, int(zinfo.file_size))
            if ext in _LARGE_BINARY_EXTENSIONS and entry_size > LARGE_BINARY_STREAM_THRESHOLD_BYTES:
                archived_large_entries.append({
                    "entry_name": zinfo.filename,
                    "rel_path": rel,
                    "archive_path": str(archive_path),
                    "file_size": entry_size,
                    "compress_size": max(0, int(zinfo.compress_size)),
                    "ext": ext,
                })
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zinfo, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 64)
            extracted_rel.append(rel)
    return extracted_rel, archived_large_entries


def _safe_extract_zip(archive_path: Path, extracted_root: Path) -> list[str]:
    extracted_rel, _large = _safe_extract_zip_selective(archive_path, extracted_root)
    return extracted_rel


def _safe_extract_7z(archive_path: Path, extracted_root: Path) -> list[str]:
    try:
        import py7zr  # type: ignore[import-not-found]
    except ImportError:
        py7zr = None

    if py7zr is not None:
        extracted_rel: list[str] = []
        with py7zr.SevenZipFile(archive_path, mode="r") as sz:
            names = sz.getnames()
            if len(names) > _MAX_ARCHIVE_ENTRIES:
                raise ValueError(f".7z archive contains too many entries ({len(names)}).")
            for nm in names:
                _validate_archive_entry_path(nm, extracted_root)
            sz.extractall(path=extracted_root)
            for nm in names:
                cand = ensure_within(extracted_root / nm, extracted_root)
                if cand.is_file():
                    extracted_rel.append(cand.relative_to(extracted_root.resolve()).as_posix())
        return extracted_rel

    seven_zip_exe = shutil.which("7z") or shutil.which("7za")
    if not seven_zip_exe:
        raise ValueError(
            "Cannot extract .7z archive: neither Python 'py7zr' nor '7z' CLI is installed."
        )
    list_proc = subprocess.run(
        [seven_zip_exe, "l", "-slt", str(archive_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if list_proc.returncode != 0:
        raise ValueError(f"7z archive inspection failed: {list_proc.stderr or list_proc.stdout}")
    listed_paths: list[str] = []
    for line in list_proc.stdout.splitlines():
        if line.startswith("Path = "):
            p_val = line[len("Path = "):].strip()
            if p_val and p_val != str(archive_path):
                _validate_archive_entry_path(p_val, extracted_root)
                listed_paths.append(p_val)
    if len(listed_paths) > _MAX_ARCHIVE_ENTRIES:
        raise ValueError(f".7z archive contains too many entries ({len(listed_paths)}).")
    ext_proc = subprocess.run(
        [seven_zip_exe, "x", "-y", f"-o{extracted_root}", str(archive_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if ext_proc.returncode != 0:
        raise ValueError(f"7z extraction failed: {ext_proc.stderr or ext_proc.stdout}")
    return [
        p.relative_to(extracted_root.resolve()).as_posix()
        for p in sorted(extracted_root.rglob("*"))
        if p.is_file()
    ]


def _extract_pe_strings(raw_bytes: bytes) -> list[str]:
    """Extract printable ASCII and UTF-16LE strings directly from PE/.NET binary heaps."""
    ascii_hits = [m.decode("ascii", errors="ignore") for m in re.findall(rb"[ -~]{4,}", raw_bytes)]
    utf16_hits = [
        m.decode("utf-16le", errors="ignore")
        for m in re.findall(rb"(?:[ -~]\x00){4,}", raw_bytes)
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for s in ascii_hits + utf16_hits:
        clean = s.strip()
        if clean and clean not in seen:
            seen.add(clean)
            ordered.append(clean)
    return ordered


def _inspect_dll_file(dll_path: Path, rel_path: str, source_mod: str) -> dict[str, Any]:
    raw_bytes = dll_path.read_bytes()
    strings = _extract_pe_strings(raw_bytes)
    joined = "\n".join(strings)

    # Optional ilspycmd decompilation if installed on PATH
    decompiled_text = ""
    ilspy_exe = shutil.which("ilspycmd")
    if ilspy_exe:
        try:
            proc = subprocess.run(
                [ilspy_exe, str(dll_path)],
                capture_output=True,
                text=True,
                timeout=25,
            )
            if proc.returncode == 0 and proc.stdout:
                decompiled_text = proc.stdout
        except Exception:
            decompiled_text = ""

    analysis_corpus = f"{joined}\n{decompiled_text}" if decompiled_text else joined
    low_corpus = analysis_corpus.lower()

    if any(tok in low_corpus for tok in ("bepinex.unity.il2cpp", "il2cppinterop", "il2cppsystem", "baseplugin")):
        runtime_target = "IL2CPP"
    elif any(tok in low_corpus for tok in ("baseunityplugin", "bepinex.dll", "unityengine.coremodule")):
        runtime_target = "Mono"
    else:
        runtime_target = "Unknown"

    plugins: list[dict[str, Any]] = []
    dependencies: list[str] = []
    hooks: list[dict[str, Any]] = []
    referenced_types: list[str] = []
    candidate_endpoints: list[dict[str, Any]] = []

    # 1. Explicit [BepInPlugin("guid", "name", "version")] patterns
    for m in re.finditer(
        r'BepInPlugin\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)',
        analysis_corpus,
    ):
        plugins.append({
            "guid": m.group(1),
            "name": m.group(2),
            "version": m.group(3),
            "runtime_target": runtime_target,
            "file": rel_path,
        })

    # Compiled string-heap heuristic for [BepInPlugin] when attribute syntax isn't decompiled
    if not plugins and "BepInPlugin" in analysis_corpus:
        guid_cand = next(
            (s for s in strings if re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+){2,}", s) and not is_excluded_unity_namespace(s)),
            "",
        )
        ver_cand = next((s for s in strings if re.fullmatch(r"\d+\.\d+(?:\.\d+){0,2}", s)), "1.0.0")
        if guid_cand:
            plugins.append({
                "guid": guid_cand,
                "name": dll_path.stem,
                "version": ver_cand,
                "runtime_target": runtime_target,
                "file": rel_path,
            })

    # 2. [BepInDependency("...")]
    for m in re.finditer(r'BepInDependency\(\s*"([^"]+)"', analysis_corpus):
        dep = m.group(1).strip()
        if dep and dep not in dependencies:
            dependencies.append(dep)

    # 3. [HarmonyPatch(...)] hooks
    seen_hooks: set[str] = set()
    for m in re.finditer(
        r'HarmonyPatch\(\s*(?:typeof\(\s*([A-Za-z0-9_.+]+)\s*\)|"([^"]+)")\s*(?:,\s*(?:nameof\(\s*([A-Za-z0-9_.]+)\s*\)|"([^"]+)"))?\s*\)',
        analysis_corpus,
    ):
        t_cls = (m.group(1) or m.group(2) or "").strip()
        t_meth = (m.group(3) or m.group(4) or "").strip().rsplit(".", 1)[-1]
        if not t_cls:
            continue
        sig = f"{t_cls}.{t_meth}".rstrip(".")
        if sig not in seen_hooks:
            seen_hooks.add(sig)
            hooks.append({
                "kind": "harmony_patch",
                "target_type": t_cls,
                "target_method": t_meth or "PatchTarget",
                "signature": sig,
                "source_file": rel_path,
            })

    # 4. Config.Bind("Section", "Key", defaultVal, ...)
    for m in re.finditer(
        r'Config\.Bind(?:<[^>]+>)?\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*([^,\)\n]+)',
        analysis_corpus,
    ):
        sec, key, raw_def = m.group(1).strip(), m.group(2).strip(), m.group(3).strip().strip('"').rstrip("fdm")
        sym = f"{sec}.{key}"
        resolved = resolve_endpoint_classification(key, engine="unity", reference_candidates=[sym, key], embedded_context=f"{dll_path.stem} {sec}")
        candidate_endpoints.append({
            "symbol": sym,
            "name": resolved["simple_label"],
            "category": resolved["category"],
            "description": resolved["simple_description"] or f"Config.Bind key '{sym}' extracted from {rel_path}.",
            "default_value": raw_def,
            "value_type": "bool" if raw_def.lower() in ("true", "false") else ("number" if re.fullmatch(r"-?\d+(?:\.\d+)?", raw_def) else "string"),
            "delivery_type": "Config",
            "source_mod": source_mod,
            "source_kind": "dll_config_bind",
        })

    # 5. Referenced non-engine game namespaces/types
    seen_types: set[str] = set()
    for s in strings:
        for tok in re.findall(r"\b[A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*){1,4}\b", s):
            if tok in seen_types or is_excluded_unity_namespace(tok):
                continue
            if tok.startswith(("BepInEx.", "HarmonyLib.", "Il2CppInterop.")):
                continue
            seen_types.add(tok)
            referenced_types.append(tok)
            if len(referenced_types) >= 80:
                break
        if len(referenced_types) >= 80:
            break

    return {
        "runtime_target": runtime_target,
        "plugins": plugins,
        "dependencies": dependencies,
        "hooks": hooks,
        "referenced_types": referenced_types,
        "candidate_endpoints": candidate_endpoints,
    }


def _classify_unreal_asset_category(asset_path: str) -> str:
    """Classify an Unreal asset path into `Engine Settings`, `QoL`, `Player Character`, or `Game Systems`."""
    low = asset_path.replace("\\", "/").lower()
    segments = [seg for seg in re.split(r"[/._-]+", low) if seg]
    seg_set = set(segments)

    if seg_set & {"ui", "hud", "menu", "widget", "wbp", "camera", "fov", "locres", "accessibility"}:
        return "QoL"
    if seg_set & {"lighting", "lgt", "volumetric", "materials", "material", "postprocess", "rendering", "shader", "scalability", "ini"}:
        return "Engine Settings"
    if seg_set & {"player", "pawn", "character", "hero", "avatar"}:
        return "Player Character"
    if seg_set & {"weapons", "weapon", "ai", "enemy", "combat", "loot", "damage", "spawn", "gameplay"}:
        return "Game Systems"
    if "lighting" in low or "volumetric" in low or "materials" in low or low.endswith(".ini"):
        return "Engine Settings"
    if "player" in low or "pawn" in low:
        return "Player Character"
    if "hud" in low or "/ui/" in low or low.endswith(".locres"):
        return "QoL"
    return "Game Systems"


def _workflow_category_key_from_label(label: str) -> str:
    return {
        "Engine Settings": "engine_settings",
        "QoL": "qol",
        "Player Character": "player_character",
        "Game Systems": "game_systems",
    }.get(label, "game_systems")


def _extract_ue_fnames_from_bytes(payload: bytes) -> list[str]:
    """Extract candidate UE4 `.uasset` FName property & component identifiers, filtering boilerplate."""
    text = payload.decode("latin-1", errors="ignore")
    found: list[str] = []
    seen: set[str] = set()
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,96}", text):
        low = tok.lower()
        if low in _UE_FNAME_BOILERPLATE:
            continue
        if low.startswith(("default__", "/script/", "executeubergraph_", "receive", "triggerevent")):
            continue
        if not (any(c.isupper() for c in tok) or "_" in tok):
            continue
        if len(tok) < 4:
            continue
        if tok not in seen:
            seen.add(tok)
            found.append(tok)
            if len(found) >= 200:
                break
    return found


def _inspect_unreal_pak(
    path: Path | str,
    rel_path: str = "",
    source_mod: str = "",
    *,
    stream: Any = None,
    file_size: int | None = None,
) -> dict[str, Any]:
    """Pure-Python UE4 `.pak` & `zlib` `.uasset` FName static inspector (supports disk paths or streams)."""
    pak_path = Path(path)
    eff_rel = rel_path or pak_path.name
    eff_src = source_mod or pak_path.name
    if stream is not None:
        size = int(file_size) if file_size is not None else 0
        if size <= 64 * 1024 * 1024:
            raw_bytes = stream.read(64 * 1024 * 1024)
        else:
            head_chunk = stream.read(16 * 1024 * 1024)
            tail_chunk = b""
            try:
                if hasattr(stream, "seekable") and stream.seekable():
                    tail_start = max(len(head_chunk), size - 16 * 1024 * 1024)
                    stream.seek(tail_start)
                    tail_chunk = stream.read(16 * 1024 * 1024)
            except Exception:
                tail_chunk = b""
            raw_bytes = head_chunk + tail_chunk
    else:
        size = pak_path.stat().st_size if pak_path.is_file() else 0
        if size <= 64 * 1024 * 1024:
            raw_bytes = pak_path.read_bytes()
        else:
            with pak_path.open("rb") as f:
                head_chunk = f.read(16 * 1024 * 1024)
                tail_start = max(len(head_chunk), size - 16 * 1024 * 1024)
                f.seek(tail_start)
                tail_chunk = f.read(16 * 1024 * 1024)
            raw_bytes = head_chunk + tail_chunk

    corpus = raw_bytes.decode("latin-1", errors="ignore")

    # 1. Mount point extraction (e.g., "../../../")
    mount_point = ""
    mp_matches = re.findall(r"(?:\.\./)+", corpus)
    if mp_matches:
        mount_point = max(mp_matches, key=len)

    # 2. Extract cooked content paths & standalone .uasset/.bak entries from index/strings
    raw_entries: list[str] = []
    seen_entry_low: set[str] = set()

    def _add_entry(raw_ent: str) -> None:
        clean = raw_ent.replace("\\", "/").strip().strip("\x00").lstrip("/")
        if not clean or len(clean) < 4:
            return
        while clean.startswith("../"):
            clean = clean[3:]
        if not clean:
            return
        low = clean.lower()
        if any(ex == low or ex.endswith("/" + low) for ex in seen_entry_low):
            return
        seen_entry_low.add(low)
        raw_entries.append(clean)

    path_patterns = (
        r"[A-Za-z0-9_-]+/Content/[A-Za-z0-9_./-]{2,180}\.(?:uasset|umap|uexp|ubulk|ini|locres|bak|orig|mod[A-Za-z0-9_-]*)(?![A-Za-z0-9_.-])",
        r"/Game/[A-Za-z0-9_./-]{2,180}(?:\.(?:uasset|umap|uexp|ubulk|ini|locres|bak|orig|mod[A-Za-z0-9_-]*))?(?![A-Za-z0-9_.-])",
        r"Content/[A-Za-z0-9_./-]{2,180}\.(?:uasset|umap|uexp|ubulk|ini|locres|bak|orig|mod[A-Za-z0-9_-]*)(?![A-Za-z0-9_.-])",
        r"[A-Za-z0-9_.-]{2,120}\.(?:uasset|umap|uexp|ubulk|ini|locres)\.(?:bak|orig|mod[A-Za-z0-9_-]*)(?![A-Za-z0-9_.-])",
        r"[A-Za-z0-9_.-]{2,120}\.(?:uasset|umap|bak|orig)(?![A-Za-z0-9_.-])",
    )
    for pat in path_patterns:
        for m in re.findall(pat, corpus, re.IGNORECASE):
            _add_entry(m)

    # 3. Partition into active_assets vs packed_backups + categorize
    active_assets: list[dict[str, Any]] = []
    packed_backups: list[str] = []
    for entry in raw_entries:
        if _PAK_BACKUP_RE.search(entry):
            packed_backups.append(entry)
            continue
        ext = Path(entry).suffix.lower()
        if ext in _PAK_ACTIVE_EXTENSIONS or entry.lower().startswith("game/") or "/content/" in entry.lower():
            cat_label = _classify_unreal_asset_category(entry)
            active_assets.append({
                "path": entry,
                "asset_name": Path(entry).name,
                "ext": ext or ".uasset",
                "category": cat_label,
            })

    hygiene_notes: list[str] = []
    if packed_backups:
        hygiene_notes.append(
            f"Packaging hygiene warning: {len(packed_backups)} packed author backup file(s) "
            f"({', '.join(Path(b).name for b in packed_backups[:4])}) were left inside {Path(eff_rel).name}."
        )

    # 4. Scan & safely decompress zlib blocks (\x78\x9c, \x78\xda, \x78\x01) for FName extraction
    uproperty_symbols: list[str] = []
    seen_sym: set[str] = set()
    active_syms_by_name: dict[str, set[str]] = {}
    backup_syms_by_name: dict[str, set[str]] = {}
    blocks_scanned = 0

    zlib_positions: list[int] = []
    for hdr in _ZLIB_HEADERS:
        start = 0
        while len(zlib_positions) < _MAX_ZLIB_BLOCKS_PER_PAK * 2:
            pos = raw_bytes.find(hdr, start)
            if pos < 0:
                break
            zlib_positions.append(pos)
            start = pos + 2
    zlib_positions = sorted(set(zlib_positions))

    cursor_end = 0
    for zpos in zlib_positions:
        if blocks_scanned >= _MAX_ZLIB_BLOCKS_PER_PAK:
            break
        if zpos < cursor_end:
            continue
        window = raw_bytes[zpos : zpos + _MAX_ZLIB_BLOCK_BYTES]
        try:
            decomp = zlib.decompressobj()
            out_bytes = decomp.decompress(window, _MAX_ZLIB_BLOCK_BYTES)
            if not out_bytes:
                continue
        except zlib.error:
            continue

        blocks_scanned += 1
        trailing_bytes = len(decomp.unused_data) if decomp.eof else len(decomp.unconsumed_tail)
        consumed = max(2, len(window) - trailing_bytes)
        cursor_end = zpos + consumed

        chunk_fnames = _extract_ue_fnames_from_bytes(out_bytes)
        if not chunk_fnames:
            continue

        ctx_window = raw_bytes[max(0, zpos - 320) : zpos].decode("latin-1", errors="ignore")
        marker_hits = re.findall(
            r"([A-Za-z0-9_./-]+\.(?:uasset|umap)(?:\.(?:bak|orig|mod[A-Za-z0-9_-]*))?)",
            ctx_window,
            re.IGNORECASE,
        )
        assoc_name = Path(marker_hits[-1]).name.lower() if marker_hits else ""

        if assoc_name and _PAK_BACKUP_RE.search(assoc_name):
            base_name = re.sub(_PAK_BACKUP_RE, "", assoc_name)
            backup_syms_by_name.setdefault(base_name, set()).update(chunk_fnames)
        else:
            if assoc_name:
                active_syms_by_name.setdefault(assoc_name, set()).update(chunk_fnames)
            for sym in chunk_fnames:
                if sym not in seen_sym:
                    seen_sym.add(sym)
                    uproperty_symbols.append(sym)
                    if len(uproperty_symbols) >= 160:
                        break

    # 5. Diff modified .uasset vs matching packed .bak FName symbols
    bak_diffs: list[dict[str, Any]] = []
    for bak in packed_backups:
        bak_name = Path(bak).name.lower()
        base_name = re.sub(_PAK_BACKUP_RE, "", bak_name)
        act_syms = active_syms_by_name.get(base_name) or set(uproperty_symbols)
        bak_syms = backup_syms_by_name.get(base_name)
        if bak_syms is not None:
            added = sorted(act_syms - bak_syms)
            removed = sorted(bak_syms - act_syms)
            matching_active = next(
                (a["path"] for a in active_assets if Path(a["path"]).name.lower() == base_name),
                base_name,
            )
            bak_diffs.append({
                "asset": matching_active,
                "backup_file": bak,
                "added_symbols": added[:80],
                "removed_symbols": removed[:80],
            })

    asset_paths = [a["path"] for a in active_assets]
    candidates: list[dict[str, Any]] = []
    for asset_item in active_assets[:25]:
        ap = asset_item["path"]
        stem = Path(ap).stem
        wf_cat = _workflow_category_key_from_label(asset_item["category"])
        candidates.append({
            "symbol": ap,
            "name": f"{stem} ({Path(eff_rel).name})",
            "category": wf_cat,
            "description": f"Unreal .pak {asset_item['category']} asset '{ap}' (mount: {mount_point or 'default'}).",
            "default_value": "Present",
            "value_type": "string",
            "delivery_type": "Mod File",
            "source_mod": eff_src,
            "source_kind": "pak_asset_path",
        })

    return {
        "mount_point": mount_point,
        "active_assets": active_assets,
        "packed_backups": packed_backups,
        "hygiene_notes": hygiene_notes,
        "compressed_blocks_scanned": blocks_scanned,
        "uproperty_symbols": uproperty_symbols,
        "bak_diffs": bak_diffs,
        "asset_paths": asset_paths,
        "candidate_endpoints": candidates,
    }


def _inspect_pak_file(pak_path: Path, rel_path: str, source_mod: str) -> dict[str, Any]:
    return _inspect_unreal_pak(pak_path, rel_path=rel_path, source_mod=source_mod)


def compute_pak_asset_conflicts(pak_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect shared `.uasset`/`.umap` overrides across two or more `.pak` mods."""
    by_asset_key: dict[str, dict[str, Any]] = {}
    for src in pak_sources:
        mod_label = str(src.get("mod_name") or src.get("filename") or "unknown.pak")
        mod_id = str(src.get("mod_id") or mod_label)
        assets = src.get("active_assets") or []
        for item in assets:
            ap = str(item.get("path") if isinstance(item, dict) else item).replace("\\", "/").strip()
            if not ap:
                continue
            low = ap.lower()
            if not (low.endswith(".uasset") or low.endswith(".umap")):
                continue
            norm_key = low.split("/content/", 1)[1] if "/content/" in low else Path(low).name
            bucket = by_asset_key.setdefault(
                norm_key,
                {
                    "asset_path": ap,
                    "asset_name": Path(ap).name,
                    "norm_key": norm_key,
                    "mods": [],
                    "mod_ids": set(),
                },
            )
            if mod_id not in bucket["mod_ids"]:
                bucket["mod_ids"].add(mod_id)
                bucket["mods"].append({
                    "mod_id": mod_id,
                    "mod_name": mod_label,
                    "source": str(src.get("source") or "Mod"),
                    "asset_path": ap,
                })

    conflicts: list[dict[str, Any]] = []
    for bucket in by_asset_key.values():
        if len(bucket["mods"]) >= 2:
            conflicts.append({
                "asset_path": bucket["asset_path"],
                "asset_name": bucket["asset_name"],
                "conflicting_mods": [m["mod_name"] for m in bucket["mods"]],
                "details": bucket["mods"],
                "severity": "warning",
            })
    conflicts.sort(key=lambda c: c["asset_name"].lower())
    return conflicts


def _inspect_lua_file(lua_text: str, rel_path: str, source_mod: str) -> dict[str, Any]:
    hooks: list[dict[str, Any]] = []
    seen_hooks: set[str] = set()
    candidates: list[dict[str, Any]] = []

    fn_patterns = (
        ("ue4ss_register_hook", r'RegisterHook\(\s*["\']([^"\']+)["\']'),
        ("ue4ss_notify_new_object", r'NotifyOnNewObject\(\s*["\']([^"\']+)["\']'),
        ("ue4ss_find_first_of", r'FindFirstOf\(\s*["\']([^"\']+)["\']'),
        ("ue4ss_find_all_of", r'FindAllOf\(\s*["\']([^"\']+)["\']'),
    )
    for kind, rx in fn_patterns:
        for sig in re.findall(rx, lua_text):
            key = f"{kind}:{sig}"
            if key in seen_hooks:
                continue
            seen_hooks.add(key)
            cls_part = sig.split(":", 1)[0].rsplit("/", 1)[-1] if ":" in sig else sig.rsplit("/", 1)[-1]
            meth_part = sig.split(":", 1)[1] if ":" in sig else ""
            hooks.append({
                "kind": kind,
                "target_type": cls_part,
                "target_method": meth_part or kind,
                "signature": sig,
                "source_file": rel_path,
            })

    # Extract UObject property/method references & Lua Config.* assignments
    for m in re.finditer(
        r"^(?:local\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(true|false|-?\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*')",
        lua_text,
        re.MULTILINE,
    ):
        sym, raw_val = m.group(1), m.group(2).strip().strip("\"'")
        key_short = sym.rsplit(".", 1)[-1]
        v_type = "bool" if raw_val.lower() in ("true", "false") else ("number" if re.fullmatch(r"-?\d+(?:\.\d+)?", raw_val) else "string")
        resolved = resolve_endpoint_classification(key_short, engine="unreal", reference_candidates=[sym, key_short], embedded_context=f"{rel_path} {sym}")
        candidates.append({
            "symbol": sym,
            "name": resolved["simple_label"],
            "category": resolved["category"],
            "description": resolved["simple_description"] or f"UE4SS Lua variable '{sym}' from {rel_path}.",
            "default_value": raw_val,
            "current_value": raw_val,
            "value_type": v_type,
            "delivery_type": "Mod File",
            "source_mod": source_mod,
            "source_kind": "ue4ss_lua_audit",
            "file_path": rel_path,
            "editable": True,
        })

    for h in hooks:
        sig = h["signature"]
        tok = h["target_method"] if h["target_method"] != h["kind"] else h["target_type"]
        resolved = resolve_endpoint_classification(tok, engine="unreal", reference_candidates=[sig, tok], embedded_context=f"{rel_path} {sig}")
        candidates.append({
            "symbol": sig,
            "name": f"{h['kind']}: {tok}",
            "category": resolved["category"],
            "description": f"UE4SS target '{sig}' ({h['kind']}) extracted from {rel_path}.",
            "default_value": "Enabled",
            "value_type": "select",
            "options": ["Enabled", "Disabled"],
            "delivery_type": "Mod File",
            "source_mod": source_mod,
            "source_kind": h["kind"],
        })

    return {"hooks": hooks, "candidate_endpoints": candidates}


def _inspect_xml_manifest(xml_text: str, rel_path: str, source_mod: str) -> dict[str, Any]:
    if _DTD_OR_ENTITY_RE.search(xml_text):
        raise ValueError(f"Unsafe XML containing DOCTYPE/ENTITY rejected in '{rel_path}'.")
    root = ET.fromstring(xml_text)
    meta: dict[str, Any] = {"file": rel_path, "plugins": [], "files": []}
    for tag in ("Name", "Author", "Version", "Description", "Website", "moduleName"):
        found = root.find(f".//{tag}")
        if found is not None and (found.text or "").strip():
            meta[tag.lower()] = (found.text or "").strip()
    for p_el in root.findall(".//plugin"):
        p_name = str(p_el.attrib.get("name") or "").strip()
        if p_name:
            meta["plugins"].append(p_name)
    for f_el in root.findall(".//file"):
        src = str(f_el.attrib.get("source") or "").strip()
        dst = str(f_el.attrib.get("destination") or "").strip()
        if src or dst:
            meta["files"].append({"source": src, "destination": dst})
    return meta


def _inspect_manifest_json(raw_text: str, rel_path: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw_text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    deps = data.get("dependencies")
    return {
        "file": rel_path,
        "name": str(data.get("name") or "").strip(),
        "version": str(data.get("version_number") or data.get("version") or "").strip(),
        "author": str(data.get("author") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "website_url": str(data.get("website_url") or "").strip(),
        "dependencies": [str(d) for d in deps] if isinstance(deps, list) else [],
    }


def _build_blueprint_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Reference Mod Audit — {report.get('mod_name') or report.get('filename')}",
        "",
        f"- **Mod ID:** `{report.get('mod_id')}`",
        f"- **Game Slug:** `{report.get('slug')}`",
        f"- **Filename:** `{report.get('filename')}`",
        f"- **Version:** `{report.get('version') or 'unknown'}`",
        f"- **Source / Notes:** {report.get('source_label') or 'Vault Upload'} — {report.get('notes') or 'None'}",
        "",
    ]
    plugins = report.get("plugins") or []
    if plugins:
        lines.append("## Plugins & Runtime Targets")
        for p in plugins:
            lines.append(f"- `{p.get('guid') or p.get('name')}` v{p.get('version')} ({p.get('runtime_target')}) in `{p.get('file')}`")
        lines.append("")
    deps = report.get("dependencies") or []
    if deps:
        lines.append("## Dependencies")
        for d in deps:
            lines.append(f"- `{d}`")
        lines.append("")
    hooks = report.get("hooks") or []
    if hooks:
        lines.append("## Hooked Classes / Methods / UE4SS Targets")
        for h in hooks[:50]:
            lines.append(f"- **[{h.get('kind')}]** `{h.get('signature')}` (`{h.get('source_file')}`)")
        lines.append("")
    assets = report.get("asset_paths") or []
    active_assets = report.get("active_assets") or []
    mount_points = report.get("mount_points") or []
    if active_assets or assets:
        lines.append("## Modified Unreal Assets")
        if mount_points:
            lines.append(f"- **Mount Point(s):** {', '.join(f'`{mp}`' for mp in mount_points)}")
        if active_assets:
            for item in active_assets[:40]:
                lines.append(f"- `{item.get('path')}` — [{item.get('category')}]")
        else:
            for ap in assets[:40]:
                lines.append(f"- `{ap}`")
        lines.append("")
    uprops = report.get("uproperty_symbols") or []
    bak_diffs = report.get("bak_diffs") or []
    if uprops or bak_diffs:
        lines.append("## Extracted UProperty / Component Names")
        if uprops:
            lines.append("- " + ", ".join(f"`{s}`" for s in uprops[:60]))
        for bd in bak_diffs:
            lines.append(
                f"- Diff `{bd.get('asset')}` vs `{bd.get('backup_file')}`: "
                f"+[{', '.join(bd.get('added_symbols') or [])}] "
                f"-[{', '.join(bd.get('removed_symbols') or [])}]"
            )
        lines.append("")
    packed_backups = report.get("packed_backups") or []
    hygiene_notes = report.get("hygiene_notes") or []
    if packed_backups or hygiene_notes:
        lines.append("## Packed Author Backups")
        for note in hygiene_notes:
            lines.append(f"- {note}")
        for bk in packed_backups[:25]:
            lines.append(f"- `{bk}`")
        lines.append("")
    conflicts = report.get("asset_conflicts") or []
    if conflicts:
        lines.append("## Cross-Mod Asset Conflicts")
        for c in conflicts:
            lines.append(
                f"- `{c.get('asset_path')}` modified by: {', '.join(c.get('conflicting_mods') or [])}"
            )
        lines.append("")
    cands = report.get("candidate_endpoints") or []
    if cands:
        lines.append("## Candidate Endpoints & Configs")
        for c in cands[:60]:
            lines.append(
                f"- `{c.get('symbol')}` [{c.get('category')}] = `{c.get('default_value')}` — {c.get('description')}"
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _sync_game_stored_mods_entry(
    slug: str,
    summary_entry: dict[str, Any] | None,
    *,
    remove_mod_id: str | None = None,
    games_store_path: Path | None = None,
) -> None:
    if games_store_path is not None:
        from app.core import config as core_cfg

        with _patch_store_path(core_cfg, games_store_path):
            _mutate_store(slug, summary_entry, remove_mod_id)
    else:
        _mutate_store(slug, summary_entry, remove_mod_id)


class _patch_store_path:
    def __init__(self, core_cfg: Any, path: Path) -> None:
        self.core_cfg = core_cfg
        self.new_path = path
        self.old_path = core_cfg.GAMES_STORE_PATH

    def __enter__(self) -> None:
        self.core_cfg.GAMES_STORE_PATH = self.new_path
        self.core_cfg.invalidate_games_cache()

    def __exit__(self, *_args: Any) -> None:
        self.core_cfg.GAMES_STORE_PATH = self.old_path
        self.core_cfg.invalidate_games_cache()


def _mutate_store(slug: str, summary_entry: dict[str, Any] | None, remove_mod_id: str | None) -> None:
    store = load_managed_games()
    clean = _clean_slug(slug)
    updated = False
    for g in store.get("games", []):
        if make_slug(str(g.get("id") or "")) == clean or str(g.get("id") or "") == slug:
            existing = [m for m in (g.get("stored_mods") or []) if isinstance(m, dict)]
            if remove_mod_id:
                existing = [m for m in existing if m.get("mod_id") != remove_mod_id]
            if summary_entry is not None:
                existing = [m for m in existing if m.get("mod_id") != summary_entry.get("mod_id")]
                existing.append(summary_entry)
            g["stored_mods"] = existing
            updated = True
            break
    if updated:
        save_managed_games(store)


def _sanitize_unpacked_relpath(raw_rel_path: str, unpacked_dir: Path) -> str:
    norm = str(raw_rel_path or "").replace("\\", "/").strip()
    if not norm or norm.startswith("/") or _WIN_DRIVE_RE.match(norm):
        raise ValueError(f"Unsafe unpacked file path rejected: '{raw_rel_path}'")
    parts = [p for p in norm.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise ValueError(f"Path traversal blocked in unpacked upload: '{raw_rel_path}'")
    ext = Path(parts[-1]).suffix.lower()
    if ext not in _UNPACKED_SAFE_EXTENSIONS:
        raise ValueError(
            f"Unsupported mod file extension '{ext or '(none)'}' in '{parts[-1]}'."
        )
    target = _validate_archive_entry_path("/".join(parts), unpacked_dir)
    return target.relative_to(unpacked_dir.resolve()).as_posix()


def _resolve_audit_content_dir(mod_dir: Path, report: dict[str, Any] | None = None) -> Path:
    if isinstance(report, dict) and report.get("storage_mode") == "linked" and report.get("linked_path"):
        lp = Path(str(report["linked_path"]))
        if lp.is_dir():
            return lp.resolve()
        if lp.is_file():
            return lp.parent.resolve()
    unpacked = mod_dir / "unpacked"
    if unpacked.is_dir():
        return unpacked.resolve()
    extracted = mod_dir / "extracted"
    if extracted.is_dir():
        return extracted.resolve()
    return unpacked.resolve()


def _finalize_mod_audit(
    game: dict[str, Any],
    clean_slug: str,
    mod_id: str,
    mod_dir: Path,
    unpacked_dir: Path,
    audit_root: Path,
    extracted_files: list[str],
    safe_name: str,
    file_size: int,
    file_sha: str,
    *,
    storage_mode: str = "zip",
    linked_path: str = "",
    bundle_name: str = "",
    notes: str = "",
    source_label: str = "",
    archived_entries: list[dict[str, Any]] | None = None,
    original_archive: Path | None = None,
    app_data_root: Path | None = None,
    games_store_path: Path | None = None,
) -> dict[str, Any]:
    plugins: list[dict[str, Any]] = []
    dependencies: list[str] = []
    hooks: list[dict[str, Any]] = []
    asset_paths: list[str] = []
    mount_points: list[str] = []
    active_assets: list[dict[str, Any]] = []
    packed_backups: list[str] = []
    hygiene_notes: list[str] = []
    uproperty_symbols: list[str] = []
    bak_diffs: list[dict[str, Any]] = []
    referenced_types: list[str] = []
    candidate_endpoints: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []

    source_mod = safe_name
    for rel in extracted_files:
        target = ensure_within(audit_root / rel, audit_root)
        if not target.is_file():
            continue
        rsuf = target.suffix.lower()
        rname_low = target.name.lower()

        if rsuf == ".dll":
            res = _inspect_dll_file(target, rel, source_mod)
            plugins.extend(res["plugins"])
            dependencies.extend(d for d in res["dependencies"] if d not in dependencies)
            hooks.extend(res["hooks"])
            referenced_types.extend(t for t in res["referenced_types"] if t not in referenced_types)
            candidate_endpoints.extend(res["candidate_endpoints"])
        elif rsuf == ".pak":
            res = _inspect_unreal_pak(target, rel, source_mod)
            if res.get("mount_point") and res["mount_point"] not in mount_points:
                mount_points.append(res["mount_point"])
            active_assets.extend(res.get("active_assets") or [])
            packed_backups.extend(b for b in (res.get("packed_backups") or []) if b not in packed_backups)
            hygiene_notes.extend(n for n in (res.get("hygiene_notes") or []) if n not in hygiene_notes)
            uproperty_symbols.extend(s for s in (res.get("uproperty_symbols") or []) if s not in uproperty_symbols)
            bak_diffs.extend(res.get("bak_diffs") or [])
            asset_paths.extend(p for p in res["asset_paths"] if p not in asset_paths)
            candidate_endpoints.extend(res["candidate_endpoints"])
        elif rsuf == ".lua":
            lua_txt = target.read_text(encoding="utf-8-sig", errors="ignore")
            res = _inspect_lua_file(lua_txt, rel, source_mod)
            hooks.extend(res["hooks"])
            candidate_endpoints.extend(res["candidate_endpoints"])
        elif rsuf == ".cfg":
            for c in parse_bepinex_cfg(target):
                sym = f"{c['section']}.{c['key']}"
                resolved = resolve_endpoint_classification(
                    c["key"],
                    engine="unity",
                    reference_candidates=[sym, c["key"]],
                    embedded_source=c.get("metadata_source") or "cfg_metadata",
                    embedded_context=f"{target.stem} {c['section']} {c['simple_description']}",
                    embedded_description=c["simple_description"],
                    embedded_value_type=c.get("value_type"),
                    embedded_options=c.get("choices") or None,
                    embedded_min=c.get("min_value"),
                    embedded_max=c.get("max_value"),
                    embedded_default=c["default_value"],
                )
                def_str = str(c["default_value"])
                candidate_endpoints.append({
                    "symbol": sym,
                    "name": resolved["simple_label"],
                    "category": resolved["category"],
                    "description": resolved["simple_description"],
                    "default_value": def_str,
                    "current_value": def_str,
                    "value_type": resolved.get("value_type") or c.get("value_type") or "string",
                    "options": resolved.get("options") or [],
                    "min": resolved.get("min"),
                    "max": resolved.get("max"),
                    "delivery_type": "Config",
                    "source_mod": source_mod,
                    "source_kind": "mod_vault_cfg",
                    "file_path": rel,
                    "section": c["section"],
                    "key": c["key"],
                    "editable": True,
                })
        elif rsuf == ".ini":
            for sec, kv in parse_unreal_ini_file(target).items():
                for k, v in list(kv.items())[:50]:
                    sym = f"{sec}.{k}"
                    resolved = resolve_endpoint_classification(k, engine="unreal", reference_candidates=[sym, k], embedded_context=f"{target.stem} {sec}")
                    val_str = str(v)
                    candidate_endpoints.append({
                        "symbol": sym,
                        "name": resolved["simple_label"],
                        "category": resolved["category"],
                        "description": resolved["simple_description"] or f"INI setting '{k}' under [{sec}] in {rel}.",
                        "default_value": val_str,
                        "current_value": val_str,
                        "value_type": resolved.get("value_type") or "string",
                        "delivery_type": "Config",
                        "source_mod": source_mod,
                        "source_kind": "mod_vault_ini",
                        "file_path": rel,
                        "section": sec,
                        "key": k,
                        "editable": True,
                    })
        elif rsuf == ".json" and rname_low == "manifest.json":
            m_info = _inspect_manifest_json(target.read_text(encoding="utf-8-sig", errors="ignore"), rel)
            if m_info:
                manifests.append({"kind": "thunderstore_manifest", **m_info})
                dependencies.extend(d for d in m_info.get("dependencies", []) if d not in dependencies)
        elif rsuf == ".xml" and rname_low in ("info.xml", "moduleconfig.xml"):
            xml_txt = target.read_text(encoding="utf-8-sig", errors="ignore")
            x_meta = _inspect_xml_manifest(xml_txt, rel, source_mod)
            manifests.append({"kind": "fomod_xml", **x_meta})
        elif rsuf == ".ct":
            ct_txt = target.read_text(encoding="utf-8-sig", errors="ignore")
            for ct_ep in parse_cheat_engine_ct(ct_txt, file_name=target.name):
                def_ct = str(ct_ep.get("default_value") or "")
                candidate_endpoints.append({
                    "symbol": (ct_ep.get("advanced_details") or {}).get("symbol") or ct_ep["name"],
                    "name": ct_ep["name"],
                    "category": ct_ep["category"],
                    "description": ct_ep["simple_description"],
                    "default_value": def_ct,
                    "current_value": def_ct,
                    "value_type": ct_ep.get("value_type") or "string",
                    "delivery_type": ct_ep.get("delivery_type") or "Bridge",
                    "source_mod": source_mod,
                    "source_kind": "mod_vault_ct",
                    "file_path": rel,
                    "editable": False,
                })

    # Inspect large binary entries retained inside original archive without duplicating to unpacked/
    eff_archive = original_archive
    if eff_archive is None and archived_entries:
        cand = Path(str(archived_entries[0].get("archive_path") or ""))
        if cand.is_file():
            eff_archive = cand
    if archived_entries and eff_archive is not None and eff_archive.is_file() and eff_archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(eff_archive, "r") as zf:
            for item in archived_entries:
                entry_name = str(item.get("entry_name") or item.get("rel_path") or "")
                rel = str(item.get("rel_path") or entry_name)
                ext = str(item.get("ext") or Path(rel).suffix).lower()
                if ext != ".pak" or not entry_name:
                    continue
                with zf.open(entry_name, "r") as zstream:
                    res = _inspect_unreal_pak(
                        rel,
                        rel,
                        source_mod,
                        stream=zstream,
                        file_size=int(item.get("file_size") or 0),
                    )
                if res.get("mount_point") and res["mount_point"] not in mount_points:
                    mount_points.append(res["mount_point"])
                active_assets.extend(res.get("active_assets") or [])
                packed_backups.extend(b for b in (res.get("packed_backups") or []) if b not in packed_backups)
                hygiene_notes.extend(n for n in (res.get("hygiene_notes") or []) if n not in hygiene_notes)
                uproperty_symbols.extend(s for s in (res.get("uproperty_symbols") or []) if s not in uproperty_symbols)
                bak_diffs.extend(res.get("bak_diffs") or [])
                asset_paths.extend(p for p in res["asset_paths"] if p not in asset_paths)
                candidate_endpoints.extend(res["candidate_endpoints"])

    primary_manifest = manifests[0] if manifests else {}
    primary_plugin = plugins[0] if plugins else {}
    nexus_meta = parse_nexus_filename(bundle_name or safe_name)
    readme_desc = _extract_readme_description(audit_root, extracted_files)
    manifest_desc = str(primary_manifest.get("description") or "").strip()

    mod_name = str(
        primary_manifest.get("name")
        or primary_plugin.get("name")
        or nexus_meta.get("title")
        or bundle_name
        or Path(safe_name).stem
    ).strip()
    mod_version = str(
        primary_manifest.get("version")
        or primary_plugin.get("version")
        or nexus_meta.get("version")
        or ""
    ).strip()
    nexus_id = str(nexus_meta.get("nexus_id") or "").strip()
    description = readme_desc or manifest_desc
    now_iso = datetime.now(timezone.utc).isoformat()

    # Check cross-mod .pak asset collisions against other vault mods & installed ~mods/*.pak
    pak_sources: list[dict[str, Any]] = []
    if active_assets:
        pak_sources.append({
            "mod_id": mod_id,
            "mod_name": mod_name,
            "filename": safe_name,
            "source": "Mod Vault",
            "active_assets": active_assets,
        })
        for other in list_stored_mod_audits(clean_slug, app_data_root=app_data_root):
            other_id = str(other.get("mod_id") or "")
            if other_id == mod_id:
                continue
            other_assets = other.get("active_assets") or other.get("asset_paths") or []
            if other_assets:
                pak_sources.append({
                    "mod_id": other_id,
                    "mod_name": str(other.get("mod_name") or other.get("filename") or other_id),
                    "source": "Mod Vault",
                    "active_assets": other_assets,
                })
        gdir = Path(str(game.get("game_dir") or "").strip())
        if gdir.is_dir():
            for inst_pak in sorted(
                list(gdir.glob("Content/Paks/~mods/*.pak")) + list(gdir.glob("*/Content/Paks/~mods/*.pak")),
                key=lambda p: p.name.lower(),
            ):
                if inst_pak.name.lower() == safe_name.lower():
                    continue
                try:
                    inst_res = _inspect_unreal_pak(inst_pak)
                    if inst_res.get("active_assets"):
                        pak_sources.append({
                            "mod_id": f"installed:{inst_pak.name}",
                            "mod_name": inst_pak.name,
                            "source": "Installed ~mods",
                            "active_assets": inst_res["active_assets"],
                        })
                except Exception:
                    continue
    asset_conflicts = compute_pak_asset_conflicts(pak_sources) if pak_sources else []

    ext_fmt = Path(safe_name).suffix.lower().lstrip(".") or storage_mode
    report: dict[str, Any] = {
        "mod_id": mod_id,
        "slug": clean_slug,
        "filename": safe_name,
        "archive_name": safe_name,
        "mod_name": mod_name,
        "nexus_id": nexus_id,
        "version": mod_version,
        "description": description,
        "storage_mode": storage_mode,
        "linked_path": linked_path,
        "unpacked_dir": str(unpacked_dir),
        "format": ext_fmt,
        "source_label": str(source_label or ("Nexus Mods" if nexus_id else "")).strip(),
        "notes": str(notes or "").strip(),
        "file_size": file_size,
        "sha256": file_sha,
        "uploaded_at": now_iso,
        "vault_dir": str(mod_dir),
        "extracted_files": extracted_files,
        "archived_entries": list(archived_entries or []),
        "manifests": manifests,
        "plugins": plugins,
        "dependencies": dependencies,
        "hooks": hooks,
        "mount_points": mount_points,
        "active_assets": active_assets,
        "packed_backups": packed_backups,
        "hygiene_notes": hygiene_notes,
        "uproperty_symbols": uproperty_symbols,
        "bak_diffs": bak_diffs,
        "asset_conflicts": asset_conflicts,
        "asset_paths": asset_paths,
        "referenced_types": referenced_types[:80],
        "candidate_endpoints": candidate_endpoints,
    }
    report["blueprint_markdown"] = _build_blueprint_markdown(report)
    audit_path = ensure_within(mod_dir / "audit_report.json", mod_dir)
    atomic_write_json(audit_path, report)

    summary = {
        "mod_id": mod_id,
        "filename": safe_name,
        "archive_name": safe_name,
        "mod_name": mod_name,
        "nexus_id": nexus_id,
        "version": mod_version,
        "description": description,
        "storage_mode": storage_mode,
        "linked_path": linked_path,
        "format": ext_fmt,
        "source_label": report["source_label"],
        "notes": report["notes"],
        "file_size": file_size,
        "sha256": file_sha,
        "uploaded_at": now_iso,
        "plugins_count": len(plugins),
        "hooks_count": len(hooks),
        "endpoints_count": len(candidate_endpoints),
        "active_assets_count": len(active_assets),
        "packed_backups_count": len(packed_backups),
        "uproperty_symbols_count": len(uproperty_symbols),
        "asset_conflicts_count": len(asset_conflicts),
    }
    _sync_game_stored_mods_entry(clean_slug, summary, games_store_path=games_store_path)
    append_audit_log(
        action="mod_vault.upload_audit",
        status="ok",
        summary=f"Stored & statically audited '{mod_name}' ({storage_mode}) for '{clean_slug}' ({len(hooks)} hooks, {len(candidate_endpoints)} endpoints)",
        sha256=file_sha,
        details={"slug": clean_slug, "mod_id": mod_id, "storage_mode": storage_mode, "plugins": len(plugins), "hooks": len(hooks), "endpoints": len(candidate_endpoints)},
    )
    return {
        "status": "ok",
        "slug": clean_slug,
        "mod_id": mod_id,
        "summary": summary,
        "audit_report": report,
    }


def store_and_audit_mod(
    game: dict[str, Any],
    slug: str,
    filename: str,
    content_bytes: bytes,
    *,
    notes: str = "",
    source_label: str = "",
    max_bytes: int = DEFAULT_MAX_MOD_UPLOAD_BYTES,
    app_data_root: Path | None = None,
    games_store_path: Path | None = None,
) -> dict[str, Any]:
    """Validate, store under `mod_vault/<slug>/<mod_id>/`, statically audit, and persist summary on game record."""
    clean_slug = _clean_slug(slug or str(game.get("id") or game.get("name") or "game"))
    safe_name = _sanitize_upload_filename(filename)

    if len(content_bytes) > max_bytes:
        raise UploadTooLargeError(
            f"Upload size ({len(content_bytes)} bytes) exceeds max limit ({max_bytes} bytes)."
        )
    if not content_bytes:
        raise ValueError("Uploaded mod file is empty.")

    mod_id = _validate_mod_id(f"mod_{make_slug(Path(safe_name).stem)[:24]}_{uuid.uuid4().hex[:8]}")
    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    mod_dir = ensure_within(game_vault / mod_id, game_vault)
    orig_dir = ensure_within(mod_dir / "original", mod_dir)
    unpacked_dir = ensure_within(mod_dir / "unpacked", mod_dir)
    orig_dir.mkdir(parents=True, exist_ok=True)
    unpacked_dir.mkdir(parents=True, exist_ok=True)

    orig_file = ensure_within(orig_dir / safe_name, orig_dir)
    orig_file.write_bytes(content_bytes)
    file_sha = sha256_file(orig_file)
    ext = orig_file.suffix.lower()
    storage_mode = "zip" if ext in (".zip", ".7z") else "unpacked"

    try:
        archived_entries: list[dict[str, Any]] = []
        if ext == ".zip":
            extracted_files, archived_entries = _safe_extract_zip_selective(orig_file, unpacked_dir)
        elif ext == ".7z":
            extracted_files = _safe_extract_7z(orig_file, unpacked_dir)
        else:
            single_copy = ensure_within(unpacked_dir / safe_name, unpacked_dir)
            shutil.copy2(orig_file, single_copy)
            extracted_files = [safe_name]

        return _finalize_mod_audit(
            game,
            clean_slug,
            mod_id,
            mod_dir,
            unpacked_dir,
            unpacked_dir,
            extracted_files,
            safe_name,
            len(content_bytes),
            file_sha,
            storage_mode=storage_mode,
            notes=notes,
            source_label=source_label,
            archived_entries=archived_entries,
            original_archive=orig_file if ext == ".zip" else None,
            app_data_root=app_data_root,
            games_store_path=games_store_path,
        )
    except Exception:
        shutil.rmtree(mod_dir, ignore_errors=True)
        raise


async def store_and_audit_streamed_uploads(
    game: dict[str, Any],
    slug: str,
    uploads: list[Any],
    *,
    bundle_name: str = "",
    notes: str = "",
    source_label: str = "",
    max_bytes: int = DEFAULT_MAX_MOD_UPLOAD_BYTES,
    app_data_root: Path | None = None,
    games_store_path: Path | None = None,
) -> dict[str, Any]:
    """Stream one or more uploaded archives or loose unpacked files/folders into the Mod Vault."""
    clean_slug = _clean_slug(slug or str(game.get("id") or game.get("name") or "game"))
    valid_uploads = [u for u in uploads if u is not None and str(getattr(u, "filename", "") or "").strip()]
    if not valid_uploads:
        raise ValueError("No uploaded files provided.")

    archive_uploads: list[Any] = []
    loose_uploads: list[Any] = []
    for u in valid_uploads:
        raw_fn = str(getattr(u, "filename", "") or "").replace("\\", "/").strip()
        if Path(raw_fn).suffix.lower() in (".zip", ".7z"):
            _sanitize_upload_filename(raw_fn)
            archive_uploads.append(u)
        else:
            loose_uploads.append(u)

    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    results: list[dict[str, Any]] = []

    for arch in archive_uploads:
        safe_name = _sanitize_upload_filename(str(arch.filename or "mod.zip"))
        mod_id = _validate_mod_id(f"mod_{make_slug(Path(safe_name).stem)[:24]}_{uuid.uuid4().hex[:8]}")
        mod_dir = ensure_within(game_vault / mod_id, game_vault)
        orig_dir = ensure_within(mod_dir / "original", mod_dir)
        unpacked_dir = ensure_within(mod_dir / "unpacked", mod_dir)
        orig_dir.mkdir(parents=True, exist_ok=True)
        unpacked_dir.mkdir(parents=True, exist_ok=True)
        orig_file = ensure_within(orig_dir / safe_name, orig_dir)
        try:
            fsize, fsha = await stream_upload_to_path(arch, orig_file, max_bytes=max_bytes)
            ext = orig_file.suffix.lower()
            archived_entries: list[dict[str, Any]] = []
            if ext == ".zip":
                extracted, archived_entries = _safe_extract_zip_selective(orig_file, unpacked_dir)
            else:
                extracted = _safe_extract_7z(orig_file, unpacked_dir)
            results.append(
                _finalize_mod_audit(
                    game,
                    clean_slug,
                    mod_id,
                    mod_dir,
                    unpacked_dir,
                    unpacked_dir,
                    extracted,
                    safe_name,
                    fsize,
                    fsha,
                    storage_mode="zip",
                    notes=notes,
                    source_label=source_label,
                    archived_entries=archived_entries,
                    original_archive=orig_file if ext == ".zip" else None,
                    app_data_root=app_data_root,
                    games_store_path=games_store_path,
                )
            )
        except Exception:
            shutil.rmtree(mod_dir, ignore_errors=True)
            raise

    if loose_uploads:
        first_rel = str(getattr(loose_uploads[0], "filename", "") or "unpacked_mod").replace("\\", "/").strip()
        inferred_bundle = str(bundle_name or "").strip()
        if not inferred_bundle:
            parts = [p for p in first_rel.split("/") if p]
            inferred_bundle = parts[0] if len(parts) > 1 else Path(first_rel).stem
        safe_bundle_stem = make_slug(inferred_bundle)[:24] or "bundle"
        mod_id = _validate_mod_id(f"mod_{safe_bundle_stem}_{uuid.uuid4().hex[:8]}")
        mod_dir = ensure_within(game_vault / mod_id, game_vault)
        unpacked_dir = ensure_within(mod_dir / "unpacked", mod_dir)
        unpacked_dir.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        combined_sha = hashlib.sha256()
        rel_files: list[str] = []
        try:
            for u in loose_uploads:
                rel = _sanitize_unpacked_relpath(str(u.filename or ""), unpacked_dir)
                dest = ensure_within(unpacked_dir / rel, unpacked_dir)
                rem_budget = max_bytes - total_bytes
                if rem_budget <= 0:
                    raise UploadTooLargeError(f"Unpacked bundle exceeds max limit ({max_bytes} bytes).")
                fsize, fsha = await stream_upload_to_path(u, dest, max_bytes=rem_budget)
                total_bytes += fsize
                combined_sha.update(fsha.encode("ascii"))
                rel_files.append(rel)

            display_name = str(bundle_name or "").strip() or (first_rel if len(loose_uploads) == 1 else f"{inferred_bundle}")
            results.append(
                _finalize_mod_audit(
                    game,
                    clean_slug,
                    mod_id,
                    mod_dir,
                    unpacked_dir,
                    unpacked_dir,
                    rel_files,
                    display_name,
                    total_bytes,
                    combined_sha.hexdigest(),
                    storage_mode="unpacked",
                    bundle_name=bundle_name or inferred_bundle,
                    notes=notes,
                    source_label=source_label,
                    app_data_root=app_data_root,
                    games_store_path=games_store_path,
                )
            )
        except Exception:
            shutil.rmtree(mod_dir, ignore_errors=True)
            raise

    payload: dict[str, Any] = {
        "status": "ok",
        "slug": clean_slug,
        "count": len(results),
        "uploaded": results,
    }
    if len(results) == 1:
        payload["mod_id"] = results[0]["mod_id"]
        payload["summary"] = results[0]["summary"]
        payload["audit_report"] = results[0]["audit_report"]
    return payload


def ingest_local_mod_path(
    game: dict[str, Any],
    local_path: Path | str,
    bundle_name: str | None = None,
    *,
    slug: str = "",
    notes: str = "",
    source_label: str = "Local Path",
    app_data_root: Path | None = None,
    games_store_path: Path | None = None,
) -> dict[str, Any]:
    """Audit and register a local unpacked mod folder, `.zip`, or `.pak`/config file on disk."""
    raw_str = str(local_path or "").strip()
    if not raw_str:
        raise ValueError("local_path is required.")
    src = Path(raw_str).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Local mod path not found: {src}")

    clean_slug = _clean_slug(slug or str(game.get("id") or game.get("name") or "game"))
    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    eff_label = str(bundle_name or "").strip() or src.name
    mod_id = _validate_mod_id(f"mod_{make_slug(eff_label)[:24]}_{uuid.uuid4().hex[:8]}")
    mod_dir = ensure_within(game_vault / mod_id, game_vault)
    unpacked_dir = ensure_within(mod_dir / "unpacked", mod_dir)
    unpacked_dir.mkdir(parents=True, exist_ok=True)

    try:
        if src.is_dir():
            rel_files: list[str] = []
            total_size = 0
            digest = hashlib.sha256()
            for item in sorted(src.rglob("*"), key=lambda p: p.as_posix().lower()):
                if item.is_symlink() or not item.is_file():
                    continue
                if len(rel_files) >= _MAX_ARCHIVE_ENTRIES:
                    break
                ext = item.suffix.lower()
                if ext not in _UNPACKED_SAFE_EXTENSIONS:
                    continue
                safe_item = ensure_within(item, src)
                rel = safe_item.relative_to(src).as_posix()
                rel_files.append(rel)
                total_size += safe_item.stat().st_size
                digest.update(rel.encode("utf-8", errors="ignore"))
            if not rel_files:
                raise ValueError(f"No supported mod files found inside local folder: {src}")
            return _finalize_mod_audit(
                game,
                clean_slug,
                mod_id,
                mod_dir,
                unpacked_dir,
                src,
                rel_files,
                eff_label,
                total_size,
                digest.hexdigest(),
                storage_mode="linked",
                linked_path=str(src),
                bundle_name=bundle_name or src.name,
                notes=notes,
                source_label=source_label,
                app_data_root=app_data_root,
                games_store_path=games_store_path,
            )

        # Single local file (.zip, .7z, .pak, .dll, .ini, .cfg, .lua, etc.)
        ext = src.suffix.lower()
        if ext not in _ALLOWED_UPLOAD_EXTENSIONS:
            raise ValueError(f"Unsupported local mod file extension '{ext or '(none)'}'.")
        total_size = src.stat().st_size
        fsha = sha256_file(src)
        archived_entries: list[dict[str, Any]] = []
        if ext == ".zip":
            extracted, archived_entries = _safe_extract_zip_selective(src, unpacked_dir)
            audit_root = unpacked_dir
        elif ext == ".7z":
            extracted = _safe_extract_7z(src, unpacked_dir)
            audit_root = unpacked_dir
        else:
            extracted = [src.name]
            audit_root = src.parent

        return _finalize_mod_audit(
            game,
            clean_slug,
            mod_id,
            mod_dir,
            unpacked_dir,
            audit_root,
            extracted,
            eff_label,
            total_size,
            fsha,
            storage_mode="linked",
            linked_path=str(src),
            bundle_name=bundle_name or src.stem,
            notes=notes,
            source_label=source_label,
            archived_entries=archived_entries,
            original_archive=src if ext == ".zip" else None,
            app_data_root=app_data_root,
            games_store_path=games_store_path,
        )
    except Exception:
        shutil.rmtree(mod_dir, ignore_errors=True)
        raise


def edit_stored_mod_value(
    slug: str,
    mod_id: str,
    *,
    rel_file: str,
    symbol: str,
    new_value: str,
    app_data_root: Path | None = None,
) -> dict[str, Any]:
    """Edit an inline config/script value inside an unpacked or linked stored mod."""
    clean_slug = _clean_slug(slug)
    clean_mod = _validate_mod_id(mod_id)
    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    mod_dir = ensure_within(game_vault / clean_mod, game_vault)
    report = get_stored_mod_audit(clean_slug, clean_mod, app_data_root=app_data_root)
    content_root = _resolve_audit_content_dir(mod_dir, report)

    norm_rel = str(rel_file or "").replace("\\", "/").strip().lstrip("/")
    if not norm_rel:
        raise ValueError("rel_file is required.")
    target_file = ensure_within(content_root / norm_rel, content_root)
    if not target_file.is_file():
        raise FileNotFoundError(f"Unpacked mod file not found: '{norm_rel}'")

    ext = target_file.suffix.lower()
    raw_val = str(new_value)
    if "\n" in raw_val or "\r" in raw_val:
        raise ValueError("Value must be a single line.")
    raw_text = target_file.read_text(encoding="utf-8-sig", errors="ignore")
    updated_text = raw_text

    if ext in (".ini", ".cfg"):
        matched_cand = next(
            (
                c
                for c in (report.get("candidate_endpoints") or [])
                if str(c.get("symbol") or "").strip() == symbol
                and (not c.get("file_path") or str(c.get("file_path") or "").replace("\\", "/").strip() == norm_rel)
            ),
            None,
        )
        if matched_cand and matched_cand.get("key"):
            sec_name = str(matched_cand.get("section") or "").strip()
            key_name = str(matched_cand.get("key") or "").strip()
        elif "." in symbol:
            sec_name, key_name = symbol.rsplit(".", 1)
        else:
            sec_name, key_name = "", symbol
        lines = raw_text.splitlines(keepends=True)
        cur_sec = ""
        replaced = False
        out_lines: list[str] = []
        key_rx = re.compile(rf"^(\s*{re.escape(key_name)}\s*=\s*)(.*?)(\r?\n?)$")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                cur_sec = stripped[1:-1].strip()
            elif not stripped.startswith((";", "#")) and (not sec_name or cur_sec.lower() == sec_name.lower()):
                m = key_rx.match(line)
                if m and not replaced:
                    out_lines.append(f"{m.group(1)}{raw_val}{m.group(3)}")
                    replaced = True
                    continue
            out_lines.append(line)
        if not replaced:
            raise ValueError(f"Setting '{symbol}' not found in '{norm_rel}'.")
        updated_text = "".join(out_lines)
    elif ext == ".lua":
        escaped_sym = re.escape(symbol)
        lua_rx = re.compile(
            rf"^(\s*(?:local\s+)?{escaped_sym}\s*=\s*)(true|false|-?\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*')([ \t]*(?:--.*)?\r?\n?)$",
            re.MULTILINE,
        )
        m = lua_rx.search(raw_text)
        if not m:
            raise ValueError(f"Lua variable '{symbol}' not found in '{norm_rel}'.")
        old_lit = m.group(2)
        if (old_lit.startswith('"') and old_lit.endswith('"')) or (old_lit.startswith("'") and old_lit.endswith("'")):
            q = old_lit[0]
            formatted = f"{q}{raw_val}{q}"
        else:
            formatted = raw_val
        updated_text = raw_text[:m.start(2)] + formatted + raw_text[m.end(2):]
    else:
        raise ValueError(f"Inline editing is supported for .ini, .cfg, and .lua files (got '{ext}').")

    target_file.write_text(updated_text, encoding="utf-8")

    for cand in report.get("candidate_endpoints") or []:
        c_sym = str(cand.get("symbol") or "").strip()
        c_file = str(cand.get("file_path") or "").replace("\\", "/").strip()
        if c_sym == symbol and (not c_file or c_file == norm_rel):
            cand["current_value"] = raw_val
            cand["default_value"] = raw_val
    report["blueprint_markdown"] = _build_blueprint_markdown(report)
    audit_path = ensure_within(mod_dir / "audit_report.json", mod_dir)
    atomic_write_json(audit_path, report)

    append_audit_log(
        action="mod_vault.edit_value",
        status="ok",
        summary=f"Edited '{symbol}' = '{raw_val}' inside stored mod '{clean_mod}' ({norm_rel})",
        details={"slug": clean_slug, "mod_id": clean_mod, "file": norm_rel, "symbol": symbol, "value": raw_val},
    )
    return {
        "status": "ok",
        "slug": clean_slug,
        "mod_id": clean_mod,
        "file_path": norm_rel,
        "symbol": symbol,
        "new_value": raw_val,
        "audit_report": report,
    }


def deploy_stored_mod(
    game: dict[str, Any],
    slug: str,
    mod_id: str,
    *,
    app_data_root: Path | None = None,
) -> dict[str, Any]:
    """Snapshot the game directory first, then deploy active stored/unpacked mod files."""
    clean_slug = _clean_slug(slug or str(game.get("id") or game.get("name") or "game"))
    clean_mod = _validate_mod_id(mod_id)
    gdir_raw = str(game.get("game_dir") or "").strip()
    gdir = Path(gdir_raw).resolve() if gdir_raw else Path()
    if not gdir_raw or not gdir.is_dir():
        raise ValueError(f"Target game directory does not exist for '{clean_slug}'.")

    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    mod_dir = ensure_within(game_vault / clean_mod, game_vault)
    report = get_stored_mod_audit(clean_slug, clean_mod, app_data_root=app_data_root)
    content_root = _resolve_audit_content_dir(mod_dir, report)

    # Create pre-deploy safety snapshot first
    snapshots_root = (Path(app_data_root) / "game_snapshots") if app_data_root is not None else None
    engine_hint = str(game.get("runtime") or game.get("detected_engine") or "")
    snap = create_game_snapshot(
        clean_slug,
        gdir,
        label=f"pre-deploy-{clean_mod}",
        engine=engine_hint,
        snapshots_root=snapshots_root,
    )

    # Resolve Unreal ~mods destination if needed
    ue_mods_dir: Path | None = None
    for cand_dir in sorted(gdir.glob("*/Content/Paks"), key=lambda p: p.as_posix().lower()):
        if cand_dir.is_dir():
            ue_mods_dir = ensure_within(cand_dir / "~mods", gdir)
            break
    if ue_mods_dir is None:
        if (gdir / "Content" / "Paks").is_dir() or "unreal" in engine_hint.lower():
            ue_mods_dir = ensure_within(gdir / "Content" / "Paks" / "~mods", gdir)

    mod_folder_name = make_slug(str(report.get("mod_name") or clean_mod))[:48] or clean_mod
    deployed_files: list[str] = []

    extracted_files = [str(f) for f in (report.get("extracted_files") or [])]
    if not extracted_files and report.get("linked_path") and Path(str(report["linked_path"])).is_file():
        extracted_files = [Path(str(report["linked_path"])).name]

    for rel in extracted_files:
        norm = rel.replace("\\", "/").strip()
        if not norm or _PAK_BACKUP_RE.search(norm):
            continue
        name_low = Path(norm).name.lower()
        if name_low in ("readme.txt", "readme.md", "manifest.json", "icon.png", "info.xml", "moduleconfig.xml"):
            continue
        src_file = ensure_within(content_root / norm, content_root)
        if not src_file.is_file():
            continue

        ext = src_file.suffix.lower()
        parts_low = [p.lower() for p in Path(norm).parts]

        if ext in (".pak", ".ucas", ".utoc"):
            dest_base = ue_mods_dir or ensure_within(gdir / "Content" / "Paks" / "~mods", gdir)
            dst_file = ensure_within(dest_base / src_file.name, gdir)
        elif "bepinex" in parts_low:
            idx = parts_low.index("bepinex")
            dst_file = ensure_within(gdir.joinpath(*Path(norm).parts[idx:]), gdir)
        elif ext == ".dll":
            dst_file = ensure_within(gdir / "BepInEx" / "plugins" / mod_folder_name / src_file.name, gdir)
        elif ext == ".cfg":
            dst_file = ensure_within(gdir / "BepInEx" / "config" / src_file.name, gdir)
        elif ext == ".lua":
            ue4ss_mods = next(
                (p for p in sorted(gdir.rglob("Mods")) if p.is_dir() and "ue4ss" in p.as_posix().lower()),
                ensure_within(gdir / "ue4ss" / "Mods", gdir),
            )
            dst_file = ensure_within(ue4ss_mods / mod_folder_name / "Scripts" / src_file.name, gdir)
        else:
            dst_file = ensure_within(gdir / Path(norm).name, gdir)

        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        deployed_files.append(dst_file.relative_to(gdir).as_posix())

    # Stream any large binary entries kept inside original/<archive.zip> directly to game target
    archived_entries = [item for item in (report.get("archived_entries") or []) if isinstance(item, dict)]
    if archived_entries:
        orig_dir = ensure_within(mod_dir / "original", mod_dir)
        fallback_arch = orig_dir / str(report.get("filename") or "")
        if not fallback_arch.is_file() and report.get("linked_path"):
            cand_link = Path(str(report["linked_path"]))
            if cand_link.is_file():
                fallback_arch = cand_link

        by_arch: dict[Path, list[dict[str, Any]]] = {}
        for item in archived_entries:
            arch_raw = str(item.get("archive_path") or "").strip()
            arch_p = Path(arch_raw) if arch_raw and Path(arch_raw).is_file() else fallback_arch
            if arch_p.is_file() and arch_p.suffix.lower() == ".zip":
                by_arch.setdefault(arch_p, []).append(item)

        for arch_p, entries in by_arch.items():
            with zipfile.ZipFile(arch_p, "r") as zf:
                for item in entries:
                    entry_name = str(item.get("entry_name") or item.get("rel_path") or "")
                    norm = str(item.get("rel_path") or entry_name).replace("\\", "/").strip()
                    if not entry_name or not norm or _PAK_BACKUP_RE.search(norm):
                        continue
                    _validate_archive_entry_path(entry_name, gdir)
                    fname = Path(norm).name
                    ext = Path(fname).suffix.lower()
                    parts_low = [p.lower() for p in Path(norm).parts]
                    if ext in (".pak", ".ucas", ".utoc"):
                        dest_base = ue_mods_dir or ensure_within(gdir / "Content" / "Paks" / "~mods", gdir)
                        dst_file = ensure_within(dest_base / fname, gdir)
                    elif "bepinex" in parts_low:
                        idx = parts_low.index("bepinex")
                        dst_file = ensure_within(gdir.joinpath(*Path(norm).parts[idx:]), gdir)
                    else:
                        dst_file = ensure_within(gdir / fname, gdir)
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(entry_name, "r") as zstream, dst_file.open("wb") as out_f:
                        shutil.copyfileobj(zstream, out_f, length=_STREAM_CHUNK_BYTES)
                    deployed_files.append(dst_file.relative_to(gdir).as_posix())

    append_audit_log(
        action="mod_vault.deploy",
        status="ok",
        summary=f"Deployed {len(deployed_files)} file(s) from '{clean_mod}' to '{clean_slug}' (snapshot '{snap['snapshot_id']}')",
        details={"slug": clean_slug, "mod_id": clean_mod, "snapshot_id": snap["snapshot_id"], "deployed_files": deployed_files},
    )
    return {
        "status": "ok",
        "slug": clean_slug,
        "mod_id": clean_mod,
        "snapshot_id": snap["snapshot_id"],
        "deployed_files": deployed_files,
        "target_dir": str(gdir),
    }


def list_stored_mods(slug: str, *, app_data_root: Path | None = None) -> list[dict[str, Any]]:
    clean_slug = _clean_slug(slug)
    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    if not game_vault.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for mod_dir in sorted(game_vault.iterdir(), key=lambda p: p.name.lower()):
        if not mod_dir.is_dir():
            continue
        rep_path = mod_dir / "audit_report.json"
        if rep_path.is_file():
            rep = load_json_sanitized(rep_path, persist=False)
            if isinstance(rep, dict):
                conflicts = rep.get("asset_conflicts") or []
                active_assets = rep.get("active_assets") or []
                packed_backups = rep.get("packed_backups") or []
                uprops = rep.get("uproperty_symbols") or []
                endpoints = rep.get("candidate_endpoints") or []
                summary_metrics = {
                    "plugins_count": len(rep.get("plugins") or []),
                    "hooks_count": len(rep.get("hooks") or []),
                    "endpoints_count": len(endpoints),
                    "candidate_endpoints_count": len(endpoints),
                    "active_assets_count": len(active_assets),
                    "packed_backups_count": len(packed_backups),
                    "uproperty_symbols_count": len(uprops),
                    "asset_conflicts_count": len(conflicts),
                }
                out.append({
                    "mod_id": rep.get("mod_id") or mod_dir.name,
                    "filename": rep.get("filename") or "",
                    "archive_name": rep.get("archive_name") or rep.get("filename") or "",
                    "mod_name": rep.get("mod_name") or mod_dir.name,
                    "nexus_id": rep.get("nexus_id") or "",
                    "version": rep.get("version") or "",
                    "description": rep.get("description") or "",
                    "storage_mode": rep.get("storage_mode") or "zip",
                    "linked_path": rep.get("linked_path") or "",
                    "format": rep.get("format") or "",
                    "source_label": rep.get("source_label") or "",
                    "notes": rep.get("notes") or "",
                    "file_size": int(rep.get("file_size") or 0),
                    "sha256": rep.get("sha256") or "",
                    "uploaded_at": rep.get("uploaded_at") or "",
                    **summary_metrics,
                    "summary": summary_metrics,
                })
    return out


def get_stored_mod_audit(slug: str, mod_id: str, *, app_data_root: Path | None = None) -> dict[str, Any]:
    clean_slug = _clean_slug(slug)
    clean_mod = _validate_mod_id(mod_id)
    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    mod_dir = ensure_within(game_vault / clean_mod, game_vault)
    rep_path = ensure_within(mod_dir / "audit_report.json", mod_dir)
    if not rep_path.is_file():
        raise FileNotFoundError(f"Stored mod '{clean_mod}' audit report not found for '{clean_slug}'.")
    rep = load_json_sanitized(rep_path, persist=False)
    if not isinstance(rep, dict):
        raise ValueError(f"Corrupt audit_report.json for '{clean_mod}'.")
    return rep


def list_stored_mod_audits(slug: str, *, app_data_root: Path | None = None) -> list[dict[str, Any]]:
    clean_slug = _clean_slug(slug)
    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    if not game_vault.is_dir():
        return []
    reports: list[dict[str, Any]] = []
    for mod_dir in sorted(game_vault.iterdir(), key=lambda p: p.name.lower()):
        rep_path = mod_dir / "audit_report.json"
        if mod_dir.is_dir() and rep_path.is_file():
            rep = load_json_sanitized(rep_path, persist=False)
            if isinstance(rep, dict):
                reports.append(rep)
    return reports


def promote_stored_mod_endpoints(
    game: dict[str, Any],
    slug: str,
    mod_id: str,
    *,
    symbols: list[str] | None = None,
    category_override: str = "",
    app_data_root: Path | None = None,
    knowledge_path: Path | None = None,
) -> dict[str, Any]:
    """Merge audited candidate endpoints/hooks into `endpoint_knowledge.json` and game's endpoint catalog."""
    clean_slug = _clean_slug(slug or str(game.get("id") or game.get("name") or "game"))
    eff_root = Path(app_data_root) if app_data_root is not None else APP_DATA_ROOT
    report = get_stored_mod_audit(clean_slug, mod_id, app_data_root=eff_root)

    rt_low = str(game.get("runtime") or "").lower()
    engine = "unreal" if "unreal" in rt_low else ("unity" if ("unity" in rt_low or "bepinex" in rt_low) else "generic")
    filter_set = {s.strip() for s in (symbols or []) if str(s).strip()}
    cat_ovr = category_override.strip().lower() if category_override.strip().lower() in _VALID_WORKFLOW_CATEGORIES else ""

    imp_file = get_imported_endpoints_dir(app_data_root=eff_root) / f"{clean_slug}.json"
    existing_imp = load_game_imported_endpoints(clean_slug, app_data_root=eff_root)
    by_id: dict[str, dict[str, Any]] = {str(e.get("id")): e for e in existing_imp if isinstance(e, dict) and e.get("id")}

    candidates: list[dict[str, Any]] = list(report.get("candidate_endpoints") or [])
    seen_syms = {str(c.get("symbol") or "").strip() for c in candidates}
    for hk in (report.get("hooks") or []):
        sig = str(hk.get("signature") or "").strip()
        if sig and sig not in seen_syms:
            seen_syms.add(sig)
            tok = str(hk.get("target_method") or sig)
            res_hk = resolve_endpoint_classification(tok, engine=engine, reference_candidates=[sig, tok])
            candidates.append({
                "symbol": sig,
                "name": f"Hook: {sig}",
                "category": res_hk["category"] if res_hk["category"] != "uncategorized" else "game_systems",
                "description": f"Promoted {hk.get('kind')} hook from stored mod '{report.get('filename')}'.",
                "default_value": "Vanilla",
                "value_type": "string",
                "delivery_type": "Mod File",
                "source_mod": report.get("filename"),
                "source_kind": hk.get("kind") or "mod_hook",
            })

    promoted: list[dict[str, Any]] = []
    knowledge_items: list[dict[str, Any]] = []
    for cand in candidates:
        sym = str(cand.get("symbol") or "").strip()
        if not sym or (filter_set and sym not in filter_set):
            continue
        cat = cat_ovr or str(cand.get("category") or "uncategorized").strip().lower()
        if cat not in _VALID_WORKFLOW_CATEGORIES:
            cat = "uncategorized"
        ep_id = f"vault_mod:{make_slug(mod_id)}:{sha256_text(sym)[:12]}"
        def_val = str(cand.get("default_value") if cand.get("default_value") is not None else "")
        ep_rec = {
            "id": ep_id,
            "category": cat,
            "category_label": _ALL_CATEGORY_LABELS.get(cat, "Uncategorized"),
            "name": str(cand.get("name") or sym),
            "simple_description": str(cand.get("description") or f"Promoted from stored mod '{report.get('filename')}'."),
            "value_type": str(cand.get("value_type") or "string"),
            "options": list(cand.get("options") or []),
            "min": cand.get("min"),
            "max": cand.get("max"),
            "default_value": def_val,
            "current_value": def_val,
            "delivery_type": str(cand.get("delivery_type") or "Mod File"),
            "can_edit": False,
            "classification_source": "mod_vault_audit",
            "confidence": "high",
            "advanced_details": {
                "source_kind": str(cand.get("source_kind") or "mod_vault_audit"),
                "source_mod": str(cand.get("source_mod") or report.get("filename") or ""),
                "vault_mod_id": mod_id,
                "symbol": sym,
                "method_signature": sym,
            },
        }
        by_id[ep_id] = ep_rec
        promoted.append(ep_rec)

        if cat != "uncategorized":
            knowledge_items.append({
                "engine": engine,
                "symbol": extract_endpoint_symbol(ep_rec),
                "category": cat,
                "simple_label": ep_rec["name"],
                "simple_description": ep_rec["simple_description"],
                "value_type": ep_rec["value_type"],
                "delivery_type": ep_rec["delivery_type"],
                "min": ep_rec.get("min"),
                "max": ep_rec.get("max"),
                "source": "mod_vault_audit",
                "confidence": "high",
            })

    now_iso = datetime.now(timezone.utc).isoformat()
    atomic_write_json(
        imp_file,
        {
            "slug": clean_slug,
            "updated_at": now_iso,
            "endpoints": list(by_id.values()),
        },
    )
    k_path = record_endpoint_knowledge(knowledge_items, knowledge_path=knowledge_path, app_data_root=eff_root)
    updated_report = scan_game_endpoints(game, defaults_dir=eff_root / "game_defaults")
    append_audit_log(
        action="mod_vault.promote_endpoints",
        status="ok",
        summary=f"Promoted {len(promoted)} endpoint(s) from stored mod '{mod_id}' into '{clean_slug}'",
        details={"slug": clean_slug, "mod_id": mod_id, "promoted_count": len(promoted), "knowledge_path": str(k_path)},
    )
    return {
        "status": "ok",
        "slug": clean_slug,
        "mod_id": mod_id,
        "promoted_count": len(promoted),
        "promoted_endpoints": promoted,
        "knowledge_path": str(k_path),
        "report": updated_report,
    }


def delete_stored_mod(
    slug: str,
    mod_id: str,
    *,
    app_data_root: Path | None = None,
    games_store_path: Path | None = None,
) -> dict[str, Any]:
    clean_slug = _clean_slug(slug)
    clean_mod = _validate_mod_id(mod_id)
    vault_root = get_mod_vault_root(app_data_root=app_data_root)
    game_vault = ensure_within(vault_root / clean_slug, vault_root)
    mod_dir = ensure_within(game_vault / clean_mod, game_vault)
    if not mod_dir.is_dir():
        raise FileNotFoundError(f"Stored mod '{clean_mod}' not found for '{clean_slug}'.")
    shutil.rmtree(mod_dir)
    _sync_game_stored_mods_entry(clean_slug, None, remove_mod_id=clean_mod, games_store_path=games_store_path)
    append_audit_log(
        action="mod_vault.delete",
        status="ok",
        summary=f"Deleted stored mod '{clean_mod}' from vault for '{clean_slug}'",
        details={"slug": clean_slug, "mod_id": clean_mod},
    )
    return {"status": "ok", "slug": clean_slug, "mod_id": clean_mod}

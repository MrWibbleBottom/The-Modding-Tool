"""Per-game modded-surface snapshot and one-click rollback service."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.audit import append_audit_log
from app.core.config import APP_DATA_ROOT, atomic_write_json, sha256_file, sha256_text
from app.services.discovery import make_slug

GAME_SNAPSHOTS_DIR = APP_DATA_ROOT / "game_snapshots"
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def ensure_within(target: Path | str, base_dir: Path | str) -> Path:
    """Return resolved *target* if it lies inside *base_dir*; raise ValueError on traversal."""
    resolved = Path(target).resolve()
    base = Path(base_dir).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Path traversal blocked: '{target}' resolves outside '{base}'")
    return resolved


def _validate_segment(value: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean or clean in (".", "..") or ".." in clean or not _SAFE_SEGMENT_RE.match(clean):
        raise ValueError(f"Invalid {label}: '{value}'")
    return clean


def _get_snapshots_root(snapshots_root: Path | None = None) -> Path:
    return Path(snapshots_root) if snapshots_root is not None else GAME_SNAPSHOTS_DIR


def _get_local_appdata_root() -> Path:
    raw = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(raw)


def _detect_snapshot_engine(game_dir: Path, engine_hint: str = "") -> str:
    low = str(engine_hint or "").lower()
    if "unity" in low or "il2cpp" in low or "mono" in low:
        return "unity"
    if "unreal" in low:
        return "unreal"
    if game_dir.is_dir():
        if (
            (game_dir / "GameAssembly.dll").is_file()
            or (game_dir / "BepInEx").is_dir()
            or (game_dir / "doorstop_config.ini").is_file()
            or bool(list(game_dir.glob("*_Data")))
        ):
            return "unity"
        if (
            any(game_dir.rglob("*.pak"))
            or (game_dir / "Engine" / "Binaries").is_dir()
            or any(p.is_dir() and p.name.lower() == "~mods" for p in game_dir.rglob("~mods"))
        ):
            return "unreal"
    return "generic"


def _collect_game_surface_files(
    game_dir: Path,
    engine: str,
    extra_ini_paths: list[Path] | None = None,
) -> tuple[list[tuple[Path, str, str, Path]], list[str]]:
    """Collect ``(src_file, rel_to_scope, scope_name, scope_base)`` and tracked surface dirs."""
    items: list[tuple[Path, str, str, Path]] = []
    seen_srcs: set[Path] = set()
    tracked_surfaces: list[str] = []

    def _add_file(src: Path, scope_name: str, scope_base: Path) -> None:
        if not src.is_file():
            return
        src_r = ensure_within(src, scope_base)
        if src_r in seen_srcs:
            return
        seen_srcs.add(src_r)
        rel = src_r.relative_to(scope_base.resolve()).as_posix()
        items.append((src_r, rel, scope_name, scope_base.resolve()))

    def _add_tree(sub_dir: Path, scope_base: Path) -> None:
        if not sub_dir.is_dir():
            return
        sub_r = ensure_within(sub_dir, scope_base)
        rel_dir = sub_r.relative_to(scope_base.resolve()).as_posix()
        if rel_dir not in tracked_surfaces:
            tracked_surfaces.append(rel_dir)
        for f in sorted(sub_r.rglob("*")):
            if f.is_file():
                _add_file(f, "game", scope_base)

    if game_dir.is_dir():
        g_base = game_dir.resolve()
        if engine in ("unity", "generic"):
            if (g_base / "BepInEx" / "plugins").is_dir() or engine == "unity":
                if "BepInEx/plugins" not in tracked_surfaces:
                    tracked_surfaces.append("BepInEx/plugins")
                _add_tree(g_base / "BepInEx" / "plugins", g_base)
            if (g_base / "BepInEx" / "config").is_dir() or engine == "unity":
                if "BepInEx/config" not in tracked_surfaces:
                    tracked_surfaces.append("BepInEx/config")
                _add_tree(g_base / "BepInEx" / "config", g_base)
            if engine == "unity" or (g_base / "doorstop_config.ini").is_file():
                tracked_surfaces.append("doorstop_config.ini")
                _add_file(g_base / "doorstop_config.ini", "game", g_base)

        if engine in ("unreal", "generic"):
            for candidate in sorted(g_base.rglob("*")):
                if candidate.is_dir() and candidate.name.lower() == "~mods":
                    _add_tree(candidate, g_base)
            for plat in ("WindowsNoEditor", "Windows"):
                for cfg_dir in sorted(g_base.rglob(plat)):
                    if (
                        cfg_dir.is_dir()
                        and cfg_dir.parent.name.lower() == "config"
                        and cfg_dir.parent.parent.name.lower() == "saved"
                    ):
                        rel_cfg = ensure_within(cfg_dir, g_base).relative_to(g_base).as_posix()
                        if rel_cfg not in tracked_surfaces:
                            tracked_surfaces.append(rel_cfg)
                        for ini_f in sorted(cfg_dir.glob("*.ini")):
                            _add_file(ini_f, "game", g_base)

    if extra_ini_paths:
        local_app = _get_local_appdata_root().resolve()
        g_resolved = game_dir.resolve() if game_dir.is_dir() else None
        for p in extra_ini_paths:
            if not p or not p.is_file():
                continue
            p_res = p.resolve()
            if g_resolved is not None and p_res.is_relative_to(g_resolved):
                _add_file(p_res, "game", g_resolved)
            elif p_res.is_relative_to(local_app):
                _add_file(p_res, "localappdata", local_app)

    return items, tracked_surfaces


def create_game_snapshot(
    slug: str,
    game_dir: Path | str,
    *,
    label: str = "",
    engine: str = "",
    extra_ini_paths: list[Path | str] | None = None,
    snapshots_root: Path | None = None,
    audit: bool = True,
) -> dict[str, Any]:
    """Capture modded game surfaces into ``game_snapshots/<slug>/<timestamp>/`` with SHA-256 manifest."""
    safe_slug = _validate_segment(make_slug(slug) or slug, "game slug")
    gdir = Path(str(game_dir).strip())
    if not gdir.is_dir():
        raise ValueError(f"Game directory does not exist: {gdir}")

    root = _get_snapshots_root(snapshots_root)
    slug_dir = ensure_within(root / safe_slug, root)
    slug_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    snapshot_id = now.strftime("%Y%m%d_%H%M%S_%f")
    snap_dir = ensure_within(slug_dir / snapshot_id, slug_dir)
    files_dir = ensure_within(snap_dir / "files", snap_dir)
    files_dir.mkdir(parents=True, exist_ok=True)

    resolved_engine = _detect_snapshot_engine(gdir, engine)
    extra_paths = [Path(p) for p in (extra_ini_paths or []) if str(p or "").strip()]
    collected, tracked_surfaces = _collect_game_surface_files(gdir, resolved_engine, extra_paths)

    manifest_files: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for src_file, rel_path, scope_name, scope_base in collected:
        ensure_within(src_file, scope_base)
        snap_rel = f"{scope_name}/{rel_path}"
        dst_file = ensure_within(files_dir / snap_rel, files_dir)
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        file_sha = sha256_file(dst_file)
        size = dst_file.stat().st_size
        total_bytes += size
        manifest_files[snap_rel] = {
            "scope": scope_name,
            "rel_path": rel_path,
            "snap_rel": snap_rel,
            "sha256": file_sha,
            "size": size,
        }

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "slug": safe_slug,
        "snapshot_id": snapshot_id,
        "created_at": now.isoformat(),
        "label": str(label or "").strip() or f"Snapshot ({resolved_engine})",
        "engine": resolved_engine,
        "game_dir": str(gdir.resolve()),
        "tracked_surfaces": tracked_surfaces,
        "file_count": len(manifest_files),
        "total_bytes": total_bytes,
        "files": manifest_files,
    }
    manifest_sha = sha256_text(json.dumps(manifest, sort_keys=True))
    manifest["manifest_sha256"] = manifest_sha
    atomic_write_json(ensure_within(snap_dir / "manifest.json", snap_dir), manifest)

    if audit:
        append_audit_log(
            action="game_snapshots.create",
            status="ok",
            summary=f"Created snapshot '{snapshot_id}' for '{safe_slug}' ({len(manifest_files)} file(s))",
            sha256=manifest_sha,
            details={"slug": safe_slug, "snapshot_id": snapshot_id, "label": manifest["label"], "file_count": len(manifest_files)},
        )

    return {
        "status": "ok",
        "slug": safe_slug,
        "snapshot_id": snapshot_id,
        "snapshot_dir": str(snap_dir),
        "created_at": manifest["created_at"],
        "label": manifest["label"],
        "engine": resolved_engine,
        "file_count": len(manifest_files),
        "total_bytes": total_bytes,
        "manifest_sha256": manifest_sha,
    }


def list_game_snapshots(
    slug: str,
    *,
    snapshots_root: Path | None = None,
) -> list[dict[str, Any]]:
    """List available snapshots for *slug*, newest first."""
    safe_slug = _validate_segment(make_slug(slug) or slug, "game slug")
    root = _get_snapshots_root(snapshots_root)
    slug_dir = ensure_within(root / safe_slug, root)
    if not slug_dir.is_dir():
        return []

    snapshots: list[dict[str, Any]] = []
    for entry in sorted(slug_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        snapshots.append({
            "slug": safe_slug,
            "snapshot_id": data.get("snapshot_id", entry.name),
            "created_at": data.get("created_at", ""),
            "label": data.get("label", entry.name),
            "engine": data.get("engine", "generic"),
            "file_count": int(data.get("file_count", len(data.get("files", {})))),
            "total_bytes": int(data.get("total_bytes", 0)),
            "manifest_sha256": data.get("manifest_sha256", ""),
            "snapshot_dir": str(entry),
        })
    return snapshots


def restore_game_snapshot(
    slug: str,
    snapshot_id: str,
    *,
    target_game_dir: Path | str | None = None,
    snapshots_root: Path | None = None,
    audit: bool = True,
) -> dict[str, Any]:
    """Restore a previously saved snapshot, validating every path with ``ensure_within()``."""
    safe_slug = _validate_segment(make_slug(slug) or slug, "game slug")
    safe_id = _validate_segment(snapshot_id, "snapshot_id")

    root = _get_snapshots_root(snapshots_root)
    slug_dir = ensure_within(root / safe_slug, root)
    snap_dir = ensure_within(slug_dir / safe_id, slug_dir)
    if not snap_dir.is_dir():
        raise FileNotFoundError(f"Snapshot '{safe_slug}/{safe_id}' not found.")

    manifest_path = ensure_within(snap_dir / "manifest.json", snap_dir)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Snapshot manifest missing in '{snap_dir}'.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files_dir = ensure_within(snap_dir / "files", snap_dir)

    gdir = Path(str(target_game_dir or manifest.get("game_dir") or "").strip())
    if not gdir.is_dir():
        raise ValueError(f"Target game directory does not exist: {gdir}")
    game_base = gdir.resolve()
    localappdata_base = _get_local_appdata_root().resolve()

    manifest_files: dict[str, dict[str, Any]] = manifest.get("files", {}) or {}
    expected_game_rels: set[str] = set()
    for meta in manifest_files.values():
        scope = str(meta.get("scope", "game"))
        rel_path = str(meta.get("rel_path", "")).strip()
        if not rel_path:
            raise ValueError("Manifest entry missing rel_path")
        if scope == "game":
            ensure_within(game_base / rel_path, game_base)
            expected_game_rels.add(Path(rel_path).as_posix())
        elif scope == "localappdata":
            ensure_within(localappdata_base / rel_path, localappdata_base)
        else:
            raise ValueError(f"Unsupported snapshot scope: '{scope}'")

    # Validate every snapshot source file and checksum before writing anything
    validated_ops: list[tuple[Path, Path, str]] = []
    for key, meta in manifest_files.items():
        snap_rel = str(meta.get("snap_rel") or key)
        src_file = ensure_within(files_dir / snap_rel, files_dir)
        if not src_file.is_file():
            raise FileNotFoundError(f"Snapshot payload file missing: {snap_rel}")
        expected_sha = str(meta.get("sha256", ""))
        if expected_sha and sha256_file(src_file) != expected_sha:
            raise ValueError(f"SHA-256 mismatch for snapshot file '{snap_rel}'")

        scope = str(meta.get("scope", "game"))
        rel_path = str(meta.get("rel_path", ""))
        dst_base = game_base if scope == "game" else localappdata_base
        dst_file = ensure_within(dst_base / rel_path, dst_base)
        validated_ops.append((src_file, dst_file, f"{scope}:{rel_path}"))

    # Remove extra files created inside tracked surfaces inside game_dir since snapshot
    removed_files: list[str] = []
    for surf in manifest.get("tracked_surfaces", []):
        surf_str = str(surf or "").strip()
        if not surf_str:
            continue
        surf_target = ensure_within(game_base / surf_str, game_base)
        if surf_target.is_file():
            rel_surf = surf_target.relative_to(game_base).as_posix()
            if rel_surf not in expected_game_rels:
                surf_target.unlink()
                removed_files.append(rel_surf)
        elif surf_target.is_dir():
            for existing in sorted(surf_target.rglob("*")):
                if existing.is_file():
                    ex_r = ensure_within(existing, game_base)
                    rel_ex = ex_r.relative_to(game_base).as_posix()
                    if rel_ex not in expected_game_rels:
                        ex_r.unlink()
                        removed_files.append(rel_ex)

    restored_files: list[str] = []
    for src_file, dst_file, label_rel in validated_ops:
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        restored_files.append(label_rel)

    summary = (
        f"Restored snapshot '{safe_id}' for '{safe_slug}': "
        f"{len(restored_files)} file(s) restored, {len(removed_files)} post-snapshot file(s) removed"
    )
    if audit:
        append_audit_log(
            action="game_snapshots.restore",
            status="ok",
            summary=summary,
            sha256=str(manifest.get("manifest_sha256", "")),
            details={
                "slug": safe_slug,
                "snapshot_id": safe_id,
                "restored_count": len(restored_files),
                "removed_count": len(removed_files),
            },
        )

    return {
        "status": "ok",
        "slug": safe_slug,
        "snapshot_id": safe_id,
        "summary": summary,
        "restored_count": len(restored_files),
        "removed_count": len(removed_files),
        "restored_files": restored_files,
        "removed_files": removed_files,
    }

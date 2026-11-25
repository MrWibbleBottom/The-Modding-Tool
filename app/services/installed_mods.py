"""Installed mods scanner, enable/disable (.disabled), and snapshot-guarded removal service."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from app.core.audit import append_audit_log
from app.core.config import sha256_text
from app.services.game_snapshots import create_game_snapshot, ensure_within

_DISABLED_SUFFIX = ".disabled"


def _rel_posix(path: Path, base_dir: Path) -> str:
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return path.as_posix()


def _mod_record(
    *,
    name: str,
    file_name: str,
    rel_path: str,
    mod_type: str,
    engine_surface: str,
    enabled: bool,
    can_toggle: bool,
    can_remove: bool,
    size_kb: float = 0.0,
    source: str = "Filesystem",
) -> dict[str, Any]:
    return {
        "mod_id": sha256_text(rel_path.lower())[:12],
        "name": name,
        "file_name": file_name,
        "rel_path": rel_path,
        "mod_type": mod_type,
        "engine_surface": engine_surface,
        "enabled": enabled,
        "can_toggle": can_toggle,
        "can_remove": can_remove,
        "size_kb": size_kb,
        "source": source,
    }


def scan_game_installed_mods(
    game_dir: Path,
    *,
    primary_exe: Path | None = None,
) -> dict[str, Any]:
    """Detect Unity, Unreal, and Vortex/MO2-manifest mods inside *game_dir*."""
    mods: list[dict[str, Any]] = []
    seen_rel: set[str] = set()
    managers: list[dict[str, Any]] = []
    vortex_detected = False
    mo2_detected = False

    if not game_dir.is_dir():
        return {
            "status": "ok",
            "game_dir": str(game_dir),
            "mods": [],
            "total_count": 0,
            "enabled_count": 0,
            "vortex_detected": False,
            "mo2_detected": False,
            "managers": [],
        }

    def _add_file_mod(path: Path, *, mod_type: str, engine_surface: str, label_prefix: str = "") -> None:
        if not path.is_file():
            return
        rel = _rel_posix(path, game_dir)
        key = rel.lower()
        if key in seen_rel:
            return
        seen_rel.add(key)
        fname = path.name
        is_disabled = fname.lower().endswith(_DISABLED_SUFFIX)
        base_name = fname[:-len(_DISABLED_SUFFIX)] if is_disabled else fname
        stem = Path(base_name).stem
        if label_prefix:
            display = f"{label_prefix} / {stem}"
        else:
            display = stem
        try:
            size_kb = round(path.stat().st_size / 1024, 1)
        except OSError:
            size_kb = 0.0
        mods.append(
            _mod_record(
                name=display,
                file_name=fname,
                rel_path=rel,
                mod_type=mod_type,
                engine_surface=engine_surface,
                enabled=not is_disabled,
                can_toggle=True,
                can_remove=True,
                size_kb=size_kb,
                source="Filesystem",
            )
        )

    # 1. Unity BepInEx plugins & patchers + MelonLoader Mods
    for sub_parts, m_type in (
        (("BepInEx", "plugins"), "bepinex_plugin"),
        (("BepInEx", "patchers"), "bepinex_patcher"),
        (("MelonLoader", "Mods"), "melonloader_mod"),
    ):
        base = game_dir.joinpath(*sub_parts)
        if base.is_dir():
            for p in sorted(base.rglob("*"), key=lambda x: x.as_posix().lower()):
                if not p.is_file():
                    continue
                low = p.name.lower()
                if low.endswith(".dll") or low.endswith(".dll.disabled"):
                    _add_file_mod(p, mod_type=m_type, engine_surface="Unity")

    # 2. Unreal ~mods / LogicMods (.pak / .pak.disabled / .ucas / .utoc)
    ue_pak_dirs: list[Path] = [
        game_dir / "Content" / "Paks" / "~mods",
        game_dir / "Content" / "Paks" / "LogicMods",
    ]
    ue_pak_dirs.extend(p for p in game_dir.glob("*/Content/Paks/~mods") if p.is_dir())
    ue_pak_dirs.extend(p for p in game_dir.glob("*/Content/Paks/LogicMods") if p.is_dir())
    seen_pak_dirs: set[str] = set()
    for pak_dir in ue_pak_dirs:
        if not pak_dir.is_dir():
            continue
        norm_dir = os.path.normcase(os.path.normpath(str(pak_dir)))
        if norm_dir in seen_pak_dirs:
            continue
        seen_pak_dirs.add(norm_dir)
        for p in sorted(pak_dir.rglob("*"), key=lambda x: x.as_posix().lower()):
            if not p.is_file():
                continue
            low = p.name.lower()
            if any(low.endswith(ext) for ext in (".pak", ".pak.disabled", ".ucas", ".ucas.disabled", ".utoc", ".utoc.disabled")):
                _add_file_mod(p, mod_type="unreal_pak", engine_surface="Unreal")

    # 3. Unreal UE4SS Lua/BP Mods folders (*/Binaries/Win64/Mods/<ModName> or */ue4ss/Mods/<ModName>)
    ue4ss_roots = (
        list(game_dir.glob("*/Binaries/Win64/Mods"))
        + list(game_dir.glob("*/Binaries/Win64/ue4ss/Mods"))
        + ([game_dir / "Mods"] if (game_dir / "Mods").is_dir() else [])
    )
    for mods_root in ue4ss_roots:
        if not mods_root.is_dir():
            continue
        for entry in sorted(mods_root.iterdir(), key=lambda x: x.name.lower()):
            if not entry.is_dir() or entry.name.lower() in ("shared", "mods"):
                continue
            has_script = (entry / "Scripts" / "main.lua").is_file() or (entry / "enabled.txt").is_file() or any(entry.glob("*.dll"))
            if not has_script:
                continue
            rel = _rel_posix(entry, game_dir)
            key = rel.lower()
            if key in seen_rel:
                continue
            seen_rel.add(key)
            enabled = (entry / "enabled.txt").is_file()
            total_bytes = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            mods.append(
                _mod_record(
                    name=entry.name,
                    file_name=entry.name,
                    rel_path=rel,
                    mod_type="ue4ss_mod",
                    engine_surface="Unreal",
                    enabled=enabled,
                    can_toggle=True,
                    can_remove=True,
                    size_kb=round(total_bytes / 1024, 1),
                    source="Filesystem",
                )
            )

    # 4. Vortex & MO2 deployment manifests (root + shallow subdirs up to depth 2)
    scan_dirs = [game_dir] + [d for d in game_dir.iterdir() if d.is_dir()][:20]
    if primary_exe and primary_exe.parent.is_dir() and primary_exe.parent not in scan_dirs:
        scan_dirs.append(primary_exe.parent)

    seen_manifest_files: set[str] = set()
    for d in scan_dirs:
        vortex_marker = d / "__folder_managed_by_vortex"
        if vortex_marker.exists():
            vortex_detected = True
            managers.append({"manager": "Vortex", "path": _rel_posix(vortex_marker, game_dir)})

        vortex_manifest = d / "vortex.deployment.json"
        if vortex_manifest.is_file():
            norm_m = os.path.normcase(os.path.normpath(str(vortex_manifest)))
            if norm_m not in seen_manifest_files:
                seen_manifest_files.add(norm_m)
                vortex_detected = True
                rel_m = _rel_posix(vortex_manifest, game_dir)
                managers.append({"manager": "Vortex", "path": rel_m})
                try:
                    vdata = json.loads(vortex_manifest.read_text(encoding="utf-8", errors="replace"))
                    deployed_items = vdata.get("files", []) if isinstance(vdata, dict) else []
                    for item in deployed_items[:100]:
                        if not isinstance(item, dict):
                            continue
                        t_rel = str(item.get("relPath") or item.get("target") or "").strip().replace("\\", "/")
                        src_mod = str(item.get("source") or Path(t_rel).name or "Vortex Mod").strip()
                        if not t_rel:
                            continue
                        manifest_key = f"vortex:{t_rel.lower()}"
                        if manifest_key in seen_rel:
                            continue
                        seen_rel.add(manifest_key)
                        mods.append(
                            _mod_record(
                                name=f"{src_mod} ({Path(t_rel).name})",
                                file_name=Path(t_rel).name,
                                rel_path=t_rel,
                                mod_type="vortex_manifest",
                                engine_surface="Mod Manager",
                                enabled=True,
                                can_toggle=False,
                                can_remove=False,
                                size_kb=0.0,
                                source="Vortex",
                            )
                        )
                except Exception:
                    pass

        mo2_list = d / "modlist.txt"
        if mo2_list.is_file():
            norm_m = os.path.normcase(os.path.normpath(str(mo2_list)))
            if norm_m not in seen_manifest_files:
                seen_manifest_files.add(norm_m)
                mo2_detected = True
                managers.append({"manager": "MO2", "path": _rel_posix(mo2_list, game_dir)})
                try:
                    for raw_line in mo2_list.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith("#") or line.endswith("_separator"):
                            continue
                        if line[0] not in ("+", "-"):
                            continue
                        m_enabled = line.startswith("+")
                        m_name = line[1:].strip()
                        if not m_name:
                            continue
                        m_key = f"mo2:{m_name.lower()}"
                        if m_key in seen_rel:
                            continue
                        seen_rel.add(m_key)
                        mods.append(
                            _mod_record(
                                name=m_name,
                                file_name=m_name,
                                rel_path=f"MO2:{m_name}",
                                mod_type="mo2_modlist",
                                engine_surface="Mod Manager",
                                enabled=m_enabled,
                                can_toggle=False,
                                can_remove=False,
                                size_kb=0.0,
                                source="MO2",
                            )
                        )
                except Exception:
                    pass

    return {
        "status": "ok",
        "game_dir": str(game_dir),
        "mods": mods,
        "total_count": len(mods),
        "enabled_count": sum(1 for m in mods if m["enabled"]),
        "vortex_detected": vortex_detected,
        "mo2_detected": mo2_detected,
        "managers": managers,
    }


def toggle_installed_mod(
    game_dir: Path,
    rel_path: str,
    *,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Enable or disable a file mod via `.disabled` suffix or UE4SS `enabled.txt` marker."""
    if not game_dir.is_dir():
        raise FileNotFoundError(f"Game directory does not exist: {game_dir}")
    clean_rel = str(rel_path or "").strip()
    if not clean_rel:
        raise ValueError("Missing mod relative path")

    target = ensure_within(game_dir / clean_rel, game_dir)
    if target.is_dir() and target.parent.name.lower() == "mods":
        flag_file = ensure_within(target / "enabled.txt", target)
        cur_enabled = flag_file.is_file()
        new_enabled = (not cur_enabled) if enabled is None else bool(enabled)
        if new_enabled != cur_enabled:
            if new_enabled:
                flag_file.write_text("", encoding="utf-8")
            else:
                flag_file.unlink(missing_ok=True)
        action_word = "Enabled" if new_enabled else "Disabled"
        append_audit_log(
            action="mods.toggle",
            status="ok",
            summary=f"{action_word} UE4SS mod '{target.name}'",
            details={"rel_path": _rel_posix(target, game_dir), "enabled": new_enabled},
        )
        return {
            "status": "ok",
            "changed": new_enabled != cur_enabled,
            "enabled": new_enabled,
            "old_rel_path": clean_rel,
            "rel_path": _rel_posix(target, game_dir),
            "summary": f"{action_word} mod '{target.name}'.",
        }

    if not target.is_file():
        raise FileNotFoundError(f"Mod file not found: {clean_rel}")

    cur_disabled = target.name.lower().endswith(_DISABLED_SUFFIX)
    cur_enabled = not cur_disabled
    new_enabled = (not cur_enabled) if enabled is None else bool(enabled)
    if new_enabled == cur_enabled:
        return {
            "status": "ok",
            "changed": False,
            "enabled": new_enabled,
            "old_rel_path": clean_rel,
            "rel_path": _rel_posix(target, game_dir),
            "summary": f"Mod '{target.name}' is already {'enabled' if new_enabled else 'disabled'}.",
        }

    new_name = target.name[:-len(_DISABLED_SUFFIX)] if new_enabled else f"{target.name}{_DISABLED_SUFFIX}"
    dest = ensure_within(target.with_name(new_name), game_dir)
    if dest.exists():
        raise FileExistsError(f"Cannot toggle '{target.name}': destination '{dest.name}' already exists.")
    target.rename(dest)
    new_rel = _rel_posix(dest, game_dir)
    action_word = "Enabled" if new_enabled else "Disabled"
    append_audit_log(
        action="mods.toggle",
        status="ok",
        summary=f"{action_word} mod '{dest.name}'",
        details={"old_rel_path": clean_rel, "rel_path": new_rel, "enabled": new_enabled},
    )
    return {
        "status": "ok",
        "changed": True,
        "enabled": new_enabled,
        "old_rel_path": clean_rel,
        "rel_path": new_rel,
        "summary": f"{action_word} mod -> {dest.name}",
    }


def remove_installed_mod(
    slug: str,
    game_dir: Path,
    rel_path: str,
    *,
    snapshots_root: Path | None = None,
) -> dict[str, Any]:
    """Capture a pre-removal snapshot and safely delete a detected mod file or UE4SS mod folder."""
    if not game_dir.is_dir():
        raise FileNotFoundError(f"Game directory does not exist: {game_dir}")
    clean_rel = str(rel_path or "").strip()
    if not clean_rel:
        raise ValueError("Missing mod relative path")

    target = ensure_within(game_dir / clean_rel, game_dir)
    if target == game_dir.resolve() or target.name.lower() in ("bepinex", "plugins", "patchers", "~mods", "mods", "logicmods"):
        raise ValueError(f"Refusing to remove protected directory: '{clean_rel}'")
    if not target.exists():
        raise FileNotFoundError(f"Mod target not found: {clean_rel}")

    snap_id = ""
    try:
        snap = create_game_snapshot(
            slug,
            game_dir,
            label=f"Pre-remove mod {target.name}",
            snapshots_root=snapshots_root,
        )
        snap_id = str(snap.get("snapshot_id", ""))
    except ValueError:
        snap_id = ""

    if target.is_dir():
        if target.parent.name.lower() != "mods":
            raise ValueError(f"Refusing to recursively remove non-mod folder: '{clean_rel}'")
        shutil.rmtree(target)
    else:
        target.unlink()

    append_audit_log(
        action="mods.remove",
        status="ok",
        summary=f"Removed mod '{clean_rel}'" + (f" (snapshot: {snap_id})" if snap_id else ""),
        details={"slug": slug, "rel_path": clean_rel, "snapshot_id": snap_id},
    )
    return {
        "status": "ok",
        "removed": clean_rel,
        "snapshot_id": snap_id,
        "summary": f"Removed '{target.name}'" + (f" (pre-removal snapshot: {snap_id})" if snap_id else ""),
    }

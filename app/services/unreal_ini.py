"""Structured Unreal Engine INI tweak presets, comment-fenced blocks, diff previews, and snapshot-safe apply."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from app.core.audit import append_audit_log
from app.engines.unreal import discover_local_unreal_ini_profiles, inspect_unreal_game_settings
from app.services.discovery import make_slug
from app.services.game_snapshots import create_game_snapshot

UNREAL_INI_TWEAKS: tuple[dict[str, Any], ...] = (
    {
        "id": "ue_disable_motion_blur",
        "label": "Disable Motion Blur",
        "description": "Sets r.MotionBlurQuality=0 and disables default post-process blur.",
        "target_file": "Engine.ini",
        "section": "/Script/Engine.RendererSettings",
        "lines": [
            "r.MotionBlurQuality=0",
            "r.DefaultFeature.MotionBlur=False",
        ],
    },
    {
        "id": "ue_disable_dof",
        "label": "Disable Depth of Field",
        "description": "Clears distant blur by setting r.DepthOfFieldQuality=0.",
        "target_file": "Engine.ini",
        "section": "/Script/Engine.RendererSettings",
        "lines": [
            "r.DepthOfFieldQuality=0",
        ],
    },
    {
        "id": "ue_sharpen_taa",
        "label": "Crisper TAA Sharpening",
        "description": "Applies light tonemapper sharpening (r.Tonemapper.Sharpen=0.8).",
        "target_file": "Engine.ini",
        "section": "SystemSettings",
        "lines": [
            "r.Tonemapper.Sharpen=0.8",
        ],
    },
    {
        "id": "ue_async_pso_cache",
        "label": "Shader PSO Precache",
        "description": "Enables startup PSO shader pipeline caching to reduce traversal stutter.",
        "target_file": "Engine.ini",
        "section": "/Script/Engine.StreamingSettings",
        "lines": [
            "r.ShaderPipelineCache.Enabled=1",
            "r.ShaderPipelineCache.StartupMode=3",
        ],
    },
    {
        "id": "ue_streaming_pool_4gb",
        "label": "4 GB Texture Streaming Pool",
        "description": "Raises r.Streaming.PoolSize=4096 while clamping safely to VRAM.",
        "target_file": "Engine.ini",
        "section": "SystemSettings",
        "lines": [
            "r.Streaming.PoolSize=4096",
            "r.Streaming.LimitPoolSizeToVRAM=1",
        ],
    },
    {
        "id": "gus_disable_vsync",
        "label": "Disable VSync (GameUserSettings)",
        "description": "Sets bUseVSync=False in [/Script/Engine.GameUserSettings].",
        "target_file": "GameUserSettings.ini",
        "section": "/Script/Engine.GameUserSettings",
        "lines": [
            "bUseVSync=False",
        ],
    },
    {
        "id": "gus_sdr_safe_output",
        "label": "Force SDR Safe Output",
        "description": "Sets bUseHDRDisplayOutput=False to prevent washed-out HDR desktop state.",
        "target_file": "GameUserSettings.ini",
        "section": "/Script/Engine.GameUserSettings",
        "lines": [
            "bUseHDRDisplayOutput=False",
        ],
    },
)

TWEAK_PRESET_MAP: dict[str, dict[str, Any]] = {t["id"]: t for t in UNREAL_INI_TWEAKS}
_FENCE_START_TEMPLATE = "; [TheModdingTool:{tweak_id}:start]"
_FENCE_END_TEMPLATE = "; [TheModdingTool:{tweak_id}:end]"


def _fence_markers(tweak_id: str) -> tuple[str, str]:
    return _FENCE_START_TEMPLATE.format(tweak_id=tweak_id), _FENCE_END_TEMPLATE.format(tweak_id=tweak_id)


def _strip_fenced_block(lines: list[str], tweak_id: str) -> tuple[list[str], int | None]:
    """Remove an existing comment-fenced block and return (remaining_lines, original_insert_idx)."""
    start_marker, end_marker = _fence_markers(tweak_id)
    out: list[str] = []
    insert_at: int | None = None
    in_block = False
    for line in lines:
        stripped = line.strip()
        if not in_block and stripped == start_marker:
            in_block = True
            if insert_at is None:
                insert_at = len(out)
            continue
        if in_block:
            if stripped == end_marker:
                in_block = False
            continue
        out.append(line)

    # Collapse duplicate trailing/adjacent blank lines left after block removal
    cleaned: list[str] = []
    for item in out:
        if item == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(item)
    if insert_at is not None and len(lines) > len(cleaned) and not cleaned:
        insert_at = 0
    return cleaned, insert_at


def upsert_fenced_block(
    original_text: str,
    tweak_id: str,
    block_lines: list[str] | None,
    *,
    section_header: str = "",
) -> str:
    """Enable, update, or remove a single comment-fenced tweak block without disturbing user lines."""
    normalized = original_text.replace("\r\n", "\n")
    had_trailing_nl = normalized.endswith("\n")
    lines = normalized.splitlines() if normalized else []
    cleaned, prev_idx = _strip_fenced_block(lines, tweak_id)

    if block_lines:
        start_marker, end_marker = _fence_markers(tweak_id)
        payload_lines: list[str] = [start_marker]
        clean_section = section_header.strip().strip("[]")
        if clean_section:
            payload_lines.append(f"[{clean_section}]")
        payload_lines.extend(str(x).rstrip("\r\n") for x in block_lines)
        payload_lines.append(end_marker)

        if prev_idx is not None and 0 <= prev_idx <= len(cleaned):
            before = cleaned[:prev_idx]
            after = cleaned[prev_idx:]
            result_lines = before + payload_lines + after
        else:
            while cleaned and cleaned[-1] == "":
                cleaned.pop()
            if cleaned:
                cleaned.append("")
            result_lines = cleaned + payload_lines
    else:
        while cleaned and cleaned[-1] == "":
            cleaned.pop()
        result_lines = cleaned

    if not result_lines:
        return ""
    joined = "\n".join(result_lines)
    if had_trailing_nl or block_lines:
        joined += "\n"
    return joined


def detect_active_tweaks(text: str, target_file: str) -> dict[str, bool]:
    """Return active/inactive booleans for every known tweak targeting *target_file*."""
    lines = {line.strip() for line in text.replace("\r\n", "\n").splitlines()}
    result: dict[str, bool] = {}
    for preset in UNREAL_INI_TWEAKS:
        if preset["target_file"] != target_file:
            continue
        start_marker, end_marker = _fence_markers(preset["id"])
        result[preset["id"]] = (start_marker in lines) and (end_marker in lines)
    return result


def render_tweaks_content(
    original_text: str,
    tweak_states: dict[str, bool],
    target_file: str,
) -> str:
    """Apply multiple *tweak_states* transitions deterministically to *original_text*."""
    current = original_text
    for preset in UNREAL_INI_TWEAKS:
        if preset["target_file"] != target_file:
            continue
        tid = preset["id"]
        if tid not in tweak_states:
            continue
        enabled = bool(tweak_states[tid])
        current = upsert_fenced_block(
            current,
            tid,
            preset["lines"] if enabled else None,
            section_header=preset["section"] if enabled else "",
        )
    return current


def apply_gus_kv_updates_to_text(original_text: str, updates: dict[str, str]) -> str:
    """Apply GameUserSettings.ini key=value updates in-memory (matching engine INI updater)."""
    if not updates:
        return original_text
    lines = original_text.replace("\r\n", "\n").splitlines()
    applied: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith((";", "#", "[")) and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}")
                applied.add(k)
                continue
        new_lines.append(line)

    remaining_sg = {k: v for k, v in updates.items() if k not in applied and k.startswith("sg.")}
    remaining_other = {k: v for k, v in updates.items() if k not in applied and not k.startswith("sg.")}
    if remaining_sg:
        idx = next((i for i, l in enumerate(new_lines) if l.strip().lower() == "[scalabilitygroups]"), -1)
        if idx >= 0:
            for offset, (k, v) in enumerate(remaining_sg.items(), start=1):
                new_lines.insert(idx + offset, f"{k}={v}")
        else:
            new_lines.extend(["", "[ScalabilityGroups]"] + [f"{k}={v}" for k, v in remaining_sg.items()])
    if remaining_other:
        idx = next(
            (i for i, l in enumerate(new_lines) if l.strip().lower() == "[/script/engine.gameusersettings]"),
            -1,
        )
        if idx >= 0:
            for offset, (k, v) in enumerate(remaining_other.items(), start=1):
                new_lines.insert(idx + offset, f"{k}={v}")
        else:
            new_lines.extend(["", "[/Script/Engine.GameUserSettings]"] + [f"{k}={v}" for k, v in remaining_other.items()])

    return "\n".join(new_lines) + ("\n" if new_lines else "")


def resolve_unreal_ini_targets(
    game_dir: Path,
    *,
    engine_ini_path: Path | None = None,
    gus_ini_path: Path | None = None,
    game_name: str = "",
) -> dict[str, Path | None]:
    """Locate Engine.ini and GameUserSettings.ini for an Unreal game directory."""
    gus_path = Path(gus_ini_path) if gus_ini_path else None
    eng_path = Path(engine_ini_path) if engine_ini_path else None

    if gus_path is None or eng_path is None:
        # Check in-tree Saved/Config first for self-contained or portable Unreal layouts
        for cfg_sub in ("Saved/Config/WindowsNoEditor", "Saved/Config/Windows", "Engine/Saved/Config/WindowsNoEditor"):
            cand_gus = game_dir / cfg_sub / "GameUserSettings.ini"
            cand_eng = game_dir / cfg_sub / "Engine.ini"
            if gus_path is None and cand_gus.is_file():
                gus_path = cand_gus
            if eng_path is None and cand_eng.is_file():
                eng_path = cand_eng
            if gus_path is None and cand_eng.is_file():
                gus_path = cand_eng.parent / "GameUserSettings.ini"
            if eng_path is None and cand_gus.is_file():
                eng_path = cand_gus.parent / "Engine.ini"

    if gus_path is None:
        try:
            ue_info = inspect_unreal_game_settings(game_dir, game_name or game_dir.name)
            found_gus = str(ue_info.get("ini_path") or "").strip()
            if found_gus:
                gus_path = Path(found_gus)
            elif ue_info.get("detected_project_name"):
                profiles = discover_local_unreal_ini_profiles(game_dir, ue_info["detected_project_name"])
                if profiles:
                    gus_path = Path(profiles[0]["path"])
        except Exception:
            pass

    if eng_path is None and gus_path is not None:
        eng_path = gus_path.parent / "Engine.ini"

    return {
        "Engine.ini": eng_path,
        "GameUserSettings.ini": gus_path,
    }


def _read_text_or_empty(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="ignore")


def list_unreal_ini_tweaks(
    game_dir: Path,
    *,
    engine_ini_path: Path | None = None,
    gus_ini_path: Path | None = None,
    game_name: str = "",
) -> dict[str, Any]:
    """Return available Unreal INI presets and their current active status."""
    targets = resolve_unreal_ini_targets(
        game_dir,
        engine_ini_path=engine_ini_path,
        gus_ini_path=gus_ini_path,
        game_name=game_name,
    )
    texts = {
        fname: _read_text_or_empty(path)
        for fname, path in targets.items()
    }
    active_map: dict[str, bool] = {}
    for fname, txt in texts.items():
        active_map.update(detect_active_tweaks(txt, fname))

    presets: list[dict[str, Any]] = []
    for preset in UNREAL_INI_TWEAKS:
        target_name = preset["target_file"]
        target_path = targets.get(target_name)
        presets.append({
            **preset,
            "active": bool(active_map.get(preset["id"], False)),
            "target_path": str(target_path) if target_path else "",
            "target_exists": bool(target_path and target_path.is_file()),
        })

    return {
        "status": "ok",
        "targets": {k: (str(v) if v else "") for k, v in targets.items()},
        "presets": presets,
    }


def preview_unreal_ini_tweaks(
    game_dir: Path,
    *,
    tweak_states: dict[str, bool] | None = None,
    updates: dict[str, str] | None = None,
    engine_ini_path: Path | None = None,
    gus_ini_path: Path | None = None,
    game_name: str = "",
) -> dict[str, Any]:
    """Generate a deterministic unified diff preview across Engine.ini and GameUserSettings.ini."""
    targets = resolve_unreal_ini_targets(
        game_dir,
        engine_ini_path=engine_ini_path,
        gus_ini_path=gus_ini_path,
        game_name=game_name,
    )
    states = tweak_states or {}
    kv_updates = updates or {}

    file_diffs: list[dict[str, Any]] = []
    combined_diff_parts: list[str] = []
    has_changes = False

    for fname in ("Engine.ini", "GameUserSettings.ini"):
        path = targets.get(fname)
        if path is None:
            continue
        before = _read_text_or_empty(path).replace("\r\n", "\n")
        after = render_tweaks_content(before, states, fname)
        if fname == "GameUserSettings.ini" and kv_updates:
            after = apply_gus_kv_updates_to_text(after, kv_updates)

        changed = before != after
        diff_lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{fname}",
                tofile=f"b/{fname}",
                lineterm="\n",
            )
        )
        diff_text = "".join(diff_lines)
        additions = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        deletions = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
        if changed:
            has_changes = True
            combined_diff_parts.append(diff_text.rstrip("\n"))

        file_diffs.append({
            "file_name": fname,
            "path": str(path),
            "exists": path.is_file(),
            "changed": changed,
            "additions": additions,
            "deletions": deletions,
            "diff": diff_text,
            "new_content": after,
        })

    return {
        "status": "ok",
        "has_changes": has_changes,
        "unified_diff": "\n\n".join(part for part in combined_diff_parts if part),
        "files": file_diffs,
    }


def apply_unreal_ini_tweaks(
    slug: str,
    game_dir: Path,
    *,
    tweak_states: dict[str, bool] | None = None,
    updates: dict[str, str] | None = None,
    engine_ini_path: Path | None = None,
    gus_ini_path: Path | None = None,
    game_name: str = "",
    label: str = "Pre-INI tweak edit",
    snapshots_root: Path | None = None,
) -> dict[str, Any]:
    """Snapshot existing configs via game_snapshots and apply fenced/structured INI tweaks."""
    preview = preview_unreal_ini_tweaks(
        game_dir,
        tweak_states=tweak_states,
        updates=updates,
        engine_ini_path=engine_ini_path,
        gus_ini_path=gus_ini_path,
        game_name=game_name,
    )
    changed_entries = [f for f in preview["files"] if f["changed"]]
    if not changed_entries:
        return {
            "status": "ok",
            "summary": "No Unreal INI changes were needed.",
            "snapshot_id": "",
            "changed_files": [],
            "unified_diff": "",
        }

    existing_ini_paths = [Path(f["path"]) for f in changed_entries if Path(f["path"]).is_file()]
    snap_id = ""
    try:
        snap = create_game_snapshot(
            make_slug(slug),
            game_dir if game_dir.is_dir() else Path(changed_entries[0]["path"]).parent,
            label=label,
            engine="unreal",
            extra_ini_paths=existing_ini_paths,
            snapshots_root=snapshots_root,
        )
        snap_id = str(snap.get("snapshot_id", ""))
    except ValueError:
        snap_id = ""

    written: list[str] = []
    for entry in changed_entries:
        target_path = Path(entry["path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(entry["new_content"], encoding="utf-8")
        written.append(str(target_path))

    summary = f"Applied Unreal INI tweaks to {len(written)} file(s)" + (f" (snapshot {snap_id})" if snap_id else "")
    append_audit_log(
        action="unreal_ini.apply",
        status="ok",
        summary=summary,
        details={
            "slug": slug,
            "snapshot_id": snap_id,
            "written": written,
            "tweak_states": tweak_states or {},
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "snapshot_id": snap_id,
        "changed_files": written,
        "unified_diff": preview["unified_diff"],
    }

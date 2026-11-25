"""PC hardware vs. game graphics configuration traffic-light optimiser service."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from app.core.audit import append_audit_log
from app.engines.unreal import inspect_unreal_game_settings, match_pc_to_unreal_reference_tier
from app.services.discovery import make_slug
from app.services.game_snapshots import create_game_snapshot
from app.services.game_store import inspect_game_modding_info
from app.services.pc_specs import scrape_pc_specs
from app.services.unreal_ini import apply_unreal_ini_tweaks


def _detect_unity_boot_config(game_dir: Path) -> Path | None:
    if not game_dir.is_dir():
        return None
    for data_dir in game_dir.glob("*_Data"):
        cand = data_dir / "boot.config"
        if cand.is_file():
            return cand
    return None


def _parse_boot_config(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        k, v = clean.split("=", 1)
        parsed[k.strip()] = v.strip()
    return parsed


def _apply_unity_boot_config_updates(boot_path: Path, updates: dict[str, str | None]) -> None:
    lines = boot_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        clean = line.strip()
        if clean and not clean.startswith("#") and "=" in clean:
            k = clean.split("=", 1)[0].strip()
            if k in remaining:
                val = remaining.pop(k)
                if val is not None:
                    out.append(f"{k}={val}")
                continue
        out.append(line)
    for k, val in remaining.items():
        if val is not None:
            out.append(f"{k}={val}")
    boot_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _extract_unreal_machine_key(raw_key: str) -> str:
    clean = str(raw_key or "").strip()
    if " " in clean:
        clean = clean.split()[0].strip()
    return clean


def _unreal_rows_from_settings(ue: dict[str, Any], specs: dict[str, Any]) -> list[dict[str, Any]]:
    ini_path = str(ue.get("ini_path") or "")
    has_ini = bool(ue.get("ini_found") and ini_path)
    ref_tier = ue.get("active_reference_tier") or {}
    scal_targets = ref_tier.get("scalability_targets") or {}
    native_hz = int((specs.get("display") or {}).get("refresh_rate_hz") or 60)

    rows: list[dict[str, Any]] = []
    for item in list(ue.get("performance_settings") or []) + list(ue.get("fidelity_settings") or []):
        raw_key = str(item.get("key") or "")
        machine_key = _extract_unreal_machine_key(raw_key)
        status = str(item.get("traffic_light") or "green").lower()
        if status not in ("green", "amber", "red"):
            status = "amber"

        apply_updates: dict[str, str] = {}
        default_updates: dict[str, str] = {}
        if machine_key in scal_targets:
            rec_val = str(scal_targets[machine_key].get("rec", 2))
            apply_updates[machine_key] = rec_val
            default_updates[machine_key] = "3"
            if raw_key == "sg.ShadingQuality & sg.PostProcessQuality":
                post_rec = str((scal_targets.get("sg.PostProcessQuality") or {}).get("rec", rec_val))
                apply_updates["sg.PostProcessQuality"] = post_rec
                default_updates["sg.PostProcessQuality"] = "3"
        elif machine_key == "ResolutionSizeX":
            apply_updates["ResolutionSizeX"] = str(ref_tier.get("target_res_x", 1920))
            apply_updates["ResolutionSizeY"] = str(ref_tier.get("target_res_y", 1080))
            disp = specs.get("display") or {}
            default_updates["ResolutionSizeX"] = str(disp.get("width") or 1920)
            default_updates["ResolutionSizeY"] = str(disp.get("height") or 1080)
        elif machine_key == "sg.ResolutionQuality":
            apply_updates["sg.ResolutionQuality"] = str(ref_tier.get("recommended_res_scale", 100))
            default_updates["sg.ResolutionQuality"] = "100.000000"
        elif machine_key == "bUseVSync":
            apply_updates["bUseVSync"] = "False" if native_hz >= 100 else "True"
            default_updates["bUseVSync"] = "True"
        elif machine_key == "bUseDynamicResolution":
            apply_updates["bUseDynamicResolution"] = "True" if ref_tier.get("dynamic_resolution") else "False"
            default_updates["bUseDynamicResolution"] = "False"
        elif machine_key == "FullscreenMode":
            apply_updates["FullscreenMode"] = "1"
            default_updates["FullscreenMode"] = "1"

        rows.append({
            "key": machine_key or raw_key,
            "label": str(item.get("name") or raw_key),
            "category": str(item.get("category") or "Graphics"),
            "current_value": str(item.get("value") or "Default"),
            "recommended_value": str(item.get("recommended_value") or "—"),
            "default_value": ", ".join(f"{k}={v}" for k, v in default_updates.items()) if default_updates else "Game Default",
            "status": status,
            "reason": str(item.get("advice") or item.get("note") or ""),
            "apply_supported": bool(has_ini and apply_updates),
            "apply_updates": apply_updates,
            "default_updates": default_updates,
            "ini_path": ini_path,
        })
    return rows


def _unity_and_generic_rows(
    game: dict[str, Any],
    game_dir: Path,
    info: dict[str, Any],
    specs: dict[str, Any],
    *,
    is_unity: bool,
) -> tuple[list[dict[str, Any]], Path | None]:
    cpu = specs.get("cpu") or {}
    gpu = specs.get("gpu") or {}
    ram = specs.get("ram") or {}
    disp = specs.get("display") or {}

    vram_gb = float(gpu.get("vram_total_gb") or 0.0)
    ram_gb = float(ram.get("total_gb") or 0.0)
    threads = int(cpu.get("logical_threads") or cpu.get("physical_cores") or 4)
    disp_w = int(disp.get("width") or 1920)
    disp_h = int(disp.get("height") or 1080)
    disp_hz = int(disp.get("refresh_rate_hz") or 60)

    boot_path = _detect_unity_boot_config(game_dir) if is_unity else None
    boot_kv = _parse_boot_config(boot_path)

    rows: list[dict[str, Any]] = []

    # 1. VRAM / Texture Streaming Headroom
    if vram_gb >= 8.0:
        vram_status = "green"
        vram_rec = "High / Full Resolution Textures"
        vram_reason = f"{gpu.get('name', 'GPU')} ({vram_gb} GB VRAM) comfortably handles uncompressed High/Ultra textures."
    elif vram_gb >= 6.0:
        vram_status = "amber"
        vram_rec = "High Textures (Cap MSAA <= 2x)"
        vram_reason = f"{vram_gb} GB VRAM is solid at 1080p/1440p; keep texture pools moderate with heavy mods."
    else:
        vram_status = "red"
        vram_rec = "Half-Res / Medium Textures + Disable HD Packs"
        vram_reason = f"Detected {vram_gb} GB VRAM; High/4K texture mods risk VRAM paging stutter."

    rows.append({
        "key": "vram_texture_budget",
        "label": "GPU VRAM & Texture Budget",
        "category": "VRAM / Fidelity",
        "current_value": f"{vram_gb} GB ({gpu.get('name', 'GPU')})",
        "recommended_value": vram_rec,
        "default_value": "In-Game Quality Default",
        "status": vram_status,
        "reason": vram_reason,
        "apply_supported": False,
        "apply_updates": {},
        "default_updates": {},
        "ini_path": "",
    })

    # 2. Display Resolution & Refresh Pacing
    fps_rec = f"{disp_w}x{disp_h} @ {disp_hz} Hz ({'VSync Off + Cap ' + str(disp_hz) if disp_hz >= 100 else 'VSync On / Borderless'})"
    disp_status = "green" if disp_hz >= 60 and disp_w >= 1920 else "amber"
    rows.append({
        "key": "display_refresh_sync",
        "label": "Display Resolution & Frame Pacing",
        "category": "Display / Latency",
        "current_value": f"{disp_w}x{disp_h} @ {disp_hz} Hz",
        "recommended_value": fps_rec,
        "default_value": f"{disp_w}x{disp_h} Native",
        "status": disp_status,
        "reason": (
            "Match in-game resolution to native panel; on 100Hz+ VRR displays disable VSync and set FPS cap 2-3 Hz below max."
            if disp_hz >= 100
            else "60Hz display detected; enable Borderless Fullscreen + VSync for tear-free frame pacing."
        ),
        "apply_supported": False,
        "apply_updates": {},
        "default_updates": {},
        "ini_path": "",
    })

    # 3. System RAM / Modding Overhead
    if ram_gb >= 16.0:
        ram_status = "green"
        ram_rec = "Sufficient for Heavy Plugin/Asset Mods"
        ram_reason = f"{ram_gb} GB system RAM provides comfortable headroom for Harmony/IL2CPP/asset caches."
    elif ram_gb >= 12.0:
        ram_status = "amber"
        ram_rec = "Limit Simultaneous Overhaul Packs"
        ram_reason = f"{ram_gb} GB RAM detected; close background browsers when running heavy asset mods."
    else:
        ram_status = "red"
        ram_rec = "Upgrade RAM or Use Lightweight Mods Only"
        ram_reason = f"{ram_gb} GB RAM is tight for modded runtimes and shader compilation."

    rows.append({
        "key": "system_ram_headroom",
        "label": "System RAM & Modding Headroom",
        "category": "Memory / Stability",
        "current_value": f"{ram_gb} GB Total ({ram.get('available_gb', '?')} GB Free)",
        "recommended_value": ram_rec,
        "default_value": "Standard",
        "status": ram_status,
        "reason": ram_reason,
        "apply_supported": False,
        "apply_updates": {},
        "default_updates": {},
        "ini_path": "",
    })

    # 4. Unity-specific writable boot.config multi-threaded graphics jobs
    if is_unity:
        gfx_jobs = boot_kv.get("gfx-enable-gfx-jobs")
        native_jobs = boot_kv.get("gfx-enable-native-gfx-jobs")
        has_boot = boot_path is not None and boot_path.is_file()
        if gfx_jobs == "1" and (threads >= 4):
            boot_status = "green"
            boot_cur = f"gfx-enable-gfx-jobs={gfx_jobs}"
            boot_reason = f"Unity multi-threaded render jobs enabled across {threads} CPU threads."
        elif has_boot and threads >= 4:
            boot_status = "amber"
            boot_cur = f"gfx-enable-gfx-jobs={gfx_jobs or 'unset'}"
            boot_reason = f"CPU has {threads} threads; enabling Unity gfx-jobs in boot.config reduces render-thread bottlenecks."
        else:
            boot_status = "amber" if has_boot else "green"
            boot_cur = f"gfx-enable-gfx-jobs={gfx_jobs or 'default'}"
            boot_reason = "Unity uses default job worker scheduling." if not has_boot else f"Native jobs={native_jobs or 'unset'}."

        rows.append({
            "key": "gfx-enable-gfx-jobs",
            "label": "Unity Multi-Threaded Render Jobs (boot.config)",
            "category": "CPU / Frame Pacing",
            "current_value": boot_cur,
            "recommended_value": "gfx-enable-gfx-jobs=1" if threads >= 4 else "default",
            "default_value": "unset",
            "status": boot_status,
            "reason": boot_reason,
            "apply_supported": bool(has_boot and threads >= 4),
            "apply_updates": {"gfx-enable-gfx-jobs": "1"} if has_boot and threads >= 4 else {},
            "default_updates": {"gfx-enable-gfx-jobs": None} if has_boot else {},
            "ini_path": str(boot_path) if boot_path else "",
        })

    return rows, boot_path


def evaluate_pc_optimisation(
    game: dict[str, Any],
    *,
    force_specs_refresh: bool = False,
    pc_specs_fn: Callable[[bool], dict[str, Any]] | None = None,
    custom_ini_path: str = "",
    reference_tier_id: str = "auto",
) -> dict[str, Any]:
    """Score graphics & runtime settings against live PC hardware specs as green/amber/red traffic lights."""
    specs_loader = pc_specs_fn or scrape_pc_specs
    specs = specs_loader(force_specs_refresh)
    game_name = str(game.get("name") or game.get("id") or "Unknown Game")
    game_dir = Path(str(game.get("game_dir") or "").strip())

    cached = game.get("cached_modding_info") if isinstance(game.get("cached_modding_info"), dict) else None
    if cached and cached.get("mcp_category"):
        info = dict(cached)
    elif game_dir.is_dir():
        info = inspect_game_modding_info(game_dir, game_name, pc_specs=specs, pc_specs_fn=specs_loader)
    else:
        info = {"mcp_category": game.get("runtime") or "Generic"}

    cat_low = str(info.get("mcp_category") or game.get("runtime") or "").lower()
    tech_low = str(info.get("sheet_engine_tech") or "").lower()
    rt_low = str(game.get("runtime") or "").lower()
    is_unreal = bool(
        info.get("is_unreal_engine")
        or "unreal" in cat_low
        or "unreal" in tech_low
        or "unreal" in rt_low
        or (custom_ini_path and Path(custom_ini_path).is_file())
    )
    is_unity = (not is_unreal) and (
        "unity" in cat_low
        or "il2cpp" in str(info.get("scripting_backend") or "").lower()
        or "mono" in str(info.get("scripting_backend") or "").lower()
        or (game_dir.is_dir() and ((game_dir / "GameAssembly.dll").is_file() or bool(list(game_dir.glob("*_Data")))))
    )

    matched_tier = match_pc_to_unreal_reference_tier(specs, reference_tier_id)
    config_path = ""
    if is_unreal:
        ue = inspect_unreal_game_settings(
            game_dir,
            info,
            custom_ini_path=custom_ini_path,
            pc_specs=specs,
            pc_specs_fn=specs_loader,
            reference_tier_id=reference_tier_id,
        )
        rows = _unreal_rows_from_settings(ue, specs)
        config_path = str(ue.get("ini_path") or "")
        engine_label = "Unreal Engine 4/5"
    else:
        rows, boot_path = _unity_and_generic_rows(game, game_dir, info, specs, is_unity=is_unity)
        config_path = str(boot_path) if boot_path else ""
        engine_label = "Unity" if is_unity else (info.get("mcp_category") or "PC Game")

    green_count = sum(1 for r in rows if r["status"] == "green")
    amber_count = sum(1 for r in rows if r["status"] == "amber")
    red_count = sum(1 for r in rows if r["status"] == "red")
    overall_status = "red" if red_count > 0 else ("amber" if amber_count > 0 else "green")
    can_apply_optimal = any(r.get("apply_supported") for r in rows)

    return {
        "status": "ok",
        "slug": make_slug(str(game.get("id") or game_name)),
        "game_name": game_name,
        "engine_label": engine_label,
        "is_unreal": is_unreal,
        "is_unity": is_unity,
        "config_path": config_path,
        "can_apply_optimal": can_apply_optimal,
        "matched_tier": {
            "tier_id": matched_tier.get("tier_id"),
            "tier_name": matched_tier.get("tier_name"),
            "target_resolution": matched_tier.get("target_resolution"),
        },
        "specs_summary": {
            "cpu": (specs.get("cpu") or {}).get("name", "Unknown CPU"),
            "cores": (specs.get("cpu") or {}).get("physical_cores"),
            "threads": (specs.get("cpu") or {}).get("logical_threads"),
            "gpu": (specs.get("gpu") or {}).get("name", "Unknown GPU"),
            "vram_gb": (specs.get("gpu") or {}).get("vram_total_gb"),
            "ram_gb": (specs.get("ram") or {}).get("total_gb"),
            "display": (specs.get("display") or {}).get("resolution_label"),
            "refresh_hz": (specs.get("display") or {}).get("refresh_rate_hz"),
        },
        "summary": {
            "overall_status": overall_status,
            "green_count": green_count,
            "amber_count": amber_count,
            "red_count": red_count,
            "total_settings": len(rows),
        },
        "rows": rows,
    }


def apply_pc_optimisation(
    slug: str,
    game: dict[str, Any],
    *,
    mode: str = "optimal",
    setting_key: str = "",
    custom_ini_path: str = "",
    reference_tier_id: str = "auto",
    snapshots_root: Path | None = None,
) -> dict[str, Any]:
    """Apply recommended (optimal) or default settings safely after creating a game snapshot."""
    clean_mode = "default" if str(mode or "").strip().lower() == "default" else "optimal"
    report = evaluate_pc_optimisation(
        game,
        custom_ini_path=custom_ini_path,
        reference_tier_id=reference_tier_id,
    )
    game_dir = Path(str(game.get("game_dir") or "").strip())
    rows = report.get("rows") or []
    target_rows = [
        r for r in rows
        if r.get("apply_supported") and (not setting_key or r.get("key") == setting_key)
    ]

    if report.get("is_unreal"):
        updates: dict[str, str] = {}
        ini_path = report.get("config_path") or ""
        for r in target_rows:
            src_map = r.get("default_updates") if clean_mode == "default" else r.get("apply_updates")
            for k, v in (src_map or {}).items():
                if v is not None:
                    updates[k] = str(v)
        if not ini_path or not Path(ini_path).is_file() or not updates:
            return {
                "status": "ok",
                "mode": clean_mode,
                "applied_count": 0,
                "snapshot_id": "",
                "summary": "No writable Unreal INI settings available to update.",
            }
        res = apply_unreal_ini_tweaks(
            slug,
            game_dir,
            updates=updates,
            gus_ini_path=Path(ini_path),
            label=f"Pre-PC-Optimisation ({clean_mode})",
            snapshots_root=snapshots_root,
        )
        return {
            "status": "ok",
            "mode": clean_mode,
            "applied_count": len(updates),
            "snapshot_id": res.get("snapshot_id", ""),
            "summary": f"Applied {clean_mode} PC optimisation ({len(updates)} Unreal INI key(s))",
        }

    if report.get("is_unity"):
        boot_path = Path(report.get("config_path") or "") if report.get("config_path") else _detect_unity_boot_config(game_dir)
        updates_unity: dict[str, str | None] = {}
        for r in target_rows:
            src_map = r.get("default_updates") if clean_mode == "default" else r.get("apply_updates")
            for k, v in (src_map or {}).items():
                updates_unity[k] = v
        if boot_path and boot_path.is_file() and updates_unity:
            snap_id = ""
            try:
                snap = create_game_snapshot(
                    make_slug(slug),
                    game_dir,
                    label=f"Pre-Unity boot.config ({clean_mode})",
                    engine="unity",
                    extra_ini_paths=[boot_path],
                    snapshots_root=snapshots_root,
                )
                snap_id = str(snap.get("snapshot_id", ""))
            except ValueError:
                snap_id = ""
            _apply_unity_boot_config_updates(boot_path, updates_unity)
            append_audit_log(
                action="pc_optimisation.apply",
                status="ok",
                summary=f"Applied {clean_mode} Unity boot.config tuning for {slug}",
                details={"slug": slug, "mode": clean_mode, "updates": updates_unity, "snapshot_id": snap_id},
            )
            return {
                "status": "ok",
                "mode": clean_mode,
                "applied_count": len(updates_unity),
                "snapshot_id": snap_id,
                "summary": f"Applied {clean_mode} Unity boot.config setting(s) (snapshot: {snap_id or 'none'})",
            }

    return {
        "status": "ok",
        "mode": clean_mode,
        "applied_count": 0,
        "snapshot_id": "",
        "summary": "Hardware advisory refreshed (no writable config file found for automatic edits).",
    }

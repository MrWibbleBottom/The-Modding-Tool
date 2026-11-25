"""Unreal Engine 4/5 adapter, `.pak`/`.ini` discovery, editor exclusions, and traffic-light evaluator."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from app.core.config import UNREAL_INI_BACKUP_DIR
from app.core.constants import (
    NON_GAME_LOCALAPPDATA_FOLDERS,
    UNREAL_HARDWARE_REFERENCE_ARRAY,
    _UE_QUALITY_LABELS,
)
from app.engines.base import EngineAdapter


def _format_ue_quality(val_str: str | None) -> tuple[str, int | None]:
    if val_str is None or str(val_str).strip() == "":
        return ("Not Set (Engine Default)", None)
    try:
        iv = int(float(str(val_str).strip()))
        return (_UE_QUALITY_LABELS.get(iv, f"Level {iv}"), iv)
    except ValueError:
        return (str(val_str).strip(), None)


def match_pc_to_unreal_reference_tier(
    pc_specs: dict[str, Any],
    override_tier_id: str = "auto",
) -> dict[str, Any]:
    """Select the matching hardware profile from UNREAL_HARDWARE_REFERENCE_ARRAY."""
    if override_tier_id and override_tier_id.strip().lower() != "auto":
        req_id = override_tier_id.strip().lower()
        for tier in UNREAL_HARDWARE_REFERENCE_ARRAY:
            if tier["tier_id"].lower() == req_id:
                return dict(tier, matched_mode="Simulated Reference Tier Override")

    vram_gb = float(pc_specs.get("gpu", {}).get("vram_total_gb", 8.0))
    for tier in UNREAL_HARDWARE_REFERENCE_ARRAY:
        if vram_gb >= float(tier["min_vram_gb"]):
            return dict(tier, matched_mode="Auto-Matched from Scraped PC Specs")
    return dict(UNREAL_HARDWARE_REFERENCE_ARRAY[-1], matched_mode="Auto-Matched from Scraped PC Specs")


def evaluate_scalability_traffic_light(
    setting_key: str,
    current_lvl: int | None,
    ref_tier: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate a single Unreal scalability setting (0..4) against the reference tier."""
    target_info = ref_tier.get("scalability_targets", {}).get(
        setting_key,
        {"rec": 2, "max_safe": 3, "label": "High (2)"},
    )
    rec_lvl = int(target_info["rec"])
    max_safe = int(target_info["max_safe"])
    rec_label = str(target_info["label"])
    tier_name = str(ref_tier.get("tier_name", "Reference Tier"))

    if current_lvl is None:
        return {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "UNSET / DEFAULT",
            "recommended_value": rec_label,
            "advice": f"Setting not saved in INI yet. Recommended for {tier_name}: {rec_label}.",
        }

    if current_lvl > max_safe:
        return {
            "traffic_light": "red",
            "light_icon": "",
            "light_badge": "OVER-BUDGET",
            "recommended_value": rec_label,
            "advice": (
                f"Current {_UE_QUALITY_LABELS.get(current_lvl, str(current_lvl))} exceeds safe ceiling "
                f"({_UE_QUALITY_LABELS.get(max_safe, str(max_safe))}) for {tier_name} — causes VRAM/frame-time bottleneck. Lower to {rec_label}."
            ),
        }

    if current_lvl == rec_lvl or (current_lvl > rec_lvl and current_lvl <= max_safe):
        return {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": rec_label,
            "advice": f"Matches optimal reference target ({rec_label}) for {tier_name}.",
        }

    diff_below = rec_lvl - current_lvl
    if diff_below == 1:
        return {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "SUB-OPTIMAL",
            "recommended_value": rec_label,
            "advice": f"Set 1 tier below hardware capability ({_UE_QUALITY_LABELS.get(current_lvl)} vs recommended {rec_label} for {tier_name}). Safe to raise.",
        }

    return {
        "traffic_light": "red" if rec_lvl >= 3 and current_lvl == 0 else "amber",
        "light_icon": "",
        "light_badge": "UNDER-UTILIZED",
        "recommended_value": rec_label,
        "advice": f"Set {diff_below} tiers below hardware capability ({_UE_QUALITY_LABELS.get(current_lvl)} vs recommended {rec_label} for {tier_name}).",
    }


def has_unreal_editor_without_shipping(dir_path: Path) -> bool:
    """Return True if *dir_path* contains UnrealEditor.exe / UE4Editor.exe without a Shipping exe."""
    try:
        if not dir_path.is_dir():
            return False
        editor_patterns = (
            "UnrealEditor.exe",
            "UE4Editor.exe",
            "*/UnrealEditor.exe",
            "*/UE4Editor.exe",
            "*/*/UnrealEditor.exe",
            "*/*/UE4Editor.exe",
            "*/*/*/UnrealEditor.exe",
            "*/*/*/UE4Editor.exe",
        )
        has_editor_exe = any(p.is_file() for pat in editor_patterns for p in dir_path.glob(pat))
        if not has_editor_exe:
            return False
        shipping_patterns = (
            "*-Win64-Shipping.exe",
            "*/*-Win64-Shipping.exe",
            "*/*/*-Win64-Shipping.exe",
            "*/*/*/*-Win64-Shipping.exe",
        )
        has_shipping_exe = any(p.is_file() for pat in shipping_patterns for p in dir_path.glob(pat))
        return not has_shipping_exe
    except OSError:
        return False


def check_unreal_loaders(root_path: Path) -> bool:
    """Return True if RE-UE4SS (proxy DLL + UE4SS.dll) is installed inside *root_path*."""
    if not root_path.is_dir():
        return False
    shipping_dirs = {p.parent for p in root_path.rglob("*-Win64-Shipping.exe")}
    win64_dirs = shipping_dirs or {
        p for p in root_path.rglob("Win64") if p.is_dir() and p.parent.name.lower() == "binaries"
    }
    for wdir in win64_dirs:
        has_proxy = any((wdir / px).is_file() for px in ("dwmapi.dll", "dxgi.dll"))
        if not has_proxy:
            has_proxy = any(
                f.is_file() and f.name.lower() in ("dwmapi.dll", "dxgi.dll")
                for f in wdir.iterdir()
            )
        has_ue4ss = any(
            c.is_file() and c.name.lower() == "ue4ss.dll"
            for c in (wdir / "ue4ss" / "UE4SS.dll", wdir / "UE4SS.dll")
        ) or any(f.is_file() and f.name.lower() == "ue4ss.dll" for f in wdir.rglob("*.dll"))
        if has_proxy and has_ue4ss:
            return True
    return False


def discover_local_unreal_ini_profiles() -> list[dict[str, str]]:
    """Find all Unreal Engine GameUserSettings.ini files under %LOCALAPPDATA%\\*\\Saved\\Config."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    profiles: list[dict[str, str]] = []
    try:
        if not local_app_data.is_dir():
            return profiles
        children = sorted(local_app_data.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return profiles

    for child in children:
        try:
            if child.name.strip().lower() in NON_GAME_LOCALAPPDATA_FOLDERS:
                continue
            if not child.is_dir():
                continue
            cfg_root = child / "Saved" / "Config"
            if not cfg_root.is_dir():
                continue
            for sub_name in ("WindowsNoEditor", "Windows", "WindowsClient"):
                gus = cfg_root / sub_name / "GameUserSettings.ini"
                if gus.is_file() and gus.stat().st_size > 10:
                    profiles.append(
                        {
                            "project_name": child.name,
                            "platform_folder": sub_name,
                            "ini_path": str(gus.resolve()),
                        }
                    )
                    break
        except OSError:
            continue
    return profiles


def parse_unreal_ini_file(ini_path: Path) -> dict[str, dict[str, str]]:
    """Parse an Unreal Engine .ini file into {section: {key: value}}."""
    sections: dict[str, dict[str, str]] = {}
    if not ini_path.is_file():
        return sections
    current_sec = "Global"
    sections[current_sec] = {}
    try:
        for raw_line in ini_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_sec = line[1:-1].strip()
                sections.setdefault(current_sec, {})
            elif "=" in line:
                k, v = line.split("=", 1)
                sections.setdefault(current_sec, {})[k.strip()] = v.strip()
    except Exception:
        pass
    return sections


def inspect_unreal_game_settings(
    game_dir: Path,
    info: dict[str, Any],
    custom_ini_path: str = "",
    reference_tier_id: str = "auto",
    *,
    pc_specs: dict[str, Any] | None = None,
    pc_specs_fn: Callable[[bool], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Locate and evaluate Unreal GameUserSettings.ini / Engine.ini against PC Specs or a reference tier."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    dir_exists = game_dir.is_dir()

    project_candidates: list[str] = []
    if dir_exists:
        staged_inis = (
            list((game_dir / "Engine" / "Config").glob("StagedBuild_*.ini"))
            if (game_dir / "Engine" / "Config").is_dir()
            else []
        )
        for st in staged_inis:
            proj = st.stem.replace("StagedBuild_", "").strip()
            if proj and proj not in project_candidates:
                project_candidates.append(proj)
        for sub in game_dir.iterdir():
            if sub.is_dir() and sub.name.lower() not in ("engine", "resources", "bepinex"):
                if (sub / "Content" / "Paks").is_dir() or (sub / "Binaries").is_dir():
                    if sub.name not in project_candidates:
                        project_candidates.append(sub.name)

    exe_name = str(info.get("executable", "")).strip()
    if exe_name and exe_name.lower().endswith(".exe"):
        stem = Path(exe_name).stem.replace("-Win64-Shipping", "").replace("-WinGDK-Shipping", "")
        if stem.lower() not in ("gamelaunchhelper", "crashreportclient", "bssndrpt64", "unknown"):
            if stem not in project_candidates:
                project_candidates.append(stem)

    prod_name = str(info.get("product_name", "")).strip()
    if prod_name:
        if prod_name not in project_candidates:
            project_candidates.append(prod_name)
        compact = prod_name.replace(" ", "")
        if compact not in project_candidates:
            project_candidates.append(compact)

    detected_project = project_candidates[0] if project_candidates else (game_dir.name or "UnrealGame")

    found_gus_path: Path | None = None
    found_engine_ini_path: Path | None = None
    searched_locations: list[str] = []

    if custom_ini_path.strip():
        cpath = Path(custom_ini_path.strip())
        searched_locations.append(str(cpath))
        if cpath.is_file():
            found_gus_path = cpath
        elif cpath.is_dir():
            for cand_file in cpath.rglob("GameUserSettings.ini"):
                found_gus_path = cand_file
                break

    if found_gus_path is None:
        for proj in project_candidates:
            cfg_base = local_app_data / proj / "Saved" / "Config"
            for plat_sub in ("Windows", "WindowsNoEditor", "WindowsClient", "WinGDK"):
                candidate_ini = cfg_base / plat_sub / "GameUserSettings.ini"
                searched_locations.append(str(candidate_ini))
                if candidate_ini.is_file() and candidate_ini.stat().st_size > 10:
                    found_gus_path = candidate_ini
                    eng_cand = cfg_base / plat_sub / "Engine.ini"
                    if eng_cand.is_file():
                        found_engine_ini_path = eng_cand
                    break
            if found_gus_path is not None:
                break

    if found_gus_path is None and dir_exists:
        for cand_file in game_dir.rglob("GameUserSettings.ini"):
            if cand_file.is_file() and cand_file.stat().st_size > 10:
                found_gus_path = cand_file
                break

    sections = parse_unreal_ini_file(found_gus_path) if found_gus_path else {}
    flat: dict[str, str] = {}
    for sec_dict in sections.values():
        for k, v in sec_dict.items():
            flat[k] = v

    if found_engine_ini_path and found_engine_ini_path.is_file():
        eng_sections = parse_unreal_ini_file(found_engine_ini_path)
        for sec_dict in eng_sections.values():
            for k, v in sec_dict.items():
                flat.setdefault(k, v)

    if pc_specs is None:
        if pc_specs_fn is not None:
            pc_specs = pc_specs_fn(False)
        else:
            from app.services.pc_specs import scrape_pc_specs as _default_scrape_pc_specs
            pc_specs = _default_scrape_pc_specs(force_refresh=False)

    native_w = int(pc_specs.get("display", {}).get("width", 1920))
    native_h = int(pc_specs.get("display", {}).get("height", 1080))
    native_hz = int(pc_specs.get("display", {}).get("refresh_rate_hz", 60))
    vram_gb = float(pc_specs.get("gpu", {}).get("vram_total_gb", 8.0))
    ram_gb = float(pc_specs.get("ram", {}).get("total_gb", 16.0))
    cpu_cores = int(pc_specs.get("cpu", {}).get("physical_cores", 6))
    cpu_threads = int(pc_specs.get("cpu", {}).get("logical_threads", 12))
    gpu_name = str(pc_specs.get("gpu", {}).get("name", "GPU"))
    cpu_name = str(pc_specs.get("cpu", {}).get("name", "CPU"))

    has_ini = found_gus_path is not None and len(flat) > 0
    res_x = int(float(flat.get("ResolutionSizeX", flat.get("DesiredScreenWidth", native_w)))) if has_ini else native_w
    res_y = int(float(flat.get("ResolutionSizeY", flat.get("DesiredScreenHeight", native_h)))) if has_ini else native_h
    fs_mode_raw = str(flat.get("FullscreenMode", "1"))
    fs_mode_map = {
        "0": "Exclusive Fullscreen (0)",
        "1": "Windowed Fullscreen / Borderless (1)",
        "2": "Windowed (2)",
    }
    fs_mode_label = fs_mode_map.get(fs_mode_raw, f"Mode {fs_mode_raw}")

    vsync_bool = str(flat.get("bUseVSync", "False")).lower() == "true"
    dyn_res_bool = str(flat.get("bUseDynamicResolution", "False")).lower() == "true"
    hdr_bool = str(flat.get("bUseHDRDisplayOutput", "False")).lower() == "true"
    hdr_nits = str(flat.get("HDRDisplayOutputNits", "1000"))

    fps_limit_raw = flat.get("FrameRateLimit", "0.000000")
    try:
        fps_limit_val = float(fps_limit_raw)
    except ValueError:
        fps_limit_val = 0.0
    fps_limit_label = "Unlimited (0 FPS Cap)" if fps_limit_val <= 0.1 else f"{int(fps_limit_val)} FPS Cap"

    res_scale_raw = flat.get("sg.ResolutionQuality", "100")
    try:
        res_scale_num = float(res_scale_raw)
    except ValueError:
        res_scale_num = 100.0
    res_scale_label = (
        "100% (Native / Auto 0)"
        if res_scale_num == 0 or res_scale_num == 100
        else f"{res_scale_num:.0f}% Internal 3D Scale"
    )

    view_dist_label, view_dist_lvl = _format_ue_quality(flat.get("sg.ViewDistanceQuality", "3" if has_ini else None))
    aa_label, aa_lvl = _format_ue_quality(flat.get("sg.AntiAliasingQuality", "3" if has_ini else None))
    shadow_label, shadow_lvl = _format_ue_quality(flat.get("sg.ShadowQuality", "3" if has_ini else None))
    gi_label, gi_lvl = _format_ue_quality(flat.get("sg.GlobalIlluminationQuality", "3" if has_ini else None))
    refl_label, refl_lvl = _format_ue_quality(flat.get("sg.ReflectionQuality", "3" if has_ini else None))
    post_label, post_lvl = _format_ue_quality(flat.get("sg.PostProcessQuality", "3" if has_ini else None))
    tex_label, tex_lvl = _format_ue_quality(flat.get("sg.TextureQuality", "3" if has_ini else None))
    fx_label, fx_lvl = _format_ue_quality(flat.get("sg.EffectsQuality", "3" if has_ini else None))
    fol_label, fol_lvl = _format_ue_quality(flat.get("sg.FoliageQuality", "3" if has_ini else None))
    shade_label, shade_lvl = _format_ue_quality(flat.get("sg.ShadingQuality", "3" if has_ini else None))
    land_label, land_lvl = _format_ue_quality(flat.get("sg.LandscapeQuality", "3" if has_ini else None))

    ref_tier = match_pc_to_unreal_reference_tier(pc_specs, reference_tier_id)
    is_simulated_tier = (reference_tier_id or "auto").strip().lower() != "auto"
    eval_vram_gb = float(ref_tier.get("vram_budget_ceiling_gb", vram_gb)) if is_simulated_tier else vram_gb
    eval_cpu_cores = int(ref_tier.get("min_cpu_cores", cpu_cores)) if is_simulated_tier else cpu_cores
    eval_ram_gb = float(ref_tier.get("min_ram_gb", ram_gb)) if is_simulated_tier else ram_gb
    eval_hw_label = (
        f"{ref_tier['tier_name']} [Simulated Reference Profile]"
        if is_simulated_tier
        else f"{gpu_name} ({vram_gb} GB) + {cpu_name} [{ref_tier['tier_name']}]"
    )

    target_res_y = int(ref_tier.get("target_res_y", 1080))
    max_safe_res_y = int(ref_tier.get("max_safe_res_y", 1440))
    if res_y > max_safe_res_y:
        res_tl = {
            "traffic_light": "red",
            "light_icon": "",
            "light_badge": "OVER-BUDGET RESOLUTION",
            "recommended_value": ref_tier["target_resolution"],
            "advice": f"{res_x}x{res_y} exceeds safe resolution ceiling ({max_safe_res_y}p) for {ref_tier['tier_name']}. Lower to {ref_tier['target_resolution']}.",
        }
    elif res_y < target_res_y:
        res_tl = {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "BELOW TARGET",
            "recommended_value": ref_tier["target_resolution"],
            "advice": f"{res_x}x{res_y} is below recommended {ref_tier['target_resolution']} for {ref_tier['tier_name']}. Safe to raise resolution.",
        }
    else:
        res_tl = {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": ref_tier["target_resolution"],
            "advice": f"Matches optimal resolution target ({ref_tier['target_resolution']}) for {ref_tier['tier_name']}.",
        }

    eff_scale = 100.0 if (res_scale_num == 0 or res_scale_num >= 99) else res_scale_num
    rec_scale = int(ref_tier.get("recommended_res_scale", 100))
    min_safe_scale = int(ref_tier.get("min_safe_res_scale", 67))
    if res_y >= 2160 and eval_vram_gb <= 10.0 and eff_scale >= 90:
        scale_tl = {
            "traffic_light": "red",
            "light_icon": "",
            "light_badge": "TOO HIGH FOR 8GB AT 4K",
            "recommended_value": f"{rec_scale}% (TSR / DLSS Quality)",
            "advice": f"100% 3D Resolution Scale at 4K overflows {ref_tier['tier_name']} VRAM/fillrate. Lower sg.ResolutionQuality to {rec_scale}%.",
        }
    elif eff_scale < min_safe_scale:
        scale_tl = {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "LOW SCALE",
            "recommended_value": f"{rec_scale}%",
            "advice": f"Internal 3D scale ({eff_scale:.0f}%) is below recommended {rec_scale}% for {ref_tier['tier_name']}.",
        }
    else:
        scale_tl = {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": f"{rec_scale}%",
            "advice": f"3D Resolution Scale ({res_scale_label}) is well balanced for {ref_tier['tier_name']}.",
        }

    fps_tl = (
        {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "CAPPED BELOW REFRESH",
            "recommended_value": f"0 (Unlimited) or {native_hz} FPS",
            "advice": f"FPS cap ({int(fps_limit_val)}) is below your {native_hz}Hz monitor refresh rate.",
        }
        if (fps_limit_val > 0.1 and fps_limit_val < native_hz)
        else {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": f"0 (Unlimited) or {native_hz} FPS",
            "advice": f"Frame rate limit ({fps_limit_label}) allows full {native_hz}Hz display utilization.",
        }
    )

    vsync_tl = (
        {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "ADDS INPUT LATENCY",
            "recommended_value": "False (with G-SYNC / VRR) or True (60Hz)",
            "advice": "VSync is ON; if using a G-SYNC/FreeSync high-refresh display, disabling VSync reduces input lag.",
        }
        if (vsync_bool and native_hz >= 100)
        else {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": "Match Display VRR",
            "advice": "VSync setting is appropriate for current display refresh configuration.",
        }
    )

    rec_drs = bool(ref_tier.get("dynamic_resolution", False))
    drs_tl = (
        {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "ADVISED FOR 6-8GB GPUs",
            "recommended_value": "True" if rec_drs else "False",
            "advice": f"Enabling Dynamic Resolution is recommended on {ref_tier['tier_name']} to prevent heavy Lumen frame drops.",
        }
        if (rec_drs and not dyn_res_bool and res_y >= 1440)
        else {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": "True" if rec_drs else "False",
            "advice": f"Dynamic Resolution ({dyn_res_bool}) matches recommendation for {ref_tier['tier_name']}.",
        }
    )

    fs_tl = (
        {
            "traffic_light": "green",
            "light_icon": "",
            "light_badge": "OPTIMAL",
            "recommended_value": "0 (Exclusive) or 1 (Borderless DX12)",
            "advice": "DX12 Fullscreen / Borderless Independent Flip active for lowest presentation latency.",
        }
        if fs_mode_raw in ("0", "1")
        else {
            "traffic_light": "amber",
            "light_icon": "",
            "light_badge": "WINDOWED OVERHEAD",
            "recommended_value": "1 (Windowed Fullscreen)",
            "advice": "Windowed mode (2) disables DX12 Independent Flip; switch to FullscreenMode=1.",
        }
    )

    vd_tl = evaluate_scalability_traffic_light("sg.ViewDistanceQuality", view_dist_lvl, ref_tier)
    fol_tl = evaluate_scalability_traffic_light("sg.FoliageQuality", fol_lvl, ref_tier)
    gi_tl = evaluate_scalability_traffic_light("sg.GlobalIlluminationQuality", gi_lvl, ref_tier)
    shd_tl = evaluate_scalability_traffic_light("sg.ShadowQuality", shadow_lvl, ref_tier)
    refl_tl = evaluate_scalability_traffic_light("sg.ReflectionQuality", refl_lvl, ref_tier)
    tex_tl = evaluate_scalability_traffic_light("sg.TextureQuality", tex_lvl, ref_tier)
    aa_tl = evaluate_scalability_traffic_light("sg.AntiAliasingQuality", aa_lvl, ref_tier)
    shd_post_tl = evaluate_scalability_traffic_light("sg.ShadingQuality", shade_lvl, ref_tier)
    land_tl = evaluate_scalability_traffic_light("sg.LandscapeQuality", land_lvl, ref_tier)
    hdr_tl = {
        "traffic_light": "green",
        "light_icon": "",
        "light_badge": "OPTIMAL",
        "recommended_value": "Match Display HDR Capability",
        "advice": f"HDR output is {'Enabled' if hdr_bool else 'Disabled (SDR)'} ({hdr_nits} nits).",
    }

    performance_settings = [
        {
            "key": "ResolutionSizeX / ResolutionSizeY",
            "name": "Output Render Resolution",
            "value": f"{res_x}x{res_y}" if has_ini else f"Not Saved Yet (Monitor Native: {native_w}x{native_h})",
            "impact": "High GPU Fillrate & VRAM Impact",
            "category": "Performance",
            "note": "Primary pixel count rendered or targeted by TSR/DLSS upscaler.",
            **res_tl,
        },
        {
            "key": "sg.ResolutionQuality",
            "name": "3D Internal Resolution Scale",
            "value": res_scale_label if has_ini else "Default (100%)",
            "impact": "High GPU Frame-Time Impact",
            "category": "Performance",
            "note": "Controls internal 3D rendering percentage before Temporal Super Resolution (TSR).",
            **scale_tl,
        },
        {
            "key": "FrameRateLimit",
            "name": "Frame Rate Limit (FPS Cap)",
            "value": fps_limit_label if has_ini else "Default (Unlimited)",
            "impact": "Frame Pacing & GPU Power",
            "category": "Performance",
            "note": "0.000000 = uncapped frame rate; set to monitor refresh rate for consistent frame pacing.",
            **fps_tl,
        },
        {
            "key": "bUseVSync",
            "name": "Vertical Sync (VSync)",
            "value": ("Enabled (True)" if vsync_bool else "Disabled (False)") if has_ini else "Default",
            "impact": "Input Latency & Tear Prevention",
            "category": "Performance",
            "note": "Locks presentation to display refresh rate; disable if using G-SYNC/FreeSync + reflex.",
            **vsync_tl,
        },
        {
            "key": "bUseDynamicResolution",
            "name": "Dynamic Resolution Scaling (DRS)",
            "value": ("Enabled (True)" if dyn_res_bool else "Disabled (False)") if has_ini else "Disabled (False)",
            "impact": "Frame Drop Mitigation",
            "category": "Performance",
            "note": "Automatically lowers internal 3D resolution during heavy GPU spikes.",
            **drs_tl,
        },
        {
            "key": "FullscreenMode",
            "name": "Window / Fullscreen Presentation",
            "value": fs_mode_label if has_ini else "Borderless Fullscreen (1)",
            "impact": "Swapchain Latency",
            "category": "Performance",
            "note": "0 = Exclusive Fullscreen, 1 = Windowed Fullscreen (DX12 Independent Flip), 2 = Windowed.",
            **fs_tl,
        },
        {
            "key": "sg.ViewDistanceQuality",
            "name": "View Distance & LOD Scale",
            "value": view_dist_label,
            "impact": "High CPU Draw-Call & Streaming Impact",
            "category": "Performance",
            "note": "Controls `r.ViewDistanceScale` and static mesh/Nanite culling distance.",
            **vd_tl,
        },
        {
            "key": "sg.FoliageQuality & sg.EffectsQuality",
            "name": "Foliage Density & Niagara Particle Effects",
            "value": f"Foliage: {fol_label} | Effects: {fx_label}",
            "impact": "Medium-High GPU Compute & Overdraw",
            "category": "Performance",
            "note": "Controls `foliage.DensityScale` and translucent Niagara GPU particle counts.",
            **fol_tl,
        },
    ]

    fidelity_settings = [
        {
            "key": "sg.GlobalIlluminationQuality",
            "name": "Global Illumination (Lumen GI / SSGI)",
            "value": gi_label,
            "impact": "Primary Lighting Fidelity (Heavy GPU)",
            "category": "Fidelity",
            "note": "Epic (3) / Cinematic (4) enables full-rate Lumen Dynamic GI & final gather quality.",
            **gi_tl,
        },
        {
            "key": "sg.ShadowQuality",
            "name": "Shadow Quality (Virtual Shadow Maps / CSM)",
            "value": shadow_label,
            "impact": "High Visual Depth & VRAM Impact",
            "category": "Fidelity",
            "note": "Controls Virtual Shadow Map (VSM) physical page pool resolution and shadow cascades.",
            **shd_tl,
        },
        {
            "key": "sg.ReflectionQuality",
            "name": "Reflection Quality (Lumen Reflections / SSR)",
            "value": refl_label,
            "impact": "Surface Realism (Heavy GPU Ray Tracing)",
            "category": "Fidelity",
            "note": "Controls Lumen reflection rays, roughness cutoffs, and screen-space reflection precision.",
            **refl_tl,
        },
        {
            "key": "sg.TextureQuality",
            "name": "Texture Quality & Anisotropic Filtering",
            "value": tex_label,
            "impact": "Surface Sharpness & VRAM Pool Size",
            "category": "Fidelity",
            "note": "Controls `r.Streaming.PoolSize` (VRAM budget) and max anisotropic filtering (16x at Epic).",
            **tex_tl,
        },
        {
            "key": "sg.AntiAliasingQuality",
            "name": "Anti-Aliasing & TSR Upscaling Precision",
            "value": aa_label,
            "impact": "Edge Stability & Sub-Pixel Detail",
            "category": "Fidelity",
            "note": "Controls Temporal Super Resolution (TSR) history sample count and anti-ghosting.",
            **aa_tl,
        },
        {
            "key": "sg.ShadingQuality & sg.PostProcessQuality",
            "name": "Material Shading & Post-Processing",
            "value": f"Shading: {shade_label} | PostFX: {post_label}",
            "impact": "Subsurface Scattering, Bloom, DoF & Motion Blur",
            "category": "Fidelity",
            "note": "Controls complex material shader permutations, ambient occlusion, and cinematic depth of field.",
            **shd_post_tl,
        },
        {
            "key": "sg.LandscapeQuality",
            "name": "Landscape & Terrain Tessellation Detail",
            "value": land_label,
            "impact": "Terrain Geometry Fidelity",
            "category": "Fidelity",
            "note": "Controls landscape LOD distribution and virtual heightfield mesh precision.",
            **land_tl,
        },
        {
            "key": "bUseHDRDisplayOutput / HDRDisplayOutputNits",
            "name": "HDR10 Display Output & Peak Luminance",
            "value": f"{'Enabled' if hdr_bool else 'Disabled (SDR)'} ({hdr_nits} nits)" if has_ini else "SDR Default (1000 nits)",
            "impact": "Dynamic Range & Color Gamut (Rec.2020)",
            "category": "Fidelity",
            "note": "Controls ACES HDR tonemapper output curve and peak nit clipping.",
            **hdr_tl,
        },
    ]

    quality_levels = [
        lvl for lvl in (view_dist_lvl, aa_lvl, shadow_lvl, gi_lvl, refl_lvl, post_lvl, tex_lvl, fx_lvl, fol_lvl, shade_lvl)
        if lvl is not None
    ]
    avg_quality = round(sum(quality_levels) / len(quality_levels), 2) if quality_levels else 3.0
    pixel_ratio = (res_x * res_y) / max(1, (1920 * 1080))
    est_vram_gb = round(3.8 + (pixel_ratio * 1.35) + (avg_quality * 1.15), 1)

    comparisons: list[dict[str, str]] = []

    if eval_vram_gb >= est_vram_gb + 2.5 and tex_tl["traffic_light"] == "green" and res_tl["traffic_light"] == "green":
        vram_light, vram_state = "green", "ok"
        vram_verdict = f"GREEN — OPTIMAL ({eval_vram_gb} GB vs ~{est_vram_gb} GB)"
        vram_advice = (
            f"Target profile [{ref_tier['tier_name']}] comfortably handles {res_x}x{res_y} with Textures at {tex_label} "
            f"(Recommended: {tex_tl['recommended_value']})."
        )
    elif eval_vram_gb < est_vram_gb or tex_tl["traffic_light"] == "red" or res_tl["traffic_light"] == "red":
        vram_light, vram_state = "red", "missing"
        vram_verdict = f"RED — VRAM OVER-BUDGET (~{est_vram_gb} GB vs {eval_vram_gb} GB)"
        vram_advice = (
            f"Current {res_x}x{res_y} + Textures at {tex_label} requires ~{est_vram_gb} GB VRAM, exceeding {ref_tier['tier_name']} ({eval_vram_gb} GB). "
            f"Recommended: Resolution {ref_tier['target_resolution']} and sg.TextureQuality = {tex_tl['recommended_value']}."
        )
    else:
        vram_light, vram_state = "amber", "pending"
        vram_verdict = f"AMBER — SUB-OPTIMAL ({eval_vram_gb} GB vs ~{est_vram_gb} GB)"
        vram_advice = f"{tex_tl['advice']} {res_tl['advice']}"

    comparisons.append(
        {
            "aspect": "1. GPU VRAM vs. Resolution & Texture Streaming Pool",
            "pc_spec": eval_hw_label,
            "game_setting": f"{res_x}x{res_y} | Textures: {tex_label} | Est. Pool: ~{est_vram_gb} GB",
            "recommended_setting": f"Res: {ref_tier['target_resolution']} | Textures: {tex_tl['recommended_value']}",
            "traffic_light": vram_light,
            "state": vram_state,
            "verdict": vram_verdict,
            "recommendation": vram_advice,
        }
    )

    light_tls = [gi_tl["traffic_light"], shd_tl["traffic_light"], refl_tl["traffic_light"]]
    if "red" in light_tls:
        light_light, light_state = "red", "missing"
        light_verdict = "RED — LUMEN / VSM EXCEEDS GPU TIER"
        light_advice = f"{gi_tl['advice']} {shd_tl['advice']} {refl_tl['advice']}"
    elif "amber" in light_tls:
        light_light, light_state = "amber", "pending"
        light_verdict = "AMBER — SUB-OPTIMAL LIGHTING / SHADOWS"
        light_advice = f"{gi_tl['advice']} {shd_tl['advice']}"
    else:
        light_light, light_state = "green", "ok"
        light_verdict = "GREEN — OPTIMAL LUMEN GI & VSM SHADOWS"
        light_advice = f"GI ({gi_label}), Shadows ({shadow_label}), and Reflections ({refl_label}) match the reference array targets for {ref_tier['tier_name']}."

    comparisons.append(
        {
            "aspect": "2. GPU Architecture vs. Lumen GI, Virtual Shadows & Reflections",
            "pc_spec": f"{ref_tier['tier_name']} ({ref_tier['example_hardware']})",
            "game_setting": f"GI: {gi_label} | Shadows: {shadow_label} | Reflections: {refl_label}",
            "recommended_setting": (
                f"GI: {gi_tl['recommended_value']} | Shadows: {shd_tl['recommended_value']} | Reflections: {refl_tl['recommended_value']}"
            ),
            "traffic_light": light_light,
            "state": light_state,
            "verdict": light_verdict,
            "recommendation": light_advice,
        }
    )

    cpu_tls = [vd_tl["traffic_light"], fol_tl["traffic_light"]]
    if "red" in cpu_tls or eval_cpu_cores < 6 and (view_dist_lvl or 3) >= 3:
        cpu_light, cpu_state = "red", "missing"
        cpu_verdict = "RED — DRAW-CALL / FOLIAGE OVERLOAD"
        cpu_advice = f"{vd_tl['advice']} {fol_tl['advice']}"
    elif "amber" in cpu_tls:
        cpu_light, cpu_state = "amber", "pending"
        cpu_verdict = "AMBER — SUB-OPTIMAL VIEW DISTANCE / FOLIAGE"
        cpu_advice = f"{vd_tl['advice']} {fol_tl['advice']}"
    else:
        cpu_light, cpu_state = "green", "ok"
        cpu_verdict = f"GREEN — OPTIMAL CPU DRAW-CALL BALANCE ({eval_cpu_cores}+ Cores)"
        cpu_advice = f"View Distance ({view_dist_label}) and Foliage ({fol_label}) match {ref_tier['tier_name']} targets."

    comparisons.append(
        {
            "aspect": "3. CPU Multi-Core Draw Calls vs. View Distance & Foliage",
            "pc_spec": f"{cpu_name} ({cpu_cores}C / {cpu_threads}T)" if not is_simulated_tier else f"{eval_cpu_cores}+ Core CPU ({ref_tier['tier_name']})",
            "game_setting": f"View Distance: {view_dist_label} | Foliage: {fol_label}",
            "recommended_setting": f"View Distance: {vd_tl['recommended_value']} | Foliage: {fol_tl['recommended_value']}",
            "traffic_light": cpu_light,
            "state": cpu_state,
            "verdict": cpu_verdict,
            "recommendation": cpu_advice,
        }
    )

    if scale_tl["traffic_light"] == "red" or res_tl["traffic_light"] == "red":
        disp_light, disp_state = "red", "missing"
        disp_verdict = "RED — RESOLUTION / 3D SCALE EXCEEDS TIER"
        disp_advice = f"{res_tl['advice']} {scale_tl['advice']}"
    elif scale_tl["traffic_light"] == "amber" or res_tl["traffic_light"] == "amber" or vsync_tl["traffic_light"] == "amber":
        disp_light, disp_state = "amber", "pending"
        disp_verdict = "AMBER — DISPLAY / VSYNC TUNING ADVISED"
        disp_advice = f"{res_tl['advice']} {vsync_tl['advice']}"
    else:
        disp_light, disp_state = "green", "ok"
        disp_verdict = f"GREEN — OPTIMAL DISPLAY & SCALING ({res_x}x{res_y})"
        disp_advice = f"Resolution ({res_x}x{res_y}), 3D Scale ({res_scale_label}), and Fullscreen mode match {ref_tier['tier_name']}."

    comparisons.append(
        {
            "aspect": "4. Display Resolution, 3D Render Scale & Sync",
            "pc_spec": f"{native_w}x{native_h} @ {native_hz} Hz Primary Display",
            "game_setting": f"{res_x}x{res_y} (Scale: {res_scale_label}) | VSync: {vsync_bool} | {fps_limit_label}",
            "recommended_setting": f"Res: {ref_tier['target_resolution']} | Scale: {ref_tier['recommended_res_scale']}% | DRS: {ref_tier['dynamic_resolution']}",
            "traffic_light": disp_light,
            "state": disp_state,
            "verdict": disp_verdict,
            "recommendation": disp_advice,
        }
    )

    if eval_ram_gb < 16.0 and avg_quality >= 2.5:
        ram_light, ram_state = "red", "missing"
        ram_verdict = f"RED — INSUFFICIENT RAM ({eval_ram_gb} GB)"
        ram_advice = "Unreal Engine 5 World Partition at High/Epic requires 16-32GB RAM to avoid severe pagefile hitches."
    elif eval_ram_gb < 24.0 and res_y >= 2160 and avg_quality >= 3.0:
        ram_light, ram_state = "amber", "pending"
        ram_verdict = "AMBER — 16GB RAM TIGHT AT 4K EPIC"
        ram_advice = "16GB system RAM is borderline for 4K Epic World Partition streaming; close background browsers or use High (2) Textures."
    else:
        ram_light, ram_state = "green", "ok"
        ram_verdict = f"GREEN — OPTIMAL SYSTEM MEMORY ({eval_ram_gb} GB RAM)"
        ram_advice = f"{eval_ram_gb} GB RAM provides smooth World Partition asset and PSO shader cache streaming."

    comparisons.append(
        {
            "aspect": "5. System Memory (RAM) vs. World Partition & Shader Streaming",
            "pc_spec": f"{ram_gb} GB Total RAM ({pc_specs.get('ram', {}).get('available_gb', 0)} GB Free)" if not is_simulated_tier else f"{eval_ram_gb} GB System RAM ({ref_tier['tier_name']})",
            "game_setting": f"Avg Scalability Tier: {avg_quality:.1f} / 4.0 ({_UE_QUALITY_LABELS.get(round(avg_quality), 'Epic')})",
            "recommended_setting": f"Min {ref_tier['min_ram_gb']} GB RAM for {ref_tier['tier_name']}",
            "traffic_light": ram_light,
            "state": ram_state,
            "verdict": ram_verdict,
            "recommendation": ram_advice,
        }
    )

    all_evaluated = performance_settings + fidelity_settings
    green_cnt = sum(1 for item in all_evaluated if item.get("traffic_light") == "green")
    amber_cnt = sum(1 for item in all_evaluated if item.get("traffic_light") == "amber")
    red_cnt = sum(1 for item in all_evaluated if item.get("traffic_light") == "red")

    if not has_ini:
        overall_light, overall_state = "amber", "pending"
        overall_verdict = (
            f"AMBER — No Saved GameUserSettings.ini Found Yet for '{detected_project}' "
            f"(Showing Reference Targets for {ref_tier['tier_name']})"
        )
    elif red_cnt > 0 or any(c["traffic_light"] == "red" for c in comparisons):
        overall_light, overall_state = "red", "missing"
        overall_verdict = (
            f"RED (NOT OPTIMAL — {red_cnt} Critical Bottleneck(s), {amber_cnt} Warning(s)): "
            f"Current Settings Exceed Safe Limits for {ref_tier['tier_name']}"
        )
    elif amber_cnt > 2:
        overall_light, overall_state = "amber", "pending"
        overall_verdict = (
            f"AMBER (SUB-OPTIMAL — {green_cnt} Green, {amber_cnt} Amber): "
            f"Playable on {ref_tier['tier_name']}, with Tuning / Headroom Adjustments Advised"
        )
    else:
        overall_light, overall_state = "green", "ok"
        overall_verdict = (
            f"GREEN (OPTIMAL — {green_cnt}/{len(all_evaluated)} Settings Optimal"
            + (f", {amber_cnt} Minor Note" if amber_cnt else "")
            + f"): Current {res_x}x{res_y} {_UE_QUALITY_LABELS.get(round(avg_quality), 'Epic')} Config Ideally Matches {ref_tier['tier_name']}"
        )

    return {
        "detected_project_name": detected_project,
        "ini_found": has_ini,
        "ini_path": str(found_gus_path.resolve()) if found_gus_path else None,
        "engine_ini_path": str(found_engine_ini_path.resolve()) if found_engine_ini_path else None,
        "searched_locations": searched_locations[:6],
        "available_local_profiles": discover_local_unreal_ini_profiles(),
        "active_reference_tier_id": (reference_tier_id or "auto").strip().lower(),
        "active_reference_tier": ref_tier,
        "reference_array": UNREAL_HARDWARE_REFERENCE_ARRAY,
        "traffic_light_summary": {
            "overall_light": overall_light,
            "overall_state": overall_state,
            "overall_verdict": overall_verdict,
            "green_count": green_cnt,
            "amber_count": amber_cnt,
            "red_count": red_cnt,
            "total_settings": len(all_evaluated),
        },
        "summary_metrics": {
            "resolution": f"{res_x}x{res_y}",
            "fullscreen_mode": fs_mode_label,
            "vsync": vsync_bool,
            "frame_rate_limit": fps_limit_label,
            "average_scalability_level": avg_quality,
            "average_scalability_label": _UE_QUALITY_LABELS.get(round(avg_quality), f"Level {avg_quality}"),
            "estimated_vram_gb": est_vram_gb,
        },
        "performance_settings": performance_settings,
        "fidelity_settings": fidelity_settings,
        "spec_comparison": {
            "overall_light": overall_light,
            "overall_state": overall_state,
            "overall_verdict": overall_verdict,
            "estimated_vram_gb": est_vram_gb,
            "available_vram_gb": eval_vram_gb,
            "items": comparisons,
        },
        "raw_sections": sections,
    }


def apply_unreal_ini_updates(
    ini_file: Path,
    game_slug: str,
    updates: dict[str, str],
) -> Path:
    """Back up *ini_file* if not already backed up and apply key=value updates."""
    UNREAL_INI_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    bak_path = UNREAL_INI_BACKUP_DIR / f"{game_slug}_{ini_file.name}.bak"
    if not bak_path.is_file():
        shutil.copy2(ini_file, bak_path)

    lines = ini_file.read_text(encoding="utf-8", errors="replace").splitlines()
    remaining = {k.strip(): str(v).strip() for k, v in updates.items() if k.strip()}
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith(";"):
            k, _ = stripped.split("=", 1)
            k_clean = k.strip()
            if k_clean in remaining:
                new_lines.append(f"{k_clean}={remaining.pop(k_clean)}")
                continue
        new_lines.append(line)

    if remaining:
        new_lines.append("")
        new_lines.append("[ScalabilityGroups]")
        for k, v in remaining.items():
            new_lines.append(f"{k}={v}")

    ini_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return bak_path


def restore_unreal_ini_backup(ini_file: Path, game_slug: str) -> Path:
    """Restore *ini_file* from its backed-up .bak copy."""
    bak_path = UNREAL_INI_BACKUP_DIR / f"{game_slug}_{ini_file.name}.bak"
    if not bak_path.is_file():
        raise FileNotFoundError(f"No INI backup found at {bak_path}")
    shutil.copy2(bak_path, ini_file)
    return bak_path


def scan_unreal_mods(
    game_dir: Path,
    add_mod_fn: Callable[..., None],
    folders_scanned: list[str],
    managed_by: list[str],
    rel_fn: Callable[[Path], str],
    clean_package_fn: Callable[[str], str],
) -> None:
    """Scan Unreal ~mods, LogicMods, Paks/Mods, *_P.pak, and UE4SS Lua/Native mods."""
    for paks_dir in game_dir.glob("*/Content/Paks"):
        if not paks_dir.is_dir():
            continue
        for sub_name, cat_label in (
            ("~mods", "Unreal Pak (~mods)"),
            ("LogicMods", "Unreal LogicMod"),
            ("Mods", "Unreal Pak (Mods)"),
        ):
            sub_dir = paks_dir / sub_name
            if not sub_dir.is_dir():
                continue
            folders_scanned.append(rel_fn(sub_dir))
            if (sub_dir / "vortex.deployment.json").is_file() and "Vortex" not in managed_by:
                managed_by.append("Vortex")
            pak_stems: set[tuple[Path, str]] = set()
            all_files = sorted(
                [p for ext in ("*.pak", "*.ucas", "*.utoc") for p in sub_dir.rglob(ext) if p.is_file()],
                key=lambda x: (x.suffix.lower() != ".pak", str(x).lower()),
            )
            for mf in all_files:
                stem_key = (mf.parent, mf.stem.lower())
                if mf.suffix.lower() in (".ucas", ".utoc") and stem_key in pak_stems:
                    continue
                if mf.suffix.lower() == ".pak":
                    pak_stems.add(stem_key)
                pkg_raw = mf.parent.name if mf.parent != sub_dir else ""
                pkg_clean = clean_package_fn(pkg_raw) if pkg_raw else ""
                add_mod_fn(
                    name=pkg_clean or mf.stem,
                    file_name=mf.name,
                    category=cat_label,
                    path_obj=mf,
                    enabled=True,
                    package=pkg_raw,
                )
        for patch_pak in sorted(paks_dir.glob("*_P.pak")):
            if patch_pak.is_file():
                add_mod_fn(
                    name=patch_pak.stem,
                    file_name=patch_pak.name,
                    category="Unreal Patch Pak",
                    path_obj=patch_pak,
                    enabled=True,
                )

    for ue_mods_glob in ("*/Binaries/Win64/Mods", "*/Binaries/Win64/ue4ss/Mods"):
        for ue_mods_dir in game_dir.glob(ue_mods_glob):
            if not ue_mods_dir.is_dir():
                continue
            folders_scanned.append(rel_fn(ue_mods_dir))
            for mod_sub in sorted(ue_mods_dir.iterdir()):
                if mod_sub.is_dir() and mod_sub.name.lower() != "shared":
                    enabled = (mod_sub / "enabled.txt").is_file()
                    add_mod_fn(
                        name=mod_sub.name,
                        file_name=mod_sub.name,
                        category="UE4SS Mod",
                        path_obj=mod_sub,
                        enabled=enabled,
                        size_kb=0.0,
                    )


def build_unreal_crucial_steps(
    info: dict[str, Any],
    *,
    ac_clear: bool,
    binaries_ok: bool,
) -> list[dict[str, str]]:
    """Build the 6 Unreal Engine crucial modding steps."""
    ue4ss_ok = bool(info.get("ue4ss_installed"))
    ue_mods_cnt = int(info.get("ue_mods_count", 0))
    return [
        {
            "step": "1. Anti-Cheat & Safety Check",
            "state": "ok" if ac_clear else "blocked",
            "label": "Clear (Safe to Mod)" if ac_clear else "Blocked (Anti-Cheat / Live-Service)",
            "detail": "Zero anti-cheat drivers detected" if ac_clear else str(info.get("discovery_mcp_tools", "")),
        },
        {
            "step": "2. Unreal Engine & Content Archives",
            "state": "ok" if binaries_ok else "missing",
            "label": f"Unreal Engine 4/5 ({info.get('pak_count', 0)} .pak archive(s))",
            "detail": f"{info.get('executable_rel') or info.get('executable')} (Content/Paks)",
        },
        {
            "step": "3. Unreal Mod Loader (RE-UE4SS)",
            "state": "ok" if ue4ss_ok else "na",
            "label": "RE-UE4SS Installed" if ue4ss_ok else "Optional — Install RE-UE4SS for Lua Mods",
            "detail": "*/Binaries/Win64/UE4SS.dll + dwmapi.dll",
        },
        {
            "step": "4. Unreal Lua / Blueprint Mods",
            "state": "ok" if ue_mods_cnt > 0 else "na",
            "label": f"{ue_mods_cnt} Mod Folder(s) Active" if ue_mods_cnt > 0 else "No Lua Mods Installed Yet",
            "detail": "*/Binaries/Win64/Mods/ (Build via Build a Mod tab)",
        },
        {
            "step": "5. Unreal INI & Hardware Advisor",
            "state": "ok",
            "label": "GameUserSettings.ini Inspection Ready",
            "detail": "Compare & apply optimal scalability settings in Unreal section below",
        },
        {
            "step": "6. INI Backup & Restore",
            "state": "ok",
            "label": "Automatic Pre-Edit Backup Enabled",
            "detail": "Backups saved in %LOCALAPPDATA%\\TheModdingTool\\unreal_ini_backups",
        },
    ]


class UnrealEngineAdapter(EngineAdapter):
    """Engine adapter for Unreal Engine 4 and 5 titles."""

    engine_id = "unreal"
    display_name = "Unreal Engine 4/5"

    def detect(self, game_dir: Path) -> bool:
        if not game_dir.is_dir():
            return False
        if has_unreal_editor_without_shipping(game_dir):
            return False
        return (
            (game_dir / "Engine" / "Binaries" / "Win64").is_dir()
            or any(game_dir.rglob("*.pak"))
            or any(game_dir.rglob("*.utoc"))
            or any(game_dir.rglob("*.ucas"))
            or any(game_dir.rglob("*-Win64-Shipping.exe"))
        )

    def fingerprint(self, game_dir: Path) -> dict[str, Any]:
        pak_files = list(game_dir.glob("*/Content/Paks/*.pak")) + list(game_dir.glob("*/Content/Paks/*.utoc")) if game_dir.is_dir() else []
        ue4ss_installed = check_unreal_loaders(game_dir)
        return {
            "game_dir": str(game_dir),
            "engine": "Unreal_4_5" if self.detect(game_dir) else "Unknown",
            "is_unreal_engine": self.detect(game_dir),
            "pak_count": len(pak_files),
            "ue4ss_installed": ue4ss_installed,
            "status": "ready" if ue4ss_installed else "external_tooling",
        }

    def discover_configs(self, game_dir: Path, **kwargs: Any) -> dict[str, Any]:
        return {
            "engine": "unreal",
            "local_profiles": discover_local_unreal_ini_profiles(),
            "in_game_inis": [str(p) for p in game_dir.rglob("*.ini") if p.is_file()][:25] if game_dir.is_dir() else [],
        }

    def check_readiness(self, game_dir: Path, info: dict[str, Any] | None = None) -> dict[str, Any]:
        ue4ss_installed = check_unreal_loaders(game_dir)
        return {
            "ready": ue4ss_installed,
            "modding_readiness": "External Tooling",
            "unreal_ue4ss": ue4ss_installed,
        }

    def get_log_paths(self, game_dir: Path) -> list[Path]:
        if not game_dir.is_dir():
            return []
        logs: list[Path] = []
        for pat in ("*/Binaries/Win64/UE4SS.log", "*/Binaries/Win64/ue4ss/UE4SS.log", "*/Saved/Logs/*.log"):
            logs.extend(p for p in game_dir.glob(pat) if p.is_file())
        return logs[:10]

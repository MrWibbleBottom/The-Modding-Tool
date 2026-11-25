"""Shared engine, discovery, hardware reference, and deployment constants."""

from __future__ import annotations

from typing import Any

ANTI_CHEAT_BINARIES: dict[str, str] = {
    "easyanticheat.exe": "EasyAntiCheat.exe",
    "beservice.exe": "BEService.exe",
    "vgk.sys": "vgk.sys",
    "gameguard.des": "GameGuard.des",
}

LIBRARY_ANTI_CHEAT_MARKERS: tuple[str, ...] = (
    "easyanticheat.exe",
    "easyanticheat_eos.exe",
    "beservice.exe",
    "vgk.sys",
    "gameguard.des",
)

PLUGIN_DEPLOY_FILES: list[str] = [
    "BepInExMCP.IL2CPP.dll",
    "BepInExMCP.IL2CPP.deps.json",
    "Microsoft.CodeAnalysis.dll",
    "Microsoft.CodeAnalysis.CSharp.dll",
    "BepInExMCP.IL2CPP.pdb",
]

_UE_QUALITY_LABELS: dict[int, str] = {
    0: "Low (0)",
    1: "Medium (1)",
    2: "High (2)",
    3: "Epic (3)",
    4: "Cinematic (4)",
}

NON_GAME_STEAM_APPIDS: frozenset[str] = frozenset({
    "228980",   # Steamworks Common Redistributables
    "250820",   # SteamVR
    "431960",   # Wallpaper Engine
    "993090",   # Lossless Scaling
    "223850",   # 3DMark
    "1070560",  # Steam Linux Runtime 1.0 (scout)
    "1391110",  # Steam Linux Runtime 2.0 (soldier)
    "1493710",  # Proton Experimental
    "1628350",  # Steam Linux Runtime 3.0 (sniper)
    "2348590",  # Proton 8.0 / runtime
})

NON_GAME_NAME_PATTERNS: tuple[str, ...] = (
    "unreal engine",
    "ue_4",
    "ue_5",
    "steamworks common redistributables",
    "steamworks shared",
    "steam controller configs",
    "steamvr",
    "proton ",
    "steam linux runtime",
    "dedicated server",
    " sdk",
    "modkit",
    "mod kit",
    "creation kit",
    "authoring tools",
    "level editor",
    "soundtrack",
    "artbook",
    "digital art book",
    "benchmark",
    "3dmark",
    "last crusade pack",
)

NON_GAME_LOCALAPPDATA_FOLDERS: frozenset[str] = frozenset({
    "unrealengine",
    "unrealheadertool",
    "epicgameslauncher",
    "crashreportclient",
    "microsoft",
    "packages",
    "programs",
    "temp",
    "steam",
    "nvidia",
    "amd",
    "google",
    "antigravity",
})

ENGINE_TOOL_MATRIX: dict[str, dict[str, list[str]]] = {
    "unity_il2cpp": {
        "allowed_mcp_bridges": ["bepinex-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["BepInEx (IL2CPP)", "Il2CppDumper", "AssetRipper"],
    },
    "unity_mono": {
        "allowed_mcp_bridges": ["bepinex-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["BepInEx (Mono)", "ILSpy", "AssetRipper"],
    },
    "unreal": {
        "allowed_mcp_bridges": ["ue4ss-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["UE4SS", "FModel", "UnrealPak"],
    },
    "godot": {
        "allowed_mcp_bridges": ["godot-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["GDRE Tools"],
    },
    "creation_engine": {
        "allowed_mcp_bridges": ["creation-kit-mcp", "filesystem-mcp"],
        "allowed_loaders_and_cli": ["xEdit", "BAE"],
    },
    "generic": {
        "allowed_mcp_bridges": ["filesystem-mcp"],
        "allowed_loaders_and_cli": [],
    },
}

ENGINE_ADAPTER_REGISTRY: dict[str, str] = {
    "Unity_IL2CPP": "bepinex-mcp",
    "Unity_Mono": "bepinex-mcp",
    "Unreal_4_5": "ue4ss-mcp",
    "Creation_Engine": "creation-kit-mcp",
    "Godot": "godot-mcp",
    "Data_Driven": "filesystem-mcp",
    "Unknown": "unknown",
}

UNREAL_HARDWARE_REFERENCE_ARRAY: list[dict[str, Any]] = [
    {
        "tier_id": "tier_flagship_24gb_4090",
        "tier_name": "Flagship 24GB+ (RTX 4090 / 5090 / 3090 Ti / RX 7900 XTX)",
        "example_hardware": "NVIDIA RTX 4090 24GB | 8-16+ Core CPU (i9 / Ryzen 9) | 32-64GB RAM",
        "min_vram_gb": 20.0,
        "max_vram_gb": 48.0,
        "min_cpu_cores": 8,
        "min_ram_gb": 24.0,
        "target_resolution": "3840x2160",
        "target_res_x": 3840,
        "target_res_y": 2160,
        "max_safe_res_y": 2160,
        "recommended_res_scale": 100,
        "min_safe_res_scale": 75,
        "vram_budget_ceiling_gb": 22.5,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — TSR High / DLAA"},
            "sg.ShadowQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full VSM"},
            "sg.GlobalIlluminationQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Hardware Lumen GI"},
            "sg.ReflectionQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Hardware Lumen Reflections"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full Uncompressed Pool"},
            "sg.EffectsQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Full Niagara GPU Particles"},
            "sg.FoliageQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3) — Max Nanite Foliage"},
            "sg.ShadingQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 4, "label": "Epic (3)"},
        },
        "notes": "24GB VRAM + Ada Lovelace SER easily sustains native 4K Epic (3) / Cinematic (4) with Hardware Lumen GI, Virtual Shadow Maps, and 12GB+ texture pools.",
    },
    {
        "tier_id": "tier_high_16gb_4080",
        "tier_name": "High-End 16GB (RTX 4080 / 4070 Ti Super / RX 7900 XT / 6950 XT)",
        "example_hardware": "NVIDIA RTX 4080 16GB | 8+ Core CPU (i7 / Ryzen 7) | 32GB RAM",
        "min_vram_gb": 15.0,
        "max_vram_gb": 19.9,
        "min_cpu_cores": 8,
        "min_ram_gb": 16.0,
        "target_resolution": "3840x2160 (TSR/DLSS 85%) or 2560x1440 Native",
        "target_res_x": 3840,
        "target_res_y": 2160,
        "max_safe_res_y": 2160,
        "recommended_res_scale": 85,
        "min_safe_res_scale": 67,
        "vram_budget_ceiling_gb": 15.0,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadowQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.GlobalIlluminationQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) — Lumen GI"},
            "sg.ReflectionQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) — 8-10GB Pool"},
            "sg.EffectsQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.FoliageQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
        },
        "notes": "16GB VRAM sustains Epic (3) across all scalability groups at 1440p Native or 4K with DLSS/TSR Quality (75-85%).",
    },
    {
        "tier_id": "tier_upper_mid_12gb_4070_3080",
        "tier_name": "Upper-Mid 12GB (RTX 4070 / RTX 3080 12GB / 3080 Ti / RX 7800 XT)",
        "example_hardware": "NVIDIA RTX 4070 / 3080 12GB | 6-8 Core CPU | 16-32GB RAM",
        "min_vram_gb": 11.0,
        "max_vram_gb": 14.9,
        "min_cpu_cores": 6,
        "min_ram_gb": 16.0,
        "target_resolution": "2560x1440",
        "target_res_x": 2560,
        "target_res_y": 1440,
        "max_safe_res_y": 1440,
        "recommended_res_scale": 85,
        "min_safe_res_scale": 67,
        "vram_budget_ceiling_gb": 11.2,
        "dynamic_resolution": False,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.AntiAliasingQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.ShadowQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — Balanced VSM"},
            "sg.GlobalIlluminationQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — Software Lumen"},
            "sg.ReflectionQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.PostProcessQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
            "sg.TextureQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3) at 1440p / High (2) at 4K"},
            "sg.EffectsQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.FoliageQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.ShadingQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.LandscapeQuality": {"rec": 3, "max_safe": 3, "label": "Epic (3)"},
        },
        "notes": "Optimal for 1440p High (2) / Epic Textures (3). Running native 4K Epic with Lumen + VSM will exceed the 12GB VRAM pool.",
    },
    {
        "tier_id": "tier_mid_8gb_3000_4060",
        "tier_name": "Mainstream 8GB RTX 3000 / 4060 Gen (RTX 3070 / 3060 Ti / 3060 8GB / 4060 / RX 6600 XT)",
        "example_hardware": "NVIDIA 8GB RTX 3000 Series (RTX 3070 / 3060 Ti) | 6+ Core CPU | 16GB RAM",
        "min_vram_gb": 7.5,
        "max_vram_gb": 10.9,
        "min_cpu_cores": 6,
        "min_ram_gb": 16.0,
        "target_resolution": "1920x1080 (or 2560x1440 with DLSS/TSR 67%)",
        "target_res_x": 1920,
        "target_res_y": 1080,
        "max_safe_res_y": 1440,
        "recommended_res_scale": 75,
        "min_safe_res_scale": 60,
        "vram_budget_ceiling_gb": 7.6,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.AntiAliasingQuality": {"rec": 2, "max_safe": 3, "label": "High (2) — TSR / DLSS Quality"},
            "sg.ShadowQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — VSM Pool Saver"},
            "sg.GlobalIlluminationQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — Software Lumen"},
            "sg.ReflectionQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — SSR / Hybrid"},
            "sg.PostProcessQuality": {"rec": 2, "max_safe": 3, "label": "High (2)"},
            "sg.TextureQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — 8GB Safe Pool (Epic overflows)"},
            "sg.EffectsQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.FoliageQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.ShadingQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
            "sg.LandscapeQuality": {"rec": 2, "max_safe": 2, "label": "High (2)"},
        },
        "notes": "8GB VRAM ceiling requires High (2) Textures and Medium (1) / High (2) Shadows & Reflections; 4K or Epic (3) Textures + VSM causes severe 8GB VRAM exhaustion.",
    },
    {
        "tier_id": "tier_entry_6gb_2060_1660",
        "tier_name": "Entry 6GB GPU (RTX 2060 6GB / GTX 1660 Super / RTX 3050 6GB / RX 5600 XT)",
        "example_hardware": "NVIDIA RTX 2060 / GTX 1660S 6GB | 4-6 Core CPU | 16GB RAM",
        "min_vram_gb": 5.5,
        "max_vram_gb": 7.4,
        "min_cpu_cores": 4,
        "min_ram_gb": 12.0,
        "target_resolution": "1920x1080 (TSR/DLSS 67% Scale)",
        "target_res_x": 1920,
        "target_res_y": 1080,
        "max_safe_res_y": 1080,
        "recommended_res_scale": 67,
        "min_safe_res_scale": 50,
        "vram_budget_ceiling_gb": 5.6,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.AntiAliasingQuality": {"rec": 2, "max_safe": 2, "label": "High (2) — TSR Upscaling"},
            "sg.ShadowQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — CSM Shadows"},
            "sg.GlobalIlluminationQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — Screen-Space GI"},
            "sg.ReflectionQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — SSR"},
            "sg.PostProcessQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.TextureQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1) — 6GB Pool Cap"},
            "sg.EffectsQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1)"},
            "sg.FoliageQuality": {"rec": 1, "max_safe": 1, "label": "Medium (1)"},
            "sg.ShadingQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
            "sg.LandscapeQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1)"},
        },
        "notes": "6GB VRAM requires 1080p with Medium (1) Scalability and TSR/DLSS 67% render scale to avoid PCIe VRAM spilling.",
    },
    {
        "tier_id": "tier_budget_4gb_1650_igpu",
        "tier_name": "Budget 4GB / Handheld / iGPU (GTX 1650 / RX 6400 / Steam Deck / ROG Ally)",
        "example_hardware": "4GB VRAM or Shared APU Memory | 4-8 Core CPU | 8-16GB RAM",
        "min_vram_gb": 0.0,
        "max_vram_gb": 5.4,
        "min_cpu_cores": 4,
        "min_ram_gb": 8.0,
        "target_resolution": "1280x720 or 1920x1080 (50% Scale)",
        "target_res_x": 1280,
        "target_res_y": 720,
        "max_safe_res_y": 1080,
        "recommended_res_scale": 50,
        "min_safe_res_scale": 50,
        "vram_budget_ceiling_gb": 3.8,
        "dynamic_resolution": True,
        "scalability_targets": {
            "sg.ViewDistanceQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.AntiAliasingQuality": {"rec": 1, "max_safe": 2, "label": "Medium (1) — TSR Performance"},
            "sg.ShadowQuality": {"rec": 0, "max_safe": 0, "label": "Low (0)"},
            "sg.GlobalIlluminationQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.ReflectionQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.PostProcessQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.TextureQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.EffectsQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.FoliageQuality": {"rec": 0, "max_safe": 0, "label": "Low (0)"},
            "sg.ShadingQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
            "sg.LandscapeQuality": {"rec": 0, "max_safe": 1, "label": "Low (0)"},
        },
        "notes": "4GB VRAM / APU requires Low (0) Scalability, 720p-1080p at 50% 3D Resolution Scale, and Dynamic Resolution Enabled.",
    },
]

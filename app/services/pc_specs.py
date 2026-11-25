"""PC hardware & OS specification scraper with disk/memory caching."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import PC_SPECS_CACHE_PATH, atomic_write_json

_PC_SPECS_MEM_CACHE: dict[str, Any] | None = None


def clear_local_pc_specs_cache() -> None:
    global _PC_SPECS_MEM_CACHE
    _PC_SPECS_MEM_CACHE = None


def scrape_pc_specs(force_refresh: bool = False) -> dict[str, Any]:
    """Scrape comprehensive PC hardware & OS specifications (CPU, GPU, VRAM, RAM, Display, Storage, Tier)."""
    global _PC_SPECS_MEM_CACHE
    if not force_refresh and _PC_SPECS_MEM_CACHE is not None:
        return _PC_SPECS_MEM_CACHE
    if not force_refresh and PC_SPECS_CACHE_PATH.is_file():
        try:
            cached = json.loads(PC_SPECS_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("cpu") and cached.get("gpu"):
                _PC_SPECS_MEM_CACHE = cached
                return cached
        except Exception:
            pass

    import ctypes
    import platform
    import subprocess
    import winreg

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    mem_stat = MEMORYSTATUSEX()
    mem_stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_stat))
    total_ram_gb = round(mem_stat.ullTotalPhys / (1024**3), 1)
    avail_ram_gb = round(mem_stat.ullAvailPhys / (1024**3), 1)
    used_ram_gb = round(total_ram_gb - avail_ram_gb, 1)
    ram_load_pct = int(mem_stat.dwMemoryLoad)

    cpu_name = platform.processor() or "Unknown CPU"
    cpu_mhz = 0
    cpu_vendor = "Unknown"
    logical_threads = os.cpu_count() or 1
    physical_cores = max(1, logical_threads // 2)
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            cpu_name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
            cpu_mhz = int(winreg.QueryValueEx(key, "~MHz")[0])
            cpu_vendor = str(winreg.QueryValueEx(key, "VendorIdentifier")[0]).strip()
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor",
        ) as root_cpu:
            logical_threads = winreg.QueryInfoKey(root_cpu)[0] or logical_threads
            physical_cores = max(1, logical_threads // 2)
    except Exception:
        pass

    board_vendor = "Unknown"
    board_product = "Unknown"
    bios_version = "Unknown"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\BIOS",
        ) as bkey:
            for reg_val, target_name in [
                ("BaseBoardManufacturer", "vendor"),
                ("BaseBoardProduct", "product"),
                ("BIOSVersion", "bios"),
            ]:
                try:
                    v, _ = winreg.QueryValueEx(bkey, reg_val)
                    if isinstance(v, list):
                        v = " ".join(str(x) for x in v)
                    if target_name == "vendor" and v:
                        board_vendor = str(v).strip()
                    elif target_name == "product" and v:
                        board_product = str(v).strip()
                    elif target_name == "bios" and v:
                        bios_version = str(v).strip()
                except OSError:
                    pass
    except Exception:
        pass

    os_product = f"{platform.system()} {platform.release()}"
    os_build = platform.version()
    os_display_ver = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as okey:
            prod, _ = winreg.QueryValueEx(okey, "ProductName")
            build, _ = winreg.QueryValueEx(okey, "CurrentBuildNumber")
            try:
                disp, _ = winreg.QueryValueEx(okey, "DisplayVersion")
                os_display_ver = str(disp)
            except OSError:
                pass
            if int(str(build)) >= 22000 and "Windows 10" in str(prod):
                prod = str(prod).replace("Windows 10", "Windows 11")
            os_product = str(prod)
            os_build = str(build)
    except Exception:
        pass

    screen_w = 1920
    screen_h = 1080
    refresh_hz = 60
    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        screen_w = int(user32.GetSystemMetrics(0)) or 1920
        screen_h = int(user32.GetSystemMetrics(1)) or 1080

        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", ctypes.c_wchar * 32),
                ("dmSpecVersion", ctypes.c_ushort),
                ("dmDriverVersion", ctypes.c_ushort),
                ("dmSize", ctypes.c_ushort),
                ("dmDriverExtra", ctypes.c_ushort),
                ("dmFields", ctypes.c_ulong),
                ("dmPositionX", ctypes.c_long),
                ("dmPositionY", ctypes.c_long),
                ("dmDisplayOrientation", ctypes.c_ulong),
                ("dmDisplayFixedOutput", ctypes.c_ulong),
                ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short),
                ("dmFormName", ctypes.c_wchar * 32),
                ("dmLogPixels", ctypes.c_ushort),
                ("dmBitsPerPel", ctypes.c_ulong),
                ("dmPelsWidth", ctypes.c_ulong),
                ("dmPelsHeight", ctypes.c_ulong),
                ("dmDisplayFlags", ctypes.c_ulong),
                ("dmDisplayFrequency", ctypes.c_ulong),
            ]

        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
            if dm.dmPelsWidth > 0 and dm.dmPelsHeight > 0:
                screen_w = int(dm.dmPelsWidth)
                screen_h = int(dm.dmPelsHeight)
            if dm.dmDisplayFrequency > 0:
                refresh_hz = int(dm.dmDisplayFrequency)
    except Exception:
        pass

    gpu_name = "Unknown GPU"
    vram_total_mb = 0
    vram_free_mb = 0
    gpu_driver = "Unknown"
    gpu_temp_c: float | None = None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            first_gpu = proc.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_gpu.split(",")]
            if len(parts) >= 4:
                gpu_name = parts[0]
                vram_total_mb = int(float(parts[1]))
                vram_free_mb = int(float(parts[2]))
                gpu_driver = parts[3]
            if len(parts) >= 5:
                try:
                    gpu_temp_c = round(float(parts[4]), 1)
                except ValueError:
                    pass
    except Exception:
        pass

    if gpu_name == "Unknown GPU":
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000",
            ) as gkey:
                gpu_name = str(winreg.QueryValueEx(gkey, "DriverDesc")[0])
                gpu_driver = str(winreg.QueryValueEx(gkey, "DriverVersion")[0])
                try:
                    qw_mem, _ = winreg.QueryValueEx(gkey, "HardwareInformation.qwMemorySize")
                    if isinstance(qw_mem, int):
                        vram_total_mb = int(qw_mem // (1024 * 1024))
                except OSError:
                    pass
        except Exception:
            pass

    vram_total_gb = round(vram_total_mb / 1024.0, 1) if vram_total_mb else 0.0
    vram_free_gb = round(vram_free_mb / 1024.0, 1) if vram_free_mb else 0.0

    drives: list[dict[str, Any]] = []
    for drv_letter in ("C:", "D:", "E:", "F:", "G:"):
        drv_path = Path(drv_letter + "\\")
        try:
            if drv_path.is_dir():
                usage = shutil.disk_usage(drv_path)
                total_g = round(usage.total / (1024**3), 1)
                free_g = round(usage.free / (1024**3), 1)
                used_g = round(usage.used / (1024**3), 1)
                pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
                drives.append(
                    {
                        "drive": drv_letter,
                        "total_gb": total_g,
                        "free_gb": free_g,
                        "used_gb": used_g,
                        "used_percent": pct,
                    }
                )
        except Exception:
            pass

    if vram_total_gb >= 16.0 and physical_cores >= 8 and total_ram_gb >= 24.0:
        tier_name = "Enthusiast / Flagship 4K Ultra Tier"
        tier_badge = "Ultra / Epic (4K 60-120+ FPS)"
        rec_ue_preset = 3
        rec_ue_preset_label = "Epic (3) / Cinematic (4) with Hardware Lumen & Virtual Shadow Maps"
        rec_resolution = f"{screen_w}x{screen_h} (Native 4K / High-Refresh)"
    elif vram_total_gb >= 10.0 and physical_cores >= 6 and total_ram_gb >= 16.0:
        tier_name = "High-End 1440p / 4K Upscaled Tier"
        tier_badge = "High / Epic (1440p / DLSS 4K)"
        rec_ue_preset = 3
        rec_ue_preset_label = "High (2) to Epic (3) with TSR / DLSS Quality"
        rec_resolution = "2560x1440 (QHD) or 3840x2160 with DLSS/TSR"
    elif vram_total_gb >= 6.0 and total_ram_gb >= 12.0:
        tier_name = "Mainstream 1080p Gaming Tier"
        tier_badge = "Medium / High (1080p)"
        rec_ue_preset = 2
        rec_ue_preset_label = "Medium (1) to High (2)"
        rec_resolution = "1920x1080 (FHD)"
    else:
        tier_name = "Entry / Integrated Graphics Tier"
        tier_badge = "Low / Medium (720p-1080p)"
        rec_ue_preset = 1
        rec_ue_preset_label = "Low (0) to Medium (1) with Dynamic Resolution"
        rec_resolution = "1920x1080 (66% Resolution Scale)"

    specs = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "hostname": platform.node(),
        "tier": {
            "name": tier_name,
            "badge": tier_badge,
            "recommended_ue_scalability": rec_ue_preset,
            "recommended_ue_preset_label": rec_ue_preset_label,
            "recommended_resolution": rec_resolution,
        },
        "cpu": {
            "name": cpu_name,
            "vendor": cpu_vendor,
            "physical_cores": physical_cores,
            "logical_threads": logical_threads,
            "base_clock_mhz": cpu_mhz,
            "base_clock_ghz": round(cpu_mhz / 1000.0, 2) if cpu_mhz else None,
            "architecture": platform.machine() or "AMD64",
        },
        "gpu": {
            "name": gpu_name,
            "vram_total_mb": vram_total_mb,
            "vram_total_gb": vram_total_gb,
            "vram_free_mb": vram_free_mb,
            "vram_free_gb": vram_free_gb,
            "driver_version": gpu_driver,
            "temperature_c": gpu_temp_c,
            "directx_feature_level": "DirectX 12 Ultimate (SM 6.6 / Hardware Ray Tracing & DLSS)"
            if ("RTX" in gpu_name or "RX 7" in gpu_name or "RX 6" in gpu_name)
            else "DirectX 12 (SM 6.0+)",
        },
        "ram": {
            "total_gb": total_ram_gb,
            "available_gb": avail_ram_gb,
            "used_gb": used_ram_gb,
            "load_percent": ram_load_pct,
        },
        "display": {
            "width": screen_w,
            "height": screen_h,
            "resolution_label": f"{screen_w}x{screen_h}",
            "refresh_rate_hz": refresh_hz,
            "aspect_ratio": "16:9"
            if round(screen_w / max(1, screen_h), 2) == 1.78
            else f"{screen_w}:{screen_h}",
        },
        "os": {
            "product_name": os_product,
            "display_version": os_display_ver,
            "build_number": os_build,
            "motherboard": f"{board_vendor} {board_product}".strip(),
            "bios_version": bios_version,
        },
        "drives": drives,
    }
    _PC_SPECS_MEM_CACHE = specs
    try:
        atomic_write_json(PC_SPECS_CACHE_PATH, specs)
    except Exception:
        pass
    return specs

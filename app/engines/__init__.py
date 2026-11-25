"""Engine adapter registry and shared engine detection entry points."""

from __future__ import annotations

from pathlib import Path

from app.engines.base import EngineAdapter
from app.engines.generic import GenericEngineAdapter, detect_normalized_engine
from app.engines.unity import UnityEngineAdapter
from app.engines.unreal import UnrealEngineAdapter

UNITY_ADAPTER = UnityEngineAdapter()
UNREAL_ADAPTER = UnrealEngineAdapter()
GENERIC_ADAPTER = GenericEngineAdapter()


def get_engine_adapter(engine_key: str) -> EngineAdapter:
    """Return the matching EngineAdapter instance for an engine key."""
    low = (engine_key or "").strip().lower()
    if "unreal" in low:
        return UNREAL_ADAPTER
    if "unity" in low or "il2cpp" in low or "mono" in low:
        return UNITY_ADAPTER
    return GENERIC_ADAPTER


def detect_engine_for_dir(game_dir: Path) -> EngineAdapter:
    """Detect engine on disk and return the corresponding EngineAdapter."""
    if UNREAL_ADAPTER.detect(game_dir):
        return UNREAL_ADAPTER
    if UNITY_ADAPTER.detect(game_dir):
        return UNITY_ADAPTER
    return GENERIC_ADAPTER


__all__ = [
    "EngineAdapter",
    "UnityEngineAdapter",
    "UnrealEngineAdapter",
    "GenericEngineAdapter",
    "UNITY_ADAPTER",
    "UNREAL_ADAPTER",
    "GENERIC_ADAPTER",
    "get_engine_adapter",
    "detect_engine_for_dir",
    "detect_normalized_engine",
]

"""Base EngineAdapter interface for engine-specific detection, configs, and readiness."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class EngineAdapter(ABC):
    """Abstract contract for per-engine detection, fingerprinting, config discovery, and readiness."""

    engine_id: str = "generic"
    display_name: str = "Generic / Native"

    @abstractmethod
    def detect(self, game_dir: Path) -> bool:
        """Return True if *game_dir* matches this engine's on-disk signature."""

    @abstractmethod
    def fingerprint(self, game_dir: Path) -> dict[str, Any]:
        """Inspect engine-specific binaries, metadata, and loader state under *game_dir*."""

    def discover_configs(self, game_dir: Path, **kwargs: Any) -> dict[str, Any]:
        """Locate and parse engine configuration files (override in subclasses)."""
        return {}

    def check_readiness(self, game_dir: Path, info: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return loader/bridge readiness summary and crucial modding steps."""
        return {
            "ready": False,
            "modding_readiness": "External Tooling",
            "crucial_steps": [],
        }

    def get_log_paths(self, game_dir: Path) -> list[Path]:
        """Return existing log file paths produced by this engine or its mod loader."""
        return []

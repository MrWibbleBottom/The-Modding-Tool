"""Unreal Engine asset analysis, INI configuration, and rescan metadata tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.core import audit as core_audit  # noqa: E402
from app.core import config as core_config  # noqa: E402
from app.core.security import SESSION_HEADER, get_session_token  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import library as library_router  # noqa: E402
from app.routers import system as system_router  # noqa: E402


class UnrealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_unreal_asset_analyzer_excludes_bepinex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Game" / "Binaries" / "Win64").mkdir(parents=True, exist_ok=True)
            (root / "Game" / "Content" / "Paks").mkdir(parents=True, exist_ok=True)
            (root / "Game" / "Config").mkdir(parents=True, exist_ok=True)
            (root / "Game" / "Binaries" / "Win64" / "Game-Win64-Shipping.exe").write_bytes(b"MZ")
            (root / "Game" / "Content" / "Paks" / "pakchunk0-Windows.pak").write_bytes(b"PAK0")
            (root / "Game" / "Config" / "DefaultEngine.ini").write_text("[/Script/Engine.Engine]\n", encoding="utf-8")

            r = self.client.post(
                "/api/game/analyze-assets",
                json={"root_path": str(root)},
                headers={SESSION_HEADER: get_session_token()},
            )
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data.get("detected_engine"), "unreal")

            tool_names = set(data.get("tools", {}).keys())
            self.assertIn("UE4SS", tool_names)
            self.assertIn("filesystem-mcp", tool_names)
            self.assertNotIn("bepinex-mcp", tool_names)
            self.assertNotIn("BepInEx (IL2CPP)", tool_names)
            self.assertNotIn("BepInEx (Mono)", tool_names)
            for item in data.get("available_tools", []):
                self.assertTrue(item.get("engine_compatible"))
                self.assertNotIn("bepinex", item.get("name", "").lower())

    def test_unreal_metadata_rescan_and_ini_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            game_dir = tmp_dir / "UnrealSampleGame"
            (game_dir / "SampleProj" / "Binaries" / "Win64").mkdir(parents=True)
            (game_dir / "SampleProj" / "Content" / "Paks").mkdir(parents=True)
            (game_dir / "SampleProj" / "Config").mkdir(parents=True)
            (game_dir / "SampleProj" / "Binaries" / "Win64" / "SampleProj-Win64-Shipping.exe").write_bytes(b"MZ")
            (game_dir / "SampleProj" / "Content" / "Paks" / "pakchunk0-Windows.pak").write_bytes(b"PAK0")

            gus_ini = tmp_dir / "GameUserSettings.ini"
            gus_ini.write_text(
                "[/Script/Engine.GameUserSettings]\n"
                "ResolutionSizeX=1920\n"
                "ResolutionSizeY=1080\n"
                "FullscreenMode=1\n"
                "bUseVSync=False\n"
                "FrameRateLimit=120.000000\n"
                "[ScalabilityGroups]\n"
                "sg.ResolutionQuality=100\n"
                "sg.ViewDistanceQuality=2\n"
                "sg.AntiAliasingQuality=2\n"
                "sg.ShadowQuality=2\n"
                "sg.GlobalIlluminationQuality=2\n"
                "sg.ReflectionQuality=2\n"
                "sg.PostProcessQuality=2\n"
                "sg.TextureQuality=2\n"
                "sg.EffectsQuality=2\n"
                "sg.FoliageQuality=2\n"
                "sg.ShadingQuality=2\n"
                "sg.LandscapeQuality=2\n",
                encoding="utf-8",
            )

            temp_games_store = tmp_dir / "managed_games.json"
            temp_games_store.write_text(
                json.dumps(
                    {
                        "active_game_id": "unreal_sample",
                        "selected_id": "unreal_sample",
                        "games": [
                            {
                                "id": "unreal_sample",
                                "name": "Unreal Sample Game",
                                "title": "Unreal Sample Game",
                                "game_dir": str(game_dir),
                                "runtime": "Unreal_Engine",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            fake_specs = {
                "cpu": {"name": "Test CPU", "physical_cores": 8, "logical_threads": 16},
                "gpu": {"name": "Test GPU", "vram_total_gb": 12.0},
                "ram": {"total_gb": 32.0, "available_gb": 20.0},
                "display": {"width": 2560, "height": 1440, "resolution_label": "2560x1440", "refresh_rate_hz": 144},
                "tier": {"name": "Upper-Mid"},
            }
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"

            with (
                patch.object(core_config, "GAMES_STORE_PATH", temp_games_store),
                patch.object(library_router, "GAMES_STORE_PATH", temp_games_store),
                patch.object(library_router, "scrape_pc_specs", return_value=fake_specs),
                patch.object(system_router, "scrape_pc_specs", return_value=fake_specs),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                r_rescan = self.client.post(
                    "/api/games/rescan",
                    json={"game_id": "unreal_sample"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_rescan.status_code, 200)
                rescan_data = r_rescan.json()
                self.assertEqual(rescan_data.get("status"), "ok")
                cached_info = rescan_data.get("game", {}).get("cached_modding_info", {})
                self.assertTrue(cached_info.get("is_unreal_engine"))

                r_ue = self.client.post(
                    "/api/games/unreal-settings",
                    json={
                        "game_id": "unreal_sample",
                        "custom_ini_path": str(gus_ini),
                        "reference_tier_id": "tier_mid_8gb_3000_4060",
                    },
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_ue.status_code, 200)
                ue_data = r_ue.json()
                self.assertEqual(ue_data.get("status"), "ok")
                ue_settings = ue_data.get("unreal_settings", {})
                self.assertTrue(ue_settings.get("ini_found"))
                self.assertEqual(
                    ue_settings.get("summary_metrics", {}).get("average_scalability_label"),
                    "High (2)",
                )


if __name__ == "__main__":
    unittest.main()

"""BepInEx MCP bridge path discovery and multi-router integration tests."""

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
from app.routers import asset_audit  # noqa: E402
from app.routers import branding as branding_router  # noqa: E402
from app.routers import library as library_router  # noqa: E402
from app.routers import mcp_bridge as mcp_router  # noqa: E402


class McpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_bepinex_bridge_discovery_paths(self) -> None:
        self.assertEqual(
            Path(core_config.DEFAULT_MCP_APP).parts[-4:],
            ("bridges", "bepinex-mcp", "mcp", "ModdersHelperApp.py"),
        )
        self.assertTrue(Path(core_config.DEFAULT_MCP_APP).is_file())
        self.assertEqual(
            Path(core_config.DEFAULT_VENV_PYTHON),
            core_config.WORKSPACE_DIR / ".venv" / "Scripts" / "python.exe",
        )
        roots = core_config.get_bepinex_bridge_roots()
        self.assertEqual(roots[0], core_config.WORKSPACE_DIR / "bridges" / "bepinex-mcp")
        self.assertEqual(roots[-1], core_config.TOOLS_ROOT / "bepinex-mcp")

        with tempfile.TemporaryDirectory() as tmp:
            fake_ws = Path(tmp) / "ws"
            fake_tools = Path(tmp) / "tools"
            fake_game = Path(tmp) / "game"
            fake_ws.mkdir()
            fake_game.mkdir()
            fallback_mcp = fake_tools / "bepinex-mcp" / "mcp" / "ModdersHelperApp.py"
            fallback_mcp.parent.mkdir(parents=True)
            fallback_mcp.write_text("# stub", encoding="utf-8")

            with (
                patch.object(asset_audit, "WORKSPACE_DIR", fake_ws),
                patch.object(asset_audit, "RESOURCE_ROOT", fake_ws),
                patch.object(asset_audit, "TOOLS_ROOT", fake_tools),
                patch.object(asset_audit, "DEFAULT_MCP_APP", str(fake_ws / "missing.py")),
                patch.object(core_config, "WORKSPACE_DIR", fake_ws),
                patch.object(core_config, "RESOURCE_ROOT", fake_ws),
                patch.object(core_config, "TOOLS_ROOT", fake_tools),
            ):
                tools, _ = asset_audit._check_local_modding_tools(fake_game, "unity_il2cpp")
                self.assertTrue(tools["bepinex-mcp"]["host_available"])

    def test_unity_bridge_discovery_and_all_router_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"
            temp_ui_cfg = tmp_dir / "app_ui_config.json"
            temp_ui_settings = tmp_dir / "ui_settings.json"
            temp_built_mods = tmp_dir / "built_mods"
            temp_games_store = tmp_dir / "managed_games.json"

            unity_game = tmp_dir / "UnityIl2CppSample"
            (unity_game / "UnitySample_Data" / "il2cpp_data" / "Metadata").mkdir(parents=True)
            (unity_game / "GameAssembly.dll").write_bytes(b"MZ-IL2CPP")
            (unity_game / "UnitySample_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(b"META")

            ue_game = tmp_dir / "UnrealModSample"
            (ue_game / "Sample" / "Binaries" / "Win64").mkdir(parents=True)
            (ue_game / "Sample" / "Content" / "Paks").mkdir(parents=True)
            (ue_game / "Sample" / "Binaries" / "Win64" / "Sample-Win64-Shipping.exe").write_bytes(b"MZ")
            (ue_game / "Sample" / "Content" / "Paks" / "pakchunk0-Windows.pak").write_bytes(b"PAK0")

            temp_games_store.write_text(
                json.dumps(
                    {
                        "active_game_id": "ue_mod_sample",
                        "selected_id": "ue_mod_sample",
                        "games": [
                            {
                                "id": "ue_mod_sample",
                                "name": "Unreal Mod Sample",
                                "title": "Unreal Mod Sample",
                                "game_dir": str(ue_game),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
                patch.object(branding_router, "APP_UI_CONFIG_PATH", temp_ui_cfg),
                patch.object(branding_router, "UI_SETTINGS_PATH", temp_ui_settings),
                patch.object(mcp_router, "BUILT_MODS_DIR", temp_built_mods),
                patch.object(mcp_router, "GAMES_STORE_PATH", temp_games_store),
                patch.object(core_config, "GAMES_STORE_PATH", temp_games_store),
                patch.object(library_router, "GAMES_STORE_PATH", temp_games_store),
            ):
                self.assertEqual(self.client.get("/api/games").status_code, 200)
                self.assertEqual(self.client.get("/api/games/scan-progress").status_code, 200)
                self.assertEqual(self.client.get("/api/library").status_code, 200)

                r_guide = self.client.get("/api/game/moddability-guide")
                self.assertEqual(r_guide.status_code, 200)
                r_assets = self.client.post(
                    "/api/game/analyze-assets",
                    json={"root_path": str(unity_game)},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_assets.status_code, 200)
                assets_data = r_assets.json()
                self.assertEqual(assets_data.get("detected_engine"), "unity_il2cpp")
                self.assertTrue(assets_data.get("tools", {}).get("bepinex-mcp", {}).get("host_available"))

                r_fp = self.client.post(
                    "/api/fingerprint",
                    json={"game_dir": str(unity_game)},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_fp.status_code, 200)
                self.assertTrue(r_fp.json().get("is_unity_il2cpp"))

                r_build = self.client.post(
                    "/api/mods/build",
                    json={"game_id": "ue_mod_sample", "game_dir": str(ue_game), "mod_name": "Smoke Unreal Mod"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_build.status_code, 200)
                self.assertEqual(r_build.json().get("status"), "ok")

                self.assertEqual(self.client.get("/api/app/backups").status_code, 200)
                self.assertEqual(self.client.get("/api/app/repo-config").status_code, 200)
                self.assertEqual(self.client.get("/api/app/git-status").status_code, 200)

                self.assertEqual(self.client.get("/api/app-ui-config").status_code, 200)
                r_ui_post = self.client.post(
                    "/api/app-ui-config",
                    json={"app_name": "The Modding Tool"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_ui_post.status_code, 200)
                self.assertEqual(self.client.get("/api/ui/banner/settings").status_code, 200)
                r_ban_post = self.client.post(
                    "/api/ui/banner/settings",
                    json={"banner_enabled": True, "banner_fit": "contain"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_ban_post.status_code, 200)

                self.assertEqual(self.client.get("/api/defaults").status_code, 200)
                self.assertEqual(self.client.get("/api/app/diagnostics").status_code, 200)
                r_tools = self.client.get("/api/app/tools/status")
                self.assertEqual(r_tools.status_code, 200)
                tools_by_id = {t["id"]: t for t in r_tools.json().get("tools", [])}
                self.assertIn("bridges\\bepinex-mcp", tools_by_id["bepinex_mcp_plugin"]["path"])
                self.assertEqual(self.client.get("/api/app/dev-mode").status_code, 200)


if __name__ == "__main__":
    unittest.main()

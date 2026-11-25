"""Game discovery, Steam/non-game filtering, metadata loading, and ETag tests."""

from __future__ import annotations

import json
import os
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
import package_windows  # noqa: E402


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_games_etag_and_304(self) -> None:
        r1 = self.client.get("/api/games")
        self.assertEqual(r1.status_code, 200)
        etag = r1.headers.get("etag")
        self.assertTrue(etag)
        r2 = self.client.get("/api/games", headers={"If-None-Match": etag})
        self.assertEqual(r2.status_code, 304)

    def test_conflicts_and_steam_assets(self) -> None:
        r1 = self.client.get("/api/games/battleground/conflicts")
        self.assertEqual(r1.status_code, 200)
        self.assertIn("plugins", r1.json())
        r2 = self.client.get("/api/games/battleground/steam-assets")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("has_assets", r2.json())

    def test_appdata_path_and_legacy_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_app = Path(tmp)
            legacy_dir = local_app / "ModToolPoC"
            legacy_dir.mkdir()
            (legacy_dir / "managed_games.json").write_text('{"games": []}', encoding="utf-8")
            (legacy_dir / "audit.jsonl").write_text('{"action": "legacy"}\n', encoding="utf-8")

            env_patch = {"LOCALAPPDATA": str(local_app)}
            with patch.dict(os.environ, env_patch, clear=False):
                os.environ.pop("MODTOOL_DATA_DIR", None)
                resolved = core_config.get_app_data_root()
                self.assertEqual(resolved, local_app / "TheModdingTool")
                self.assertTrue((resolved / "managed_games.json").is_file())
                self.assertTrue((resolved / "audit.jsonl").is_file())

                (resolved / "managed_games.json").write_text('{"games": ["updated"]}', encoding="utf-8")
                self.assertFalse(core_config.migrate_legacy_appdata(resolved, legacy_dir))
                self.assertIn("updated", (resolved / "managed_games.json").read_text(encoding="utf-8"))

    def test_steam_reference_csv_and_packaging_config(self) -> None:
        self.assertEqual(library_router.STEAM_REF_CSV.parts[-3:], ("app", "data", "steam_games_reference.csv"))
        rows = library_router.load_steam_reference_sheet()
        self.assertGreater(len(rows), 0)

        cmd = package_windows.build_pyinstaller_command()
        self.assertIn("TheModdingTool", cmd)
        self.assertIn("--icon", cmd)
        self.assertTrue(any("app/data" in arg and "steam_games_reference.csv" in arg for arg in cmd))

    def test_non_game_exclusions_and_unreal_editor_ini_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            steam_root = tmp_dir / "SteamRoot"
            steamapps = steam_root / "steamapps"
            common = steamapps / "common"
            (common / "Steamworks Shared").mkdir(parents=True)
            (common / "Steamworks Shared" / "redist.exe").write_bytes(b"MZ")
            (common / "Sample Dedicated Server").mkdir(parents=True)
            (common / "Sample Dedicated Server" / "server.exe").write_bytes(b"MZ")
            (common / "ValidRealGame").mkdir(parents=True)
            (common / "ValidRealGame" / "ValidRealGame.exe").write_bytes(b"MZ")

            (steamapps / "appmanifest_228980.acf").write_text(
                '"AppState"\n{\n\t"appid"\t\t"228980"\n\t"name"\t\t"Steamworks Common Redistributables"\n\t"StateFlags"\t\t"4"\n\t"installdir"\t\t"Steamworks Shared"\n}\n',
                encoding="utf-8",
            )
            (steamapps / "appmanifest_999001.acf").write_text(
                '"AppState"\n{\n\t"appid"\t\t"999001"\n\t"name"\t\t"Sample Dedicated Server"\n\t"StateFlags"\t\t"4"\n\t"installdir"\t\t"Sample Dedicated Server"\n}\n',
                encoding="utf-8",
            )
            (steamapps / "appmanifest_999002.acf").write_text(
                '"AppState"\n{\n\t"appid"\t\t"999002"\n\t"name"\t\t"Valid Real Game"\n\t"StateFlags"\t\t"4"\n\t"installdir"\t\t"ValidRealGame"\n}\n',
                encoding="utf-8",
            )

            with patch.object(library_router, "_locate_steam_root", return_value=steam_root):
                steam_entries = library_router.scan_steam_installed()
            steam_titles = {e["title"] for e in steam_entries}
            self.assertEqual(steam_titles, {"Valid Real Game"})

            self.assertTrue(library_router.is_non_game_entry("228980", "Steamworks Common Redistributables", ""))
            self.assertTrue(library_router.is_non_game_entry(None, "My Game Dedicated Server", ""))

            scan_root = tmp_dir / "ScanLibrary"
            ue_editor_dir = scan_root / "EngineInstall"
            (ue_editor_dir / "Engine" / "Binaries" / "Win64").mkdir(parents=True)
            (ue_editor_dir / "Engine" / "Binaries" / "Win64" / "UnrealEditor.exe").write_bytes(b"MZ")

            ue_named_dir = scan_root / "Unreal Engine"
            (ue_named_dir / "Binaries").mkdir(parents=True)
            (ue_named_dir / "Binaries" / "Tool.exe").write_bytes(b"MZ")

            ded_dir = scan_root / "Palworld Dedicated Server"
            ded_dir.mkdir(parents=True)
            (ded_dir / "PalServer.exe").write_bytes(b"MZ")

            redist_dir = scan_root / "Steamworks Common Redistributables"
            redist_dir.mkdir(parents=True)
            (redist_dir / "helper.exe").write_bytes(b"MZ")

            real_ue_game = scan_root / "ValidUnrealGame"
            (real_ue_game / "ValidProj" / "Binaries" / "Win64").mkdir(parents=True)
            (real_ue_game / "ValidProj" / "Content" / "Paks").mkdir(parents=True)
            (real_ue_game / "ValidProj" / "Binaries" / "Win64" / "ValidProj-Win64-Shipping.exe").write_bytes(b"MZ")
            (real_ue_game / "ValidProj" / "Content" / "Paks" / "pakchunk0-Windows.pak").write_bytes(b"PAK0")

            self.assertTrue(library_router.is_non_game_entry(None, "EngineInstall", ue_editor_dir))
            self.assertFalse(library_router.is_non_game_entry(None, "ValidUnrealGame", real_ue_game))

            temp_games_store = tmp_dir / "managed_games.json"
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"
            with (
                patch.object(core_config, "GAMES_STORE_PATH", temp_games_store),
                patch.object(library_router, "GAMES_STORE_PATH", temp_games_store),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                r_scan = self.client.post(
                    "/api/games/scan",
                    json={"custom_path": str(scan_root), "auto_populate": False},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_scan.status_code, 200)
                scan_data = r_scan.json()
                discovered_dirs = {Path(d["game_dir"]).name for d in scan_data.get("discovered", [])}
                self.assertEqual(discovered_dirs, {"ValidUnrealGame"})

            fake_local = tmp_dir / "LocalAppData"
            ue_editor_ini = fake_local / "UnrealEngine" / "5.4" / "Saved" / "Config" / "WindowsEditor" / "GameUserSettings.ini"
            ue_editor_ini.parent.mkdir(parents=True)
            ue_editor_ini.write_text("[/Script/Engine.GameUserSettings]\nResolutionSizeX=1920\n", encoding="utf-8")

            ue_root_win_ini = fake_local / "UnrealEngine" / "Saved" / "Config" / "Windows" / "GameUserSettings.ini"
            ue_root_win_ini.parent.mkdir(parents=True)
            ue_root_win_ini.write_text("[/Script/Engine.GameUserSettings]\nResolutionSizeX=1920\n", encoding="utf-8")

            game_editor_only_ini = fake_local / "EditorOnlyProj" / "Saved" / "Config" / "WindowsEditor" / "GameUserSettings.ini"
            game_editor_only_ini.parent.mkdir(parents=True)
            game_editor_only_ini.write_text("[/Script/Engine.GameUserSettings]\nResolutionSizeX=1920\n", encoding="utf-8")

            valid_game_ini = fake_local / "DaysGoneClone" / "Saved" / "Config" / "WindowsNoEditor" / "GameUserSettings.ini"
            valid_game_ini.parent.mkdir(parents=True)
            valid_game_ini.write_text("[/Script/Engine.GameUserSettings]\nResolutionSizeX=2560\n", encoding="utf-8")

            with patch.dict(os.environ, {"LOCALAPPDATA": str(fake_local)}, clear=False):
                profiles = library_router.discover_local_unreal_ini_profiles()
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0]["project_name"], "DaysGoneClone")
            self.assertEqual(profiles[0]["platform_folder"], "WindowsNoEditor")

    def test_load_game_switches_full_profile_and_ui_wiring(self) -> None:
        html = self.client.get("/").text
        for marker in (
            "header-quick-load",
            "global-game-select",
            "onGlobalGameSelected()",
            "info-slug-badge",
            "info-engine-badge",
        ):
            self.assertIn(marker, html)

        active_js = self.client.get("/static/js/active_game.js").text
        table_js = self.client.get("/static/js/game_table.js").text
        lib_js = self.client.get("/static/js/library.js").text
        self.assertIn("export function renderActiveGamePanel", active_js)
        self.assertIn("import { ENTITIES, VARIABLES, syncModBuilderWithGame }", active_js)
        self.assertIn("renderActiveGamePanel", lib_js)
        self.assertIn("renderActiveGamePanel(localGame", table_js)
        self.assertIn("renderActiveGamePanel(data.game)", table_js)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            game_a = tmp_dir / "GameA_Unity"
            (game_a / "GameA_Data" / "il2cpp_data" / "Metadata").mkdir(parents=True)
            (game_a / "GameAssembly.dll").write_bytes(b"MZ-IL2CPP")
            (game_a / "GameA_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(b"META")
            (game_a / "GameA.exe").write_bytes(b"MZ")

            game_b = tmp_dir / "GameB_Unreal"
            (game_b / "ProjB" / "Binaries" / "Win64").mkdir(parents=True)
            (game_b / "ProjB" / "Content" / "Paks").mkdir(parents=True)
            (game_b / "ProjB" / "Binaries" / "Win64" / "ProjB-Win64-Shipping.exe").write_bytes(b"MZ")
            (game_b / "ProjB" / "Content" / "Paks" / "pakchunk0-Windows.pak").write_bytes(b"PAK0")

            temp_games_store = tmp_dir / "managed_games.json"
            temp_games_store.write_text(
                json.dumps(
                    {
                        "active_game_id": "game_a",
                        "selected_id": "game_a",
                        "games": [
                            {"id": "game_a", "name": "Alpha Unity Game", "game_dir": str(game_a), "runtime": "Unity_IL2CPP"},
                            {"id": "game_b", "name": "Beta Unreal Game", "game_dir": str(game_b), "runtime": "Unreal Engine 4/5"},
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
                core_config.invalidate_games_cache()
                r_a = self.client.post(
                    "/api/games/select",
                    json={"game_id": "game_a"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_a.status_code, 200)
                da = r_a.json()
                self.assertEqual(da.get("active_game_id"), "game_a")
                self.assertEqual(da.get("game", {}).get("detected_engine"), "unity_il2cpp")
                self.assertTrue(da.get("game", {}).get("metadata_scraped"))

                r_b = self.client.post(
                    "/api/games/load",
                    json={"game_id": "game_b"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_b.status_code, 200)
                db = r_b.json()
                self.assertEqual(db.get("active_game_id"), "game_b")
                gb = db.get("game", {})
                self.assertEqual(gb.get("id"), "game_b")
                self.assertEqual(gb.get("name"), "Beta Unreal Game")
                self.assertEqual(gb.get("game_dir"), str(game_b))
                self.assertTrue(gb.get("metadata_scraped"))
                self.assertEqual(gb.get("detected_engine"), "unreal")
                self.assertTrue(gb.get("modding_info", {}).get("is_unreal_engine"))
                self.assertGreaterEqual(gb.get("modding_info", {}).get("pak_count", 0), 1)

                r_list = self.client.get("/api/games")
                self.assertEqual(r_list.status_code, 200)
                self.assertEqual(r_list.json().get("active_game_id"), "game_b")
                core_config.invalidate_games_cache()


if __name__ == "__main__":
    unittest.main()

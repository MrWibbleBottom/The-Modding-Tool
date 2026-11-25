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
from app.services import endpoint_applier  # noqa: E402
from app.services import endpoints as endpoints_service  # noqa: E402
from app.services import game_snapshots  # noqa: E402


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

    def test_unreal_ini_fenced_tweaks_preview_and_snapshot_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ue_game = tmp_dir / "UnrealFencedSample"
            cfg_dir = ue_game / "Saved" / "Config" / "WindowsNoEditor"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            eng_ini = cfg_dir / "Engine.ini"
            gus_ini = cfg_dir / "GameUserSettings.ini"
            user_eng_line = "; CustomUserEngineKeep=1\n[/Script/Engine.Engine]\nbSmoothFrameRate=False\n"
            user_gus_line = "; CustomUserGusKeep=1\n[/Script/Engine.GameUserSettings]\nResolutionSizeX=2560\nResolutionSizeY=1440\n"
            eng_ini.write_text(user_eng_line, encoding="utf-8")
            gus_ini.write_text(user_gus_line, encoding="utf-8")

            snaps_root = tmp_dir / "game_snapshots"
            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "unreal_fenced_sample",
                        "name": "Unreal Fenced Sample",
                        "game_dir": str(ue_game),
                        "runtime": "Unreal Engine",
                    }]
                }),
                encoding="utf-8",
            )
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                r_list = self.client.get(
                    f"/api/games/unreal_fenced_sample/unreal-ini/tweaks?gus_ini_path={gus_ini}&engine_ini_path={eng_ini}"
                )
                self.assertEqual(r_list.status_code, 200)
                self.assertGreaterEqual(len(r_list.json().get("presets", [])), 5)

                payload = {
                    "tweak_states": {"ue_disable_motion_blur": True, "gus_disable_vsync": True},
                    "engine_ini_path": str(eng_ini),
                    "gus_ini_path": str(gus_ini),
                    "label": "Pre-tweak snapshot",
                }
                r_prev = self.client.post(
                    "/api/games/unreal_fenced_sample/unreal-ini/preview",
                    json=payload,
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_prev.status_code, 200)
                prev_data = r_prev.json()
                self.assertTrue(prev_data.get("has_changes"))
                self.assertIn("; [TheModdingTool:ue_disable_motion_blur:start]", prev_data.get("unified_diff", ""))
                self.assertIn("+bUseVSync=False", prev_data.get("unified_diff", ""))
                # Preview must not mutate files on disk
                self.assertEqual(eng_ini.read_text(encoding="utf-8"), user_eng_line)

                r_apply = self.client.post(
                    "/api/games/unreal_fenced_sample/unreal-ini/apply",
                    json=payload,
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_apply.status_code, 200)
                apply_data = r_apply.json()
                self.assertTrue(apply_data.get("snapshot_id"))
                snaps = self.client.get("/api/games/unreal_fenced_sample/snapshots").json().get("snapshots", [])
                self.assertTrue(any(s.get("snapshot_id") == apply_data["snapshot_id"] for s in snaps))

                eng_after = eng_ini.read_text(encoding="utf-8")
                gus_after = gus_ini.read_text(encoding="utf-8")
                self.assertIn("; CustomUserEngineKeep=1", eng_after)
                self.assertIn("; [TheModdingTool:ue_disable_motion_blur:start]", eng_after)
                self.assertIn("r.MotionBlurQuality=0", eng_after)
                self.assertIn("; CustomUserGusKeep=1", gus_after)
                self.assertIn("; [TheModdingTool:gus_disable_vsync:start]", gus_after)

                # Toggle tweak off without disturbing user lines
                r_off = self.client.post(
                    "/api/games/unreal_fenced_sample/unreal-ini/apply",
                    json={
                        "tweak_states": {"ue_disable_motion_blur": False},
                        "engine_ini_path": str(eng_ini),
                        "gus_ini_path": str(gus_ini),
                    },
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_off.status_code, 200)
                eng_cleaned = eng_ini.read_text(encoding="utf-8")
                self.assertIn("; CustomUserEngineKeep=1", eng_cleaned)
                self.assertIn("bSmoothFrameRate=False", eng_cleaned)
                self.assertNotIn("TheModdingTool:ue_disable_motion_blur", eng_cleaned)

    def test_unreal_endpoints_categorization_defaults_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ue_dir = tmp_dir / "UnrealEndpointSample"
            cfg_dir = ue_dir / "Saved" / "Config" / "WindowsNoEditor"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            gus_ini = cfg_dir / "GameUserSettings.ini"
            eng_ini = cfg_dir / "Engine.ini"
            scal_ini = cfg_dir / "Scalability.ini"
            gus_ini.write_text(
                "[/Script/Engine.GameUserSettings]\nFrameRateLimit=60.000000\nbUseVSync=True\n"
                "[ScalabilityGroups]\nsg.ViewDistanceQuality=3\n",
                encoding="utf-8",
            )
            eng_ini.write_text("[SystemSettings]\nr.ViewDistanceScale=1.0\n", encoding="utf-8")
            scal_ini.write_text("[FoliageQuality@3]\nfoliage.DensityScale=1.0\n", encoding="utf-8")

            lua_dir = ue_dir / "Proj" / "Binaries" / "Win64" / "Mods" / "GameplayCheatsMod" / "Scripts"
            lua_dir.mkdir(parents=True, exist_ok=True)
            (lua_dir.parent / "enabled.txt").write_text("", encoding="utf-8")
            (lua_dir / "main.lua").write_text(
                'RegisterHook("/Script/Engine.PlayerController:RevealMapObjects", function() end)\n',
                encoding="utf-8",
            )
            (ue_dir / "Saved" / "Logs").mkdir(parents=True, exist_ok=True)
            (ue_dir / "Saved" / "Logs" / "UnrealEndpointSample.log").write_text(
                "LogScript: Error: Sample Unreal endpoint error\n",
                encoding="utf-8",
            )

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "unreal_ep_sample",
                        "name": "Unreal Endpoint Sample",
                        "game_dir": str(ue_dir),
                        "runtime": "Unreal Engine 4/5",
                    }]
                }),
                encoding="utf-8",
            )
            appdata_root = tmp_dir / "appdata"
            snaps_root = tmp_dir / "game_snapshots"
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                core_config.invalidate_games_cache()
                r_scan = self.client.post(
                    "/api/games/unreal_ep_sample/endpoints/scan",
                    json={},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_scan.status_code, 200)
                sdata = r_scan.json()
                self.assertEqual([c["id"] for c in sdata["categories"]], [
                    "game_systems", "player_character", "engine_settings", "cheats", "qol", "error_logs"
                ])
                defaults_file = appdata_root / "game_defaults" / "unreal_ep_sample.json"
                self.assertTrue(defaults_file.is_file())

                eps = {e["id"]: e for e in sdata["endpoints"]}
                vd_ep = eps["ue_ini:GameUserSettings.ini:ScalabilityGroups:sg.ViewDistanceQuality"]
                for required_key in ("default_value", "current_value", "is_modified", "delivery_type", "simple_description", "advanced_details"):
                    self.assertIn(required_key, vd_ep)
                self.assertEqual(vd_ep["category"], "engine_settings")
                self.assertEqual(vd_ep["delivery_type"], "Config")
                self.assertEqual(vd_ep["default_value"], "3")
                self.assertTrue(any(e["category"] == "cheats" and e["delivery_type"] == "Mod File" for e in sdata["endpoints"]))
                self.assertTrue(any(e["category"] == "error_logs" for e in sdata["endpoints"]))

                # Modify sg.ViewDistanceQuality to 1 and verify stored default stays 3 across re-scans
                r_upd = self.client.post(
                    "/api/games/unreal_ep_sample/endpoints/update",
                    json={"endpoint_id": vd_ep["id"], "value": "1", "reset_to_default": False},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_upd.status_code, 200)
                self.assertEqual(r_upd.json()["endpoint"]["current_value"], "1")
                self.assertEqual(r_upd.json()["endpoint"]["default_value"], "3")
                self.assertTrue(r_upd.json()["endpoint"]["is_modified"])

                r_rescan = self.client.get("/api/games/unreal_ep_sample/endpoints?category=engine_settings")
                self.assertEqual(r_rescan.status_code, 200)
                rescanned_vd = next(e for e in r_rescan.json()["endpoints"] if e["id"] == vd_ep["id"])
                self.assertEqual(rescanned_vd["default_value"], "3")
                self.assertEqual(rescanned_vd["current_value"], "1")

                # Reset single setting back to stored default
                r_reset = self.client.post(
                    "/api/games/unreal_ep_sample/endpoints/update",
                    json={"endpoint_id": vd_ep["id"], "reset_to_default": True},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_reset.status_code, 200)
                self.assertEqual(r_reset.json()["endpoint"]["current_value"], "3")
                self.assertFalse(r_reset.json()["endpoint"]["is_modified"])
                self.assertIn("sg.ViewDistanceQuality=3", gus_ini.read_text(encoding="utf-8"))
                core_config.invalidate_games_cache()

    def test_unreal_contextual_apply_validate_mod_file_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ue_dir = tmp_dir / "UnrealApplySample"
            cfg_dir = ue_dir / "Saved" / "Config" / "WindowsNoEditor"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            gus_ini = cfg_dir / "GameUserSettings.ini"
            gus_ini.write_text("[ScalabilityGroups]\nsg.ViewDistanceQuality=3\n", encoding="utf-8")

            lua_dir = ue_dir / "Proj" / "Binaries" / "Win64" / "Mods" / "LootBoostMod" / "Scripts"
            lua_dir.mkdir(parents=True, exist_ok=True)
            (lua_dir / "main.lua").write_text(
                'RegisterHook("/Script/Engine.PlayerController:BoostLootDrops", function() end)\n',
                encoding="utf-8",
            )
            logs_dir = ue_dir / "Saved" / "Logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file = logs_dir / "UnrealApplySample.log"
            log_file.write_text("LogInit: Display: Ready\n", encoding="utf-8")

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "unreal_apply_sample",
                        "name": "Unreal Apply Sample",
                        "game_dir": str(ue_dir),
                        "runtime": "Unreal Engine 4/5",
                    }]
                }),
                encoding="utf-8",
            )
            appdata_root = tmp_dir / "appdata"
            snaps_root = tmp_dir / "game_snapshots"
            built_root = tmp_dir / "built_mods"
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoint_applier, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoint_applier, "BUILT_MODS_DIR", built_root),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                core_config.invalidate_games_cache()
                # 1. Config delivery + persistence validation
                cfg_ep_id = "ue_ini:GameUserSettings.ini:ScalabilityGroups:sg.ViewDistanceQuality"
                r_cfg = self.client.post(
                    "/api/games/unreal_apply_sample/endpoints/apply",
                    json={"endpoint_id": cfg_ep_id, "new_value": "2", "action": "apply"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_cfg.status_code, 200)
                cdata = r_cfg.json()
                self.assertTrue(cdata["ok"])
                self.assertEqual(cdata["delivery_type"], "config")
                self.assertTrue(cdata["validated"])
                self.assertTrue(cdata["restart_required"])
                self.assertTrue(cdata["snapshot_id"])

                r_val = self.client.post(
                    "/api/games/unreal_apply_sample/endpoints/validate",
                    json={"endpoint_id": cfg_ep_id, "expected_value": "2"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_val.status_code, 200)
                self.assertTrue(r_val.json()["validated"])

                # 2. Mod File deploy + downloadable zip + per-mod rollback
                mod_ep_id = "ue4ss_hook:LootBoostMod:1"
                r_dep = self.client.post(
                    "/api/games/unreal_apply_sample/endpoints/apply",
                    json={"endpoint_id": mod_ep_id, "new_value": "Enabled", "action": "deploy_mod"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_dep.status_code, 200)
                ddata = r_dep.json()
                self.assertTrue(ddata["ok"])
                self.assertEqual(ddata["delivery_type"], "mod_file")
                self.assertTrue(ddata["validated"])
                self.assertTrue((lua_dir.parent / "enabled.txt").is_file())
                r_dl = self.client.get(ddata["download_url"])
                self.assertEqual(r_dl.status_code, 200)
                self.assertIn("application/zip", r_dl.headers.get("content-type", ""))

                r_rb = self.client.post(
                    "/api/games/unreal_apply_sample/endpoints/apply",
                    json={"endpoint_id": mod_ep_id, "action": "rollback_mod"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_rb.status_code, 200)
                self.assertTrue(r_rb.json()["ok"])
                self.assertFalse((lua_dir.parent / "enabled.txt").exists())

                # 3. Failure log surfacing when INI error appears in game log
                log_file.write_text("LogConfig: Error: Invalid GameUserSettings.ini key sg.ViewDistanceQuality\n", encoding="utf-8")
                r_fail = self.client.post(
                    "/api/games/unreal_apply_sample/endpoints/apply",
                    json={"endpoint_id": cfg_ep_id, "new_value": "1", "action": "apply"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_fail.status_code, 200)
                fdata = r_fail.json()
                self.assertFalse(fdata["ok"])
                self.assertFalse(fdata["validated"])
                self.assertIn("GameUserSettings.ini", fdata["log_excerpt"])
                core_config.invalidate_games_cache()

    def test_unreal_offline_reference_dict_structural_and_uncategorized_fallback(self) -> None:
        from app.services import endpoint_parsers
        import package_windows

        cmd = package_windows.build_pyinstaller_command()
        self.assertTrue(any("app/data" in arg and "engine_endpoints_reference.json" in arg for arg in cmd))
        ref_hit = endpoint_parsers.lookup_engine_reference(["foliage.DensityScale"], engine="unreal")
        self.assertIsNotNone(ref_hit)
        self.assertEqual(ref_hit["source"], "reference_dict")
        self.assertEqual(ref_hit["category"], "engine_settings")
        wildcard_hit = endpoint_parsers.lookup_engine_reference(["sg.ReflectionQuality"], engine="unreal")
        self.assertIsNotNone(wildcard_hit)
        self.assertEqual(wildcard_hit["pattern"], "sg.*")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ue_dir = tmp_dir / "UnrealParserGame"
            cfg_dir = ue_dir / "Saved" / "Config" / "WindowsNoEditor"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "Engine.ini").write_text(
                "\n".join([
                    "[SystemSettings]",
                    "r.ViewDistanceScale=1.5",
                    "foliage.DensityScale=0.8",
                    "[/Script/Engine.RendererSettings]",
                    "r.MotionBlurQuality=0",
                    "[/Script/Engine.InputSettings]",
                    "bEnableMouseSmoothing=False",
                    "[GameBalance]",
                    "bGodMode=True",
                    "fDamageMultiplier=2.5",
                    "iMaxAmmo=99",
                    "[CustomOpaqueModule]",
                    "XyzzyOpaqueCustomFlag=1",
                ]) + "\n",
                encoding="utf-8",
            )
            dt_dir = ue_dir / "UnrealParserGame" / "Content" / "Data"
            dt_dir.mkdir(parents=True, exist_ok=True)
            (dt_dir / "DT_Weapons.uasset").write_bytes(b"UASSET")
            (dt_dir / "DT_PlayerStats.uasset").write_bytes(b"UASSET")
            (dt_dir / "DT_Quests.uasset").write_bytes(b"UASSET")

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "unreal_parser_sample",
                        "name": "Unreal Parser Sample",
                        "game_dir": str(ue_dir),
                        "runtime": "Unreal Engine 4/5",
                    }]
                }),
                encoding="utf-8",
            )
            appdata_root = tmp_dir / "appdata"
            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
            ):
                core_config.invalidate_games_cache()
                r_scan = self.client.post(
                    "/api/games/unreal_parser_sample/endpoints/scan",
                    json={},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_scan.status_code, 200)
                eps = {e["id"]: e for e in r_scan.json()["endpoints"]}

                vd = eps["ue_ini:Engine.ini:SystemSettings:r.ViewDistanceScale"]
                self.assertEqual(vd["category"], "engine_settings")
                self.assertEqual(vd["classification_source"], "reference_dict")
                self.assertEqual(vd["confidence"], "high")
                self.assertEqual(vd["min"], 0.5)
                self.assertEqual(vd["max"], 3.0)

                mb = eps["ue_ini:Engine.ini:/Script/Engine.RendererSettings:r.MotionBlurQuality"]
                self.assertEqual(mb["category"], "qol")
                self.assertEqual(mb["classification_source"], "reference_dict")

                ms = eps["ue_ini:Engine.ini:/Script/Engine.InputSettings:bEnableMouseSmoothing"]
                self.assertEqual(ms["category"], "qol")
                self.assertEqual(ms["classification_source"], "reference_dict")

                god = eps["ue_ini:Engine.ini:GameBalance:bGodMode"]
                self.assertEqual(god["category"], "cheats")
                self.assertEqual(god["value_type"], "bool")
                self.assertEqual(god["classification_source"], "unreal_metadata")

                dmg = eps["ue_ini:Engine.ini:GameBalance:fDamageMultiplier"]
                self.assertEqual(dmg["category"], "game_systems")
                self.assertEqual(dmg["value_type"], "number")
                self.assertEqual(dmg["classification_source"], "unreal_metadata")

                ammo = eps["ue_ini:Engine.ini:GameBalance:iMaxAmmo"]
                self.assertEqual(ammo["category"], "game_systems")
                self.assertEqual(ammo["value_type"], "number")

                self.assertEqual(eps["ue_datatable:DT_Weapons"]["category"], "game_systems")
                self.assertEqual(eps["ue_datatable:DT_Weapons"]["classification_source"], "unreal_metadata")
                self.assertEqual(eps["ue_datatable:DT_PlayerStats"]["category"], "player_character")
                self.assertEqual(eps["ue_datatable:DT_Quests"]["category"], "cheats")

                unk = eps["ue_ini:Engine.ini:CustomOpaqueModule:XyzzyOpaqueCustomFlag"]
                self.assertEqual(unk["category"], "uncategorized")
                self.assertEqual(unk["category_label"], "Uncategorized")
                self.assertEqual(unk["confidence"], "low")
                core_config.invalidate_games_cache()


if __name__ == "__main__":
    unittest.main()

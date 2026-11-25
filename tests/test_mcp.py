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
from app.services import endpoint_applier  # noqa: E402
from app.services import endpoints as endpoints_service  # noqa: E402
from app.services import game_snapshots  # noqa: E402


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

    def test_log_tail_and_ai_context_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            unity_game = tmp_dir / "UnityLogSample"
            (unity_game / "BepInEx" / "plugins" / "DemoMod").mkdir(parents=True, exist_ok=True)
            (unity_game / "BepInEx" / "interop").mkdir(parents=True, exist_ok=True)
            (unity_game / "BepInEx" / "config").mkdir(parents=True, exist_ok=True)
            (unity_game / "BepInEx" / "plugins" / "DemoMod" / "DemoMod.dll").write_bytes(b"MZ")
            (unity_game / "GameAssembly.dll").write_bytes(b"MZ")
            (unity_game / "UnityPlayer.dll").write_bytes(b"MZ")
            (unity_game / "winhttp.dll").write_bytes(b"MZ")
            (unity_game / "BepInEx" / "LogOutput.log").write_text(
                "\n".join([
                    "[Info   :   BepInEx] BepInEx 6.0.0 started",
                    "[Message:   BepInEx] Loading DemoMod.dll",
                    "[Error  :   DemoMod] NullReferenceException: Object reference not set",
                    "  at DemoMod.Plugin.Load () [0x00012] in <abc>:0",
                    "[Info   :   BepInEx] Startup complete",
                ]),
                encoding="utf-8",
            )

            ue_game = tmp_dir / "UnrealLogSample"
            (ue_game / "Saved" / "Logs").mkdir(parents=True, exist_ok=True)
            (ue_game / "Saved" / "Logs" / "UnrealLogSample.log").write_text(
                "LogInit: Display: Engine ready\nLogScript: Error: Missing actor binding\n",
                encoding="utf-8",
            )

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [
                        {
                            "id": "unity_log_sample",
                            "name": "Unity Log Sample",
                            "game_dir": str(unity_game),
                            "runtime": "Unity IL2CPP",
                        },
                        {
                            "id": "unreal_log_sample",
                            "name": "Unreal Log Sample",
                            "game_dir": str(ue_game),
                            "runtime": "Unreal Engine",
                        },
                    ]
                }),
                encoding="utf-8",
            )
            snaps_root = tmp_dir / "game_snapshots"

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(mcp_router, "GAMES_STORE_PATH", store_path),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
            ):
                r_err = self.client.get("/api/games/unity_log_sample/log-tail?lines=100&errors_only=true")
                self.assertEqual(r_err.status_code, 200)
                err_data = r_err.json()
                self.assertEqual(err_data.get("error_count"), 1)
                self.assertIn("NullReferenceException", err_data.get("text", ""))
                self.assertIn("at DemoMod.Plugin.Load", err_data.get("text", ""))
                self.assertNotIn("BepInEx 6.0.0 started", err_data.get("text", ""))

                r_ue_tail = self.client.get("/api/games/unreal_log_sample/log-tail?lines=50&errors_only=false")
                self.assertEqual(r_ue_tail.status_code, 200)
                self.assertIn("Missing actor binding", r_ue_tail.json().get("text", ""))

                r_ctx = self.client.get("/api/games/unity_log_sample/ai-context")
                self.assertEqual(r_ctx.status_code, 200)
                md = r_ctx.json().get("markdown", "")
                for heading in (
                    "## Game & Runtime",
                    "## MCP Bridge & Tooling Paths",
                    "## Dump & Config Paths",
                    "## Installed Plugins / Mods",
                    "## Safety, Conflicts & Snapshots",
                    "## Last 30 Log Lines",
                ):
                    self.assertIn(heading, md)
                self.assertIn("DemoMod.dll", md)
                self.assertIn("NullReferenceException", md)

    def test_unity_endpoints_cfg_bridge_defaults_and_simple_advanced_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            unity_dir = tmp_dir / "UnityEndpointSample"
            (unity_dir / "BepInEx" / "config").mkdir(parents=True, exist_ok=True)
            (unity_dir / "BepInEx" / "interop").mkdir(parents=True, exist_ok=True)
            (unity_dir / "GameAssembly.dll").write_bytes(b"MZ")
            (unity_dir / "BepInEx" / "interop" / "Jade.GameView.dll").write_bytes(b"DLL")
            (unity_dir / "BepInEx" / "LogOutput.log").write_text("[Error : Demo] Missing method\n", encoding="utf-8")

            cfg_file = unity_dir / "BepInEx" / "config" / "com.modtool.balance.cfg"
            cfg_file.write_text(
                "\n".join([
                    "[HeroStats]",
                    "## Sets maximum hero health points",
                    "# Setting type: Int32",
                    "# Default value: 22",
                    "HeroMaxHealth = 30",
                    "",
                    "[Gameplay]",
                    "## Multiplier for player attack damage",
                    "# Setting type: Single",
                    "# Default value: 1.0",
                    "PlayerDamageScale = 1.0",
                ]) + "\n",
                encoding="utf-8",
            )

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "unity_ep_sample",
                        "name": "Unity Endpoint Sample",
                        "game_dir": str(unity_dir),
                        "runtime": "Unity_IL2CPP",
                    }]
                }),
                encoding="utf-8",
            )
            appdata_root = tmp_dir / "appdata"
            snaps_root = tmp_dir / "game_snapshots"
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"

            fake_live_bridge = [
                {
                    "id": "live_mcp:CameraTargetCharacter:CurrencyGold",
                    "category": "player_character",
                    "name": "Party Gold (Live Bridge)",
                    "namespace": "Jade.GameModel",
                    "class_name": "PartyInventory",
                    "member": "GoldAmount",
                    "signature": "Jade.GameModel.PartyInventory.GoldAmount",
                    "current_value": "500",
                    "default_value": "100",
                    "description": "Live party currency count via BepInExMCP.",
                }
            ]

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoints_service, "_probe_live_bepinex_bridge", return_value=fake_live_bridge),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                core_config.invalidate_games_cache()
                r_scan = self.client.post(
                    "/api/games/unity_ep_sample/endpoints/scan",
                    json={},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_scan.status_code, 200)
                sdata = r_scan.json()
                self.assertEqual(sdata["engine"], "unity")
                self.assertTrue((appdata_root / "game_defaults" / "unity_ep_sample.json").is_file())

                eps = {e["id"]: e for e in sdata["endpoints"]}
                hp_ep = eps["bep_cfg:com.modtool.balance.cfg:HeroStats:HeroMaxHealth"]
                self.assertEqual(hp_ep["category"], "player_character")
                self.assertEqual(hp_ep["default_value"], "22")
                self.assertEqual(hp_ep["current_value"], "30")
                self.assertTrue(hp_ep["is_modified"])
                self.assertEqual(hp_ep["delivery_type"], "Config")
                self.assertIn("maximum hero health", hp_ep["simple_description"].lower())
                self.assertEqual(hp_ep["advanced_details"]["section"], "HeroStats")
                self.assertEqual(hp_ep["advanced_details"]["key"], "HeroMaxHealth")

                live_ep = eps["live_mcp:CameraTargetCharacter:CurrencyGold"]
                self.assertEqual(live_ep["delivery_type"], "Bridge")
                self.assertEqual(live_ep["advanced_details"]["namespace"], "Jade.GameModel")
                self.assertEqual(live_ep["advanced_details"]["class_name"], "PartyInventory")
                self.assertEqual(live_ep["advanced_details"]["method_signature"], "Jade.GameModel.PartyInventory.GoldAmount")

                # Reset BepInEx .cfg setting back to stored default (22)
                r_reset = self.client.post(
                    "/api/games/unity_ep_sample/endpoints/update",
                    json={"endpoint_id": hp_ep["id"], "reset_to_default": True},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_reset.status_code, 200)
                self.assertEqual(r_reset.json()["endpoint"]["current_value"], "22")
                self.assertFalse(r_reset.json()["endpoint"]["is_modified"])
                self.assertIn("HeroMaxHealth = 22", cfg_file.read_text(encoding="utf-8"))
                core_config.invalidate_games_cache()

    def test_unity_contextual_bridge_apply_validate_and_failure_surfacing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            unity_dir = tmp_dir / "UnityBridgeApply"
            (unity_dir / "BepInEx" / "config").mkdir(parents=True, exist_ok=True)
            (unity_dir / "BepInEx" / "interop").mkdir(parents=True, exist_ok=True)
            (unity_dir / "GameAssembly.dll").write_bytes(b"MZ")
            (unity_dir / "BepInEx" / "interop" / "Jade.GameView.dll").write_bytes(b"DLL")
            log_file = unity_dir / "BepInEx" / "LogOutput.log"
            log_file.write_text("[Info : BepInEx] Startup complete\n", encoding="utf-8")

            cfg_file = unity_dir / "BepInEx" / "config" / "com.modtool.balance.cfg"
            cfg_file.write_text("[HeroStats]\nHeroMaxHealth = 22\n", encoding="utf-8")

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "unity_bridge_apply",
                        "name": "Unity Bridge Apply",
                        "game_dir": str(unity_dir),
                        "runtime": "Unity_IL2CPP",
                        "default_go_id": "-269924",
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

            live_state = {"val": "22", "fail_mode": False}

            async def fake_bridge(tool_name: str, args: dict, game: dict):
                if tool_name == "set_component_value":
                    if live_state["fail_mode"]:
                        return {"tool": tool_name, "isError": True, "raw": "NullReferenceException in SetValue", "data": {}}
                    live_state["val"] = str(args.get("value", ""))
                    return {"tool": tool_name, "isError": False, "raw": "ok", "data": {"ok": True}}
                if tool_name == "get_component_member":
                    return {"tool": tool_name, "isError": False, "raw": live_state["val"], "data": {"value": live_state["val"]}}
                return {"tool": tool_name, "isError": False, "raw": "{}", "data": {}}

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoint_applier, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoint_applier, "BUILT_MODS_DIR", built_root),
                patch.object(endpoint_applier, "_default_mcp_bridge_call", side_effect=fake_bridge),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                core_config.invalidate_games_cache()
                # 1. Config delivery on .cfg
                cfg_ep = "bep_cfg:com.modtool.balance.cfg:HeroStats:HeroMaxHealth"
                r_cfg = self.client.post(
                    "/api/games/unity_bridge_apply/endpoints/apply",
                    json={"endpoint_id": cfg_ep, "new_value": "35", "action": "apply"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_cfg.status_code, 200)
                self.assertTrue(r_cfg.json()["ok"])
                self.assertTrue(r_cfg.json()["restart_required"])

                # 2. Live Bridge push + get_component_member read-back verification
                bridge_ep = "dump_sig:Jade.GameView.CameraTargetCharacter:targetPiece.stats.Health.maxValue"
                r_push = self.client.post(
                    "/api/games/unity_bridge_apply/endpoints/apply",
                    json={"endpoint_id": bridge_ep, "new_value": "45", "action": "apply"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_push.status_code, 200)
                pdata = r_push.json()
                self.assertTrue(pdata["ok"])
                self.assertEqual(pdata["delivery_type"], "bridge")
                self.assertTrue(pdata["validated"])
                self.assertFalse(pdata["restart_required"])
                self.assertEqual(pdata["readback_value"], "45")
                self.assertEqual(pdata["validation_badge"], "Bridge Confirmed")

                r_val = self.client.post(
                    "/api/games/unity_bridge_apply/endpoints/validate",
                    json={"endpoint_id": bridge_ep, "expected_value": "45"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_val.status_code, 200)
                self.assertTrue(r_val.json()["validated"])

                # 3. Bridge failure + log surfacing
                live_state["fail_mode"] = True
                log_file.write_text("[Error : BepInExMCP] NullReferenceException: CameraTargetCharacter missing\n", encoding="utf-8")
                r_fail = self.client.post(
                    "/api/games/unity_bridge_apply/endpoints/apply",
                    json={"endpoint_id": bridge_ep, "new_value": "99", "action": "apply"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_fail.status_code, 200)
                fdata = r_fail.json()
                self.assertFalse(fdata["ok"])
                self.assertFalse(fdata["validated"])
                self.assertEqual(fdata["validation_badge"], "Validation Failed")
                self.assertIn("NullReferenceException", fdata["log_excerpt"])
                core_config.invalidate_games_cache()

    def test_unity_cfg_doc_ranges_dump_attributes_and_uncategorized_fallback(self) -> None:
        from app.services import endpoint_parsers

        # Direct unit check of Token + Type Heuristic Classifier & Reference Dict
        low_heur = endpoint_parsers.classify_identifier_heuristic("XyzzyFrobnitzInternalSeed")
        self.assertEqual(low_heur["category"], "uncategorized")
        self.assertEqual(low_heur["confidence"], "low")

        ref_fps = endpoint_parsers.lookup_engine_reference(["Application.targetFrameRate"], engine="unity")
        self.assertIsNotNone(ref_fps)
        self.assertEqual(ref_fps["category"], "engine_settings")
        self.assertEqual(ref_fps["source"], "reference_dict")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            unity_dir = tmp_dir / "UnityParserSample"
            (unity_dir / "BepInEx" / "config").mkdir(parents=True, exist_ok=True)
            (unity_dir / "BepInEx" / "interop").mkdir(parents=True, exist_ok=True)
            (unity_dir / "GameAssembly.dll").write_bytes(b"MZ")
            (unity_dir / "BepInEx" / "interop" / "Assembly-CSharp.dll").write_bytes(b"DLL")

            (unity_dir / "BepInEx" / "config" / "BepInEx.cfg").write_text(
                "\n".join([
                    "[Logging.Console]",
                    "## Enables live console debug output",
                    "# Setting type: Boolean",
                    "# Default value: false",
                    "Enabled = true",
                ]) + "\n",
                encoding="utf-8",
            )
            (unity_dir / "BepInEx" / "config" / "com.demo.tweaks.cfg").write_text(
                "\n".join([
                    "[Combat]",
                    "## Scales outgoing melee and spell damage",
                    "# Setting type: Single",
                    "# Default value: 1.0",
                    "# Acceptable value range: From 0.5 to 5.0",
                    "PlayerDamageMultiplier = 1.5",
                    "",
                    "[Visuals]",
                    "## Selects preset particle density",
                    "# Setting type: String",
                    "# Default value: Medium",
                    "# Acceptable values: Low, Medium, High",
                    "ParticlePreset = High",
                    "",
                    "[Internal]",
                    "# Setting type: Int32",
                    "# Default value: 7",
                    "XyzzyOpaqueCounter = 7",
                ]) + "\n",
                encoding="utf-8",
            )

            (unity_dir / "dump.cs").write_text(
                "\n".join([
                    "// Namespace: Game.Combat",
                    "public class WeaponController : MonoBehaviour",
                    "{",
                    '    [Header("Weapon Balance")]',
                    '    [Tooltip("Multiplies projectile damage for all equipped weapons")]',
                    "    [Range(0.25f, 8.0f)]",
                    "    public float ProjectileDamageScale; // 0x20",
                    "}",
                    "// Namespace: Game.Core",
                    "public class InternalState",
                    "{",
                    '    [Tooltip("Opaque diagnostic build tag")]',
                    "    public int ZxQwertyBuildNumber; // 0x10",
                    "}",
                ]) + "\n",
                encoding="utf-8",
            )

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "unity_parser_sample",
                        "name": "Unity Parser Sample",
                        "game_dir": str(unity_dir),
                        "runtime": "Unity_IL2CPP",
                    }]
                }),
                encoding="utf-8",
            )
            appdata_root = tmp_dir / "appdata"
            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoints_service, "_probe_live_bepinex_bridge", return_value=[]),
            ):
                core_config.invalidate_games_cache()
                r_scan = self.client.post(
                    "/api/games/unity_parser_sample/endpoints/scan",
                    json={},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_scan.status_code, 200)
                sdata = r_scan.json()
                self.assertEqual(sdata["uncategorized_count"], 2)
                eps = {e["id"]: e for e in sdata["endpoints"]}

                con_ep = eps["bep_cfg:BepInEx.cfg:Logging.Console:Enabled"]
                self.assertEqual(con_ep["category"], "error_logs")
                self.assertEqual(con_ep["classification_source"], "reference_dict")

                dmg_ep = eps["bep_cfg:com.demo.tweaks.cfg:Combat:PlayerDamageMultiplier"]
                self.assertEqual(dmg_ep["category"], "game_systems")
                self.assertEqual(dmg_ep["classification_source"], "cfg_metadata")
                self.assertEqual(dmg_ep["min"], 0.5)
                self.assertEqual(dmg_ep["max"], 5.0)
                self.assertIn("outgoing melee", dmg_ep["simple_description"].lower())

                vis_ep = eps["bep_cfg:com.demo.tweaks.cfg:Visuals:ParticlePreset"]
                self.assertEqual(vis_ep["value_type"], "select")
                self.assertEqual(vis_ep["options"], ["Low", "Medium", "High"])

                dump_ep = eps["il2cpp_dump:WeaponController:ProjectileDamageScale"]
                self.assertEqual(dump_ep["classification_source"], "unity_dump")
                self.assertEqual(dump_ep["min"], 0.25)
                self.assertEqual(dump_ep["max"], 8)
                self.assertEqual(dump_ep["advanced_details"]["method_signature"], "Game.Combat.WeaponController.ProjectileDamageScale")
                self.assertIn("projectile damage", dump_ep["simple_description"].lower())

                unk_cfg = eps["bep_cfg:com.demo.tweaks.cfg:Internal:XyzzyOpaqueCounter"]
                self.assertEqual(unk_cfg["category"], "uncategorized")
                self.assertEqual(unk_cfg["confidence"], "low")

                unk_dump = eps["il2cpp_dump:InternalState:ZxQwertyBuildNumber"]
                self.assertEqual(unk_dump["category"], "uncategorized")
                self.assertEqual(unk_dump["confidence"], "low")
                core_config.invalidate_games_cache()


if __name__ == "__main__":
    unittest.main()

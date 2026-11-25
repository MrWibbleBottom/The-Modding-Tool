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

                # Internal BepInEx.cfg framework entries are filtered out of game endpoints,
                # while engine_endpoints_reference.json still resolves reference keys directly
                self.assertFalse(any(eid.startswith("bep_cfg:BepInEx.cfg:") for eid in eps))
                ref_log_cls = endpoint_parsers.resolve_endpoint_classification(
                    "Enabled", engine="unity", reference_candidates=["Logging.Console.Enabled", "Enabled"]
                )
                self.assertEqual(ref_log_cls["category"], "error_logs")
                self.assertEqual(ref_log_cls["source"], "reference_dict")

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

    def test_unity_multi_instance_nested_stats_reflection_and_boilerplate_filter(self) -> None:
        from app.services import endpoint_applier

        # 1. Verify runtime reflection discovers multiple party hero instances + 2-level nested containers
        live_values: dict[tuple[str, str], Any] = {
            ("101", "HeroName"): "Fighter",
            ("101", "MaxHealth"): 40,
            ("101", "Stats.Armor"): 12,
            ("101", "State.ActionPoints"): 3,
            ("102", "HeroName"): "Wizard",
            ("102", "MaxHealth"): 24,
            ("102", "Stats.Armor"): 4,
            ("102", "State.ActionPoints"): 4,
        }

        def fake_reflection_call(tool_name: str, params: dict[str, Any] | None = None) -> Any:
            p = params or {}
            if tool_name == "search_runtime_types":
                if p.get("query") == "Hero":
                    return [
                        {"name": "Transform", "fullName": "UnityEngine.Transform", "isComponent": True},
                        {"name": "String", "fullName": "System.String", "isComponent": False},
                        {"name": "TMP_Text", "fullName": "TMPro.TMP_Text", "isComponent": True},
                        {"name": "BaseUnityPlugin", "fullName": "BepInEx.Unity.IL2CPP.BasePlugin", "isComponent": False},
                        {"name": "HeroController", "fullName": "Game.Actors.HeroController", "isComponent": True},
                    ]
                return []
            if tool_name == "describe_runtime_type":
                tn = p.get("typeName")
                if tn == "Game.Actors.HeroController":
                    return {
                        "type": {"fullName": tn},
                        "members": [
                            {"name": "HeroName", "type": "System.String", "readable": True, "writable": True, "isStatic": False},
                            {"name": "MaxHealth", "type": "System.Int32", "readable": True, "writable": True, "isStatic": False},
                            {"name": "Stats", "type": "Game.Actors.HeroStats", "readable": True, "writable": False, "isStatic": False},
                            {"name": "State", "type": "Game.Actors.HeroState", "readable": True, "writable": False, "isStatic": False},
                        ],
                    }
                if tn == "Game.Actors.HeroStats":
                    return {
                        "type": {"fullName": tn},
                        "members": [{"name": "Armor", "type": "System.Int32", "readable": True, "writable": True, "isStatic": False}],
                    }
                if tn == "Game.Actors.HeroState":
                    return {
                        "type": {"fullName": tn},
                        "members": [{"name": "ActionPoints", "type": "System.Int32", "readable": True, "writable": True, "isStatic": False}],
                    }
            if tool_name == "find_objects_with_component" and p.get("componentName") == "Game.Actors.HeroController":
                return [
                    {"id": 101, "name": "HeroUnit_01(Clone)"},
                    {"id": 102, "name": "HeroUnit_02(Clone)"},
                ]
            if tool_name == "get_component_member":
                gid = str(p.get("id") or p.get("game_object_id") or "")
                mn = str(p.get("memberName") or p.get("member_name") or "")
                if (gid, mn) in live_values:
                    return {"value": str(live_values[(gid, mn)])}
            return {}

        reflected = endpoints_service.discover_live_bepinex_via_reflection(fake_reflection_call)
        # UnityEngine/System/TMPro/BepInEx namespaces excluded cleanly
        self.assertFalse(any(x["component"].startswith(("UnityEngine.", "System.", "TMPro.", "BepInEx.")) for x in reflected))
        names_map = {x["name"]: x for x in reflected}
        self.assertIn("Fighter — Max Health", names_map)
        self.assertIn("Wizard — Max Health", names_map)
        self.assertIn("Fighter — Stats.Armor", names_map)
        self.assertIn("Wizard — State.Action Points", names_map)
        self.assertEqual(names_map["Fighter — Stats.Armor"]["object_id"], "101")
        self.assertEqual(names_map["Wizard — State.Action Points"]["object_id"], "102")

        # 2. Verify full /endpoints/scan excludes BepInEx.cfg / BepInExMCP.IL2CPP.cfg & error_logs from total_count,
        # auto-discovers dump.cs under interop/ or tools/, and pushes dotted path Stats.Armor to Fighter (id 101)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            unity_dir = tmp_dir / "RpgUnityGame"
            (unity_dir / "BepInEx" / "config").mkdir(parents=True, exist_ok=True)
            (unity_dir / "GameAssembly.dll").write_bytes(b"MZ")
            (unity_dir / "BepInEx" / "LogOutput.log").write_text("[Error : Demo] Log error sample\n", encoding="utf-8")

            # Boilerplate framework CFGs must NOT count toward gameplay endpoints
            (unity_dir / "BepInEx" / "config" / "BepInEx.cfg").write_text("[Logging.Console]\nEnabled = true\n", encoding="utf-8")
            (unity_dir / "BepInEx" / "config" / "BepInExMCP.IL2CPP.cfg").write_text("[Server]\nPort = 8080\n", encoding="utf-8")

            # Barren scan before dump/bridge should have 0 gameplay endpoints despite log errors & BepInEx.cfg
            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "rpg_unity_game",
                        "name": "RPG Unity Game",
                        "game_dir": str(unity_dir),
                        "runtime": "Unity_IL2CPP",
                    }]
                }),
                encoding="utf-8",
            )
            appdata_root = tmp_dir / "appdata"
            snaps_root = tmp_dir / "game_snapshots"
            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoints_service, "_probe_live_bepinex_bridge", return_value=[]),
            ):
                core_config.invalidate_games_cache()
                r_barren = self.client.post(
                    "/api/games/rpg_unity_game/endpoints/scan",
                    json={},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_barren.status_code, 200)
                bdata = r_barren.json()
                self.assertEqual(bdata["total_count"], 0)
                self.assertEqual(bdata["diagnostics_count"], 2)
                self.assertFalse(any(e["id"].startswith(("bep_cfg:BepInEx.cfg:", "bep_cfg:BepInExMCP.IL2CPP.cfg:")) for e in bdata["endpoints"]))

            # Now place dump.cs inside BepInEx/interop/ (menu scan reflection) & enable reflected live bridge items
            (unity_dir / "BepInEx" / "interop").mkdir(parents=True, exist_ok=True)
            (unity_dir / "BepInEx" / "interop" / "dump.cs").write_text(
                "\n".join([
                    "// Namespace: UnityEngine",
                    "public class MonoBehaviour {}",
                    "// Namespace: Game.Tactical",
                    "public struct HeroStats",
                    "{",
                    "    public int Shield; // 0x10",
                    "    public int Energy; // 0x14",
                    "}",
                ]) + "\n",
                encoding="utf-8",
            )

            pushed_calls: list[tuple[str, dict[str, Any]]] = []

            async def fake_bridge_exec(tool_name: str, arguments: dict[str, Any], game: dict[str, Any]) -> dict[str, Any]:
                pushed_calls.append((tool_name, dict(arguments)))
                gid = str(arguments.get("game_object_id") or "")
                mname = str(arguments.get("member_name") or "")
                if tool_name == "set_component_value":
                    live_values[(gid, mname)] = int(arguments["value"])
                    return {"tool": tool_name, "isError": False, "data": {"ok": True}, "raw": "ok"}
                if tool_name == "get_component_member":
                    if mname == "Stats.Armor":
                        # Test nested container fallback dict traversal too
                        return {"tool": tool_name, "isError": True, "raw": "fallback"}
                    if mname == "Stats":
                        return {"tool": tool_name, "isError": False, "data": {"Stats": {"Armor": live_values[(gid, "Stats.Armor")]}}, "raw": ""}
                return {"tool": tool_name, "isError": True, "raw": "unknown"}

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoints_service, "_probe_live_bepinex_bridge", return_value=reflected),
                patch.object(endpoint_applier, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoint_applier, "_default_mcp_bridge_call", side_effect=fake_bridge_exec),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
            ):
                core_config.invalidate_games_cache()
                r_scan = self.client.post(
                    "/api/games/rpg_unity_game/endpoints/scan",
                    json={},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_scan.status_code, 200)
                eps = {e["id"]: e for e in r_scan.json()["endpoints"]}
                self.assertEqual(eps["il2cpp_dump:HeroStats:Shield"]["category"], "player_character")
                self.assertEqual(eps["il2cpp_dump:HeroStats:Energy"]["category"], "player_character")

                fighter_armor_id = "live_mcp:HeroController:101:Stats.Armor"
                self.assertEqual(eps[fighter_armor_id]["name"], "Fighter — Stats.Armor")
                self.assertEqual(eps[fighter_armor_id]["advanced_details"]["object_id"], "101")
                self.assertEqual(eps[fighter_armor_id]["advanced_details"]["member_path"], "Stats.Armor")

                r_push = self.client.post(
                    "/api/games/rpg_unity_game/endpoints/apply",
                    json={"endpoint_id": fighter_armor_id, "new_value": "35", "action": "apply"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_push.status_code, 200)
                pdata = r_push.json()
                self.assertTrue(pdata["ok"])
                self.assertEqual(pdata["readback_value"], "35")
                self.assertEqual(pushed_calls[0][0], "set_component_value")
                self.assertEqual(pushed_calls[0][1]["game_object_id"], "101")
                self.assertEqual(pushed_calls[0][1]["member_name"], "Stats.Armor")
                core_config.invalidate_games_cache()

    def test_il2cpp_dumper_runner_and_structured_index(self) -> None:
        from app.services import il2cpp_indexer

        sample_dump_cs = "\n".join([
            "// Namespace: UnityEngine",
            "public class Transform : Component",
            "{",
            "    public static int EngineOnlyFlag;",
            "    public float x;",
            "}",
            "// Namespace: System.IO",
            "public enum SeekOrigin",
            "{",
            "    public int value__;",
            "    public const SeekOrigin Begin = 0;",
            "}",
            "// Namespace: Game.Stats",
            "public enum StatType",
            "{",
            "    public int value__; // 0x0",
            "    public const StatType Armor = 0; // 0x0",
            "    public const StatType MaxHealth = 1; // 0x0",
            "    public const StatType ActionPoints = 0x2; // 0x0",
            "}",
            "// Namespace: Game.Managers",
            "public class BattleManager : MonoBehaviour",
            "{",
            "    public static BattleManager Instance { get; set; }",
            "    public static int TurnCounter; // 0x8",
            "    // RVA: 0x104A20 Offset: 0x103E20 VA: 0x180104A20",
            "    public bool CanSpendActionPoints(int cost) { }",
            "    public float CalculateDamage(float baseDamage, bool isCrit) { }",
            "    public string ToString() { }",
            "}",
            "// Namespace: Game.Data",
            "[Serializable]",
            "public class HeroCardData : ScriptableObject",
            "{",
            '    [Header("Card Stats")]',
            '    [Tooltip("Armor granted to hero when card resolves")]',
            "    [Range(1, 50)]",
            "    public int BonusArmor; // 0x18",
            "    public StatType PrimaryStat; // 0x1C",
            "}",
        ]) + "\n"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            game_dir = tmp_dir / "SampleIl2CppGame"
            meta_dir = game_dir / "Sample_Data" / "il2cpp_data" / "Metadata"
            meta_dir.mkdir(parents=True, exist_ok=True)
            (game_dir / "GameAssembly.dll").write_bytes(b"MZ")
            (meta_dir / "global-metadata.dat").write_bytes(b"META")

            tools_root = tmp_dir / "tools"
            dumper_dir = tools_root / "Il2CppDumper"
            dumper_dir.mkdir(parents=True, exist_ok=True)
            (dumper_dir / "Il2CppDumper.exe").write_bytes(b"MZ")

            appdata_root = tmp_dir / "appdata"
            direct_dump_dir = tmp_dir / "direct_dump"
            direct_dump_dir.mkdir(parents=True, exist_ok=True)
            (direct_dump_dir / "dump.cs").write_text(sample_dump_cs, encoding="utf-8")

            # 1. Direct build_il2cpp_index check
            direct_idx = il2cpp_indexer.build_il2cpp_index("direct_sample", direct_dump_dir, app_data_root=appdata_root)
            self.assertNotIn("UnityEngine.Transform", direct_idx["classes"])
            self.assertNotIn("UnityEngine.Transform", direct_idx["static_roots"])
            self.assertNotIn("System.IO.SeekOrigin", direct_idx["enums"])

            self.assertEqual(direct_idx["enums"]["Game.Stats.StatType"], {"Armor": 0, "MaxHealth": 1, "ActionPoints": 2})
            self.assertIn("Game.Managers.BattleManager", direct_idx["static_roots"])
            self.assertTrue(any(m["name"] == "Instance" for m in direct_idx["static_roots"]["Game.Managers.BattleManager"]["singleton_members"]))
            self.assertIn("Game.Data.HeroCardData", direct_idx["classes"])
            self.assertTrue(direct_idx["classes"]["Game.Data.HeroCardData"]["is_scriptable_object"])
            self.assertTrue(direct_idx["classes"]["Game.Data.HeroCardData"]["is_serializable"])
            card_fields = {f["name"]: f for f in direct_idx["classes"]["Game.Data.HeroCardData"]["fields"]}
            self.assertEqual(card_fields["BonusArmor"]["min"], 1)
            self.assertEqual(card_fields["BonusArmor"]["max"], 50)
            self.assertIn("Armor granted", card_fields["BonusArmor"]["tooltip"])

            meth_names = {m["name"] for m in direct_idx["patchable_methods"]}
            self.assertIn("CanSpendActionPoints", meth_names)
            self.assertIn("CalculateDamage", meth_names)
            self.assertNotIn("ToString", meth_names)

            # 2. Full API POST /il2cpp/dump + GET /il2cpp/index + endpoints discovery integration
            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "il2cpp_indexed_game",
                        "name": "IL2CPP Indexed Game",
                        "game_dir": str(game_dir),
                        "runtime": "Unity_IL2CPP",
                    }]
                }),
                encoding="utf-8",
            )

            def fake_dumper_run(cmd, **kwargs):
                out_dir = Path(cmd[3])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "dump.cs").write_text(sample_dump_cs, encoding="utf-8")
                (out_dir / "script.json").write_text(json.dumps({"ScriptMethod": []}), encoding="utf-8")
                return type("Proc", (), {"returncode": 0, "stdout": "Done!", "stderr": ""})()

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoints_service, "_probe_live_bepinex_bridge", return_value=[]),
                patch.object(il2cpp_indexer, "TOOLS_ROOT", tools_root),
                patch.object(il2cpp_indexer.subprocess, "run", side_effect=fake_dumper_run) as mock_run,
            ):
                core_config.invalidate_games_cache()
                r_pre = self.client.get("/api/games/il2cpp_indexed_game/il2cpp/index")
                self.assertEqual(r_pre.status_code, 200)
                self.assertFalse(r_pre.json()["indexed"])

                r_dump = self.client.post(
                    "/api/games/il2cpp_indexed_game/il2cpp/dump",
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_dump.status_code, 200)
                mock_run.assert_called_once()
                ddata = r_dump.json()
                self.assertEqual(ddata["counts"]["enums"], 1)
                self.assertEqual(ddata["counts"]["static_roots"], 1)
                self.assertGreaterEqual(ddata["counts"]["classes"], 1)
                self.assertGreaterEqual(ddata["counts"]["patchable_methods"], 2)

                r_idx = self.client.get("/api/games/il2cpp_indexed_game/il2cpp/index")
                self.assertEqual(r_idx.status_code, 200)
                self.assertTrue(r_idx.json()["indexed"])

                r_scan = self.client.post(
                    "/api/games/il2cpp_indexed_game/endpoints/scan",
                    json={},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_scan.status_code, 200)
                eps = {e["id"]: e for e in r_scan.json()["endpoints"]}
                self.assertIn("il2cpp_idx:HeroCardData:PrimaryStat", eps)
                self.assertEqual(eps["il2cpp_idx:HeroCardData:PrimaryStat"]["options"], ["Armor", "MaxHealth", "ActionPoints"])
                self.assertIn("il2cpp_method:BattleManager:CanSpendActionPoints", eps)
                core_config.invalidate_games_cache()

    def test_il2cpp_bridge_scriptable_static_and_collection_contracts(self) -> None:
        import ast
        import asyncio
        import importlib.util

        plugin_dir = ROOT / "bridges" / "bepinex-mcp" / "plugins" / "BepInExMCP.IL2CPP"
        mcp_dir = ROOT / "bridges" / "bepinex-mcp" / "mcp"

        protocol_cs = (plugin_dir / "Protocol.cs").read_text(encoding="utf-8")
        reg_cs = (plugin_dir / "RegistrationIdStore.cs").read_text(encoding="utf-8")
        plugin_cs = (plugin_dir / "Plugin.cs").read_text(encoding="utf-8")
        backend_cs = (plugin_dir / "Il2CppGameBackend.cs").read_text(encoding="utf-8")
        router_cs = (plugin_dir / "CommandRouter.cs").read_text(encoding="utf-8")

        for field_name in ("object_id", "type_name", "static_members", "member_type", "field_path", "collection_type", "element_type"):
            self.assertIn(f'JsonPropertyName("{field_name}")', protocol_cs)

        self.assertIn("RegisterObject(", reg_cs)
        self.assertIn("TryGetObject(", reg_cs)
        self.assertIn("new Il2CppGameBackend(\n", plugin_cs.replace("\r\n", "\n"))
        self.assertIn("registrationIds)", plugin_cs)

        for method_sig in (
            "ListScriptableObjects(",
            "InspectStaticType(",
            "ExpandCollection(",
            "SetCollectionEntry(",
            "Resources.FindObjectsOfTypeAll<ScriptableObject>()",
            "BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static",
        ):
            self.assertIn(method_sig, backend_cs)

        for route in (
            "scene/list_scriptable_objects",
            "type/inspect_static",
            "object/expand_collection",
            "object/set_collection_entry",
        ):
            self.assertIn(f'"{route}"', backend_cs)
            self.assertGreaterEqual(router_cs.count(f'"{route}"'), 2)

        self.assertTrue((plugin_dir / "bin" / "Release" / "net6.0" / "BepInExMCP.IL2CPP.dll").is_file())

        # Verify BridgeClient Python methods and forwarding arguments
        spec = importlib.util.spec_from_file_location("bepinex_bridge_client", mcp_dir / "bridge_client.py")
        self.assertIsNotNone(spec)
        bridge_mod = importlib.util.module_from_spec(spec)
        sys.modules["bepinex_bridge_client"] = bridge_mod
        spec.loader.exec_module(bridge_mod)

        client = bridge_mod.BridgeClient("http://127.0.0.1:8080/mcp")
        calls: list[tuple[str, dict | None]] = []

        async def fake_get(self_client, endpoint: str, params=None) -> str:
            calls.append((endpoint, params))
            return "{}"

        with patch.object(bridge_mod.BridgeClient, "get", new=fake_get):
            asyncio.run(client.list_scriptable_objects(filter="Hero", limit=25))
            asyncio.run(client.inspect_static_type("Game.BattleManager"))
            asyncio.run(client.expand_collection("so-1", "PartyStats", limit=15))
            asyncio.run(client.set_collection_entry("so-1", "PartyStats", "Health", "999"))

        self.assertEqual(
            calls,
            [
                ("scene/list_scriptable_objects", {"filter": "Hero", "limit": 25}),
                ("type/inspect_static", {"typeName": "Game.BattleManager"}),
                ("object/expand_collection", {"objectId": "so-1", "fieldPath": "PartyStats", "limit": 15}),
                ("object/set_collection_entry", {"objectId": "so-1", "fieldPath": "PartyStats", "key": "Health", "value": "999"}),
            ],
        )

        # Verify @mcp.tool() registrations in ModdersHelperApp.py
        mcp_app_src = (mcp_dir / "ModdersHelperApp.py").read_text(encoding="utf-8")
        tree = ast.parse(mcp_app_src)
        tool_funcs: dict[str, ast.AsyncFunctionDef] = {}
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "tool":
                        tool_funcs[node.name] = node

        for tool_name in ("list_scriptable_objects", "inspect_static_type", "expand_collection", "set_collection_entry"):
            self.assertIn(tool_name, tool_funcs)

    def test_il2cpp_deep_discovery_collection_apply_method_hooks_and_data_files(self) -> None:
        import asyncio
        import os
        from app.core.config import atomic_write_json
        from app.services import mod_builder

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            appdata_root = tmp_dir / "appdata"
            snaps_root = tmp_dir / "snapshots"
            defaults_dir = appdata_root / "game_defaults"
            game_dir = tmp_dir / "DeepIl2CppGame"
            data_dir = game_dir / "DeepGame_Data"
            sa_dir = data_dir / "StreamingAssets"
            (sa_dir / "aa").mkdir(parents=True)
            (game_dir / "BepInEx" / "interop").mkdir(parents=True)
            (game_dir / "GameAssembly.dll").write_bytes(b"MZ-IL2CPP")
            (data_dir / "app.info").write_text("CoolStudio\nDeepRPG\n", encoding="utf-8")

            # Gameplay data in StreamingAssets + excluded Unity/Addressables boilerplate
            (sa_dir / "aa" / "catalog.json").write_text(json.dumps({"m_InternalIds": ["a", "b"]}), encoding="utf-8")
            (sa_dir / "link.xml").write_text("<linker><assembly fullname=\"Game\"/></linker>", encoding="utf-8")
            (sa_dir / "gameplay_balance.json").write_text(
                json.dumps({"combat": {"enemyDamageMultiplier": 1.0, "startingGold": 150}}),
                encoding="utf-8",
            )

            # LocalLow gameplay config
            user_prof = tmp_dir / "userprofile"
            locallow_game = user_prof / "AppData" / "LocalLow" / "CoolStudio" / "DeepRPG"
            locallow_game.mkdir(parents=True)
            (locallow_game / "user_tweaks.xml").write_text("<Settings><FieldOfView>75</FieldOfView></Settings>", encoding="utf-8")
            (locallow_game / "Player.log").write_text("Unity log", encoding="utf-8")

            # Seed il2cpp_index.json with enums, static_roots, and patchable_methods
            slug = "deep_il2cpp_game"
            idx_path = appdata_root / "dumps" / slug / "il2cpp_index.json"
            atomic_write_json(idx_path, {
                "slug": slug,
                "enums": {
                    "Game.Stats.StatType": {"Health": 0, "Armor": 1, "ActionPoints": 2},
                },
                "static_roots": {
                    "Game.Managers.PartyContext": {
                        "static_fields": [{"name": "Instance", "type": "Game.Managers.PartyContext", "kind": "field"}],
                        "singleton_members": [{"name": "Instance", "type": "Game.Managers.PartyContext", "kind": "field"}],
                    },
                },
                "classes": {},
                "patchable_methods": [
                    {
                        "class_full_name": "Game.Combat.DamageCalculator",
                        "class_name": "DamageCalculator",
                        "namespace": "Game.Combat",
                        "name": "CalculateDamage",
                        "return_type": "System.Single",
                        "parameters": "System.Int32 baseDmg",
                        "signature": "Game.Combat.DamageCalculator.CalculateDamage(System.Int32 baseDmg)",
                        "rva": "0x2000",
                    },
                    {
                        "class_full_name": "Game.Managers.PartyContext",
                        "class_name": "PartyContext",
                        "namespace": "Game.Managers",
                        "name": "CanSpendActionPoints",
                        "return_type": "System.Boolean",
                        "parameters": "System.Int32 cost",
                        "signature": "Game.Managers.PartyContext.CanSpendActionPoints(System.Int32 cost)",
                        "rva": "0x2040",
                    },
                ],
            })

            # Stateful mocked bridge
            live_stats: dict[str, str] = {"0": "30", "1": "12", "2": "3"}
            active_patches: dict[str, dict[str, Any]] = {}
            patch_calls: list[dict[str, Any]] = []
            remove_calls: list[str] = []

            def fake_bridge_sync(tool_name: str, params: dict[str, Any] | None = None) -> Any:
                p = params or {}
                if tool_name == "inspect_static_type":
                    return {
                        "type_name": "Game.Managers.PartyContext",
                        "static_members": [
                            {"name": "Instance", "member_type": "Game.Managers.PartyContext", "value": "PartyContext", "object_id": "ref-party"},
                        ],
                    }
                if tool_name == "describe_runtime_type":
                    tn = str(p.get("typeName") or "")
                    if tn == "Game.Managers.PartyContext":
                        return {"members": [{"name": "partyPieces", "type": "Il2CppSystem.Collections.Generic.List<Game.Actors.HeroPiece>", "readable": True, "isStatic": False}]}
                    if tn == "Game.Actors.HeroPiece":
                        return {"members": [{"name": "stats", "type": "Il2CppSystem.Collections.Generic.Dictionary<Game.Stats.StatType, System.Int32>", "readable": True, "isStatic": False}]}
                    if tn == "Game.Data.HeroTemplate":
                        return {"members": [{"name": "BaseHealth", "type": "System.Int32", "readable": True, "isStatic": False}]}
                    if tn == "Game.Data.CardTemplate":
                        return {"members": [{"name": "Damage", "type": "System.Single", "readable": True, "isStatic": False}]}
                    return {"members": []}
                if tool_name == "list_scriptable_objects":
                    return [
                        {"object_id": "so-hero", "name": "Hero_Paladin", "type_name": "Game.Data.HeroTemplate"},
                        {"object_id": "so-card", "name": "FireballCard", "type_name": "Game.Data.CardTemplate"},
                        {"object_id": "so-tmp", "name": "TMP_Settings", "type_name": "TMPro.TMP_Settings"},
                    ]
                if tool_name == "expand_collection":
                    oid = str(p.get("object_id") or p.get("objectId") or "")
                    fp = str(p.get("field_path") or p.get("fieldPath") or "")
                    if oid == "ref-party" and fp == "partyPieces":
                        return {
                            "object_id": "ref-party",
                            "field_path": "partyPieces",
                            "collection_type": "Il2CppSystem.Collections.Generic.List<Game.Actors.HeroPiece>",
                            "entries": [{"key": "0", "value": "Fighter", "element_type": "Game.Actors.HeroPiece", "object_id": "elem-hero-0"}],
                        }
                    if oid == "elem-hero-0" and fp == "stats":
                        return {
                            "object_id": "elem-hero-0",
                            "field_path": "stats",
                            "collection_type": "Il2CppSystem.Collections.Generic.Dictionary<Game.Stats.StatType, System.Int32>",
                            "entries": [
                                {"key": k, "value": v, "element_type": "System.Int32"}
                                for k, v in live_stats.items()
                            ],
                        }
                return {}

            async def fake_bridge_async(tool_name: str, args: dict[str, Any], game_obj: dict[str, Any]) -> dict[str, Any]:
                if tool_name == "set_collection_entry":
                    if str(args.get("object_id")) == "elem-hero-0" and str(args.get("field_path")) == "stats":
                        live_stats[str(args["key"])] = str(args["value"])
                    return {"tool": tool_name, "isError": False, "raw": '{"status":"ok"}', "data": {"status": "ok"}}
                if tool_name == "expand_collection":
                    data = fake_bridge_sync("expand_collection", args)
                    return {"tool": tool_name, "isError": False, "raw": json.dumps(data), "data": data}
                if tool_name == "mod_patch_method":
                    patch_calls.append(dict(args))
                    rid = str(args["registration_id"])
                    active_patches[rid] = {"id": rid, "target": f"{args['target_class']}.{args['target_method']}", "active": True}
                    return {"tool": tool_name, "isError": False, "raw": json.dumps({"status": "ok", "id": rid}), "data": {"status": "ok", "id": rid}}
                if tool_name == "remove_mod_patch":
                    rid = str(args["registration_id"])
                    remove_calls.append(rid)
                    active_patches.pop(rid, None)
                    return {"tool": tool_name, "isError": False, "raw": '{"status":"ok"}', "data": {"status": "ok"}}
                if tool_name == "list_mod_patches":
                    plist = list(active_patches.values())
                    return {"tool": tool_name, "isError": False, "raw": json.dumps(plist), "data": plist}
                return {"tool": tool_name, "isError": True, "raw": f"Unexpected {tool_name}", "data": {}}

            game_rec = {
                "id": slug,
                "name": "Deep IL2CPP Game",
                "game_dir": str(game_dir),
                "runtime": "Unity_IL2CPP",
            }
            with (
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoint_applier, "APP_DATA_ROOT", appdata_root),
                patch.dict(os.environ, {"USERPROFILE": str(user_prof)}),
            ):
                report = endpoints_service.scan_game_endpoints(
                    game_rec,
                    defaults_dir=defaults_dir,
                    bridge_call_fn=fake_bridge_sync,
                )
                eps = {e["id"]: e for e in report["endpoints"]}

                # 1. Enum-decoded dictionary entry from depth-2 collection expansion
                armor_ep = next((e for e in report["endpoints"] if e["advanced_details"].get("collection_key_label") == "Armor"), None)
                self.assertIsNotNone(armor_ep)
                self.assertEqual(armor_ep["advanced_details"]["collection_key"], "1")
                self.assertEqual(armor_ep["current_value"], "12")
                self.assertIn("Armor", armor_ep["name"])

                # 2. Loaded ScriptableObject fields categorized cleanly, TMPro excluded
                self.assertIn("live_so:HeroTemplate:so-hero:BaseHealth", eps)
                self.assertEqual(eps["live_so:HeroTemplate:so-hero:BaseHealth"]["category"], "player_character")
                self.assertIn("live_so:CardTemplate:so-card:Damage", eps)
                self.assertEqual(eps["live_so:CardTemplate:so-card:Damage"]["category"], "game_systems")
                self.assertFalse(any("TMP_Settings" in eid for eid in eps))

                # 3. Patchable methods get numeric multiplier (1.0) & bool override ("vanilla") with Bridge delivery when online
                dmg_m = eps["il2cpp_method:DamageCalculator:CalculateDamage"]
                self.assertEqual(dmg_m["default_value"], "1.0")
                self.assertEqual(dmg_m["delivery_type"], "Bridge")
                self.assertEqual(dmg_m["advanced_details"]["modifier_kind"], "numeric_multiplier")
                bool_m = eps["il2cpp_method:PartyContext:CanSpendActionPoints"]
                self.assertEqual(bool_m["default_value"], "vanilla")
                self.assertEqual(bool_m["options"], ["vanilla", "always_true", "always_false"])

                # Offline scan falls back to Mod File delivery for patchable methods
                offline_report = endpoints_service.scan_game_endpoints(
                    game_rec,
                    defaults_dir=defaults_dir,
                    live_bridge_fn=lambda g, i: [],
                )
                offline_eps = {e["id"]: e for e in offline_report["endpoints"]}
                self.assertEqual(offline_eps["il2cpp_method:DamageCalculator:CalculateDamage"]["delivery_type"], "Mod File")

                # 4. StreamingAssets & LocalLow data files discovered while excluding aa/catalog.json & link.xml
                self.assertIn("unity_data:streaming_assets_data:gameplay_balance.json:combat.startingGold", eps)
                self.assertIn("unity_data:locallow_data:user_tweaks.xml:FieldOfView", eps)
                self.assertFalse(any("catalog.json" in eid or "link.xml" in eid for eid in eps))

                # 5. Apply collection entry -> set_collection_entry + expand_collection read-back persistence check
                col_res = asyncio.run(endpoint_applier.apply_endpoint_change(
                    game_rec,
                    armor_ep["id"],
                    new_value="25",
                    defaults_dir=defaults_dir,
                    snapshots_root=snaps_root,
                    bridge_call_fn=fake_bridge_async,
                ))
                self.assertTrue(col_res["ok"])
                self.assertTrue(col_res["validated"])
                self.assertEqual(col_res["readback_value"], "25")
                self.assertEqual(live_stats["1"], "25")

                # 6. Apply method multiplier -> mod_patch_method with DynamicPatcher Postfix, then validate & unpatch on reset_default
                meth_res = asyncio.run(endpoint_applier.apply_endpoint_change(
                    game_rec,
                    dmg_m["id"],
                    new_value="2.5",
                    defaults_dir=defaults_dir,
                    snapshots_root=snaps_root,
                    bridge_call_fn=fake_bridge_async,
                ))
                self.assertTrue(meth_res["ok"])
                self.assertEqual(len(patch_calls), 1)
                self.assertIn("DynamicPatcher", patch_calls[0]["patch_code"])
                self.assertIn("2.5f", patch_calls[0]["patch_code"])

                val_patch = asyncio.run(endpoint_applier.validate_endpoint_state(
                    game_rec,
                    dmg_m["id"],
                    expected_value="2.5",
                    defaults_dir=defaults_dir,
                    bridge_call_fn=fake_bridge_async,
                ))
                self.assertTrue(val_patch["validated"])

                reset_res = asyncio.run(endpoint_applier.apply_endpoint_change(
                    game_rec,
                    dmg_m["id"],
                    action="reset_default",
                    defaults_dir=defaults_dir,
                    snapshots_root=snaps_root,
                    bridge_call_fn=fake_bridge_async,
                ))
                self.assertTrue(reset_res["ok"])
                self.assertEqual(len(remove_calls), 1)
                self.assertEqual(reset_res["readback_value"], "1.0")

                # 7. StreamingAssets JSON edit + persistence verification
                json_res = asyncio.run(endpoint_applier.apply_endpoint_change(
                    game_rec,
                    "unity_data:streaming_assets_data:gameplay_balance.json:combat.startingGold",
                    new_value="500",
                    defaults_dir=defaults_dir,
                    snapshots_root=snaps_root,
                ))
                self.assertTrue(json_res["ok"])
                saved_json = json.loads((sa_dir / "gameplay_balance.json").read_text(encoding="utf-8"))
                self.assertEqual(saved_json["combat"]["startingGold"], 500)

                # 8. Verify offline Harmony C# plugin generation includes multiplier & bool postfix hooks
                offline_cs = mod_builder._generate_il2cpp_plugin_cs(
                    "com.test.hooks",
                    "Test Hooks",
                    "1.0.0",
                    "TestHooksMod",
                    2.0,
                    [
                        type("Rule", (), {"rule_type": "harmony_patch", "label": "Dmg x2", "component_name": "Game.Combat.DamageCalculator", "target_method": "CalculateDamage", "member_path": "CalculateDamage", "hook_kind": "Postfix", "return_type": "System.Single", "value": "2.0", "skip_original": False})(),
                        type("Rule", (), {"rule_type": "harmony_patch", "label": "Free AP", "component_name": "Game.Managers.PartyContext", "target_method": "CanSpendActionPoints", "member_path": "CanSpendActionPoints", "hook_kind": "Postfix", "return_type": "System.Boolean", "value": "always_true", "skip_original": False})(),
                    ],
                )
                self.assertIn("DynamicMultiplierPostfix_0(ref float __result)", offline_cs)
                self.assertIn("DynamicBoolPostfix_1(ref bool __result)", offline_cs)
                core_config.invalidate_games_cache()

    def test_bridge_action_diff_capture_compare_and_promote_flow(self) -> None:
        from app.services import bridge_diff, endpoint_catalog

        html = self.client.get("/").text
        for dom_id in ("card-action-diff", "btn-action-diff-baseline", "btn-action-diff-compare", "action-diff-tbody"):
            self.assertIn(f'id="{dom_id}"', html)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            appdata_root = tmp_dir / "appdata"
            game_dir = tmp_dir / "DiffSampleGame"
            game_dir.mkdir(parents=True)
            (game_dir / "GameAssembly.dll").write_bytes(b"MZ")
            slug = "diff_sample_game"

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": slug,
                        "name": "Diff Sample Game",
                        "game_dir": str(game_dir),
                        "runtime": "Unity_IL2CPP",
                    }]
                }),
                encoding="utf-8",
            )

            mem_state: dict[str, Any] = {
                "ActionPoints": 3,
                "Health": 22,
                "HeroName": "Fighter",
                "Gold": 100,
                "CameraTime": 10.5,
            }

            def fake_bridge_caller(tool_name: str, params: dict[str, Any] | None = None) -> Any:
                p = params or {}
                if tool_name == "search_runtime_types":
                    if p.get("query") == "Hero":
                        return [
                            {"name": "HeroController", "fullName": "Game.Actors.HeroController", "isComponent": True},
                            {"name": "CameraClock", "fullName": "Game.Camera.CameraClock", "isComponent": False},
                        ]
                    return []
                if tool_name == "describe_runtime_type":
                    tn = str(p.get("typeName") or "")
                    if tn == "Game.Actors.HeroController":
                        return {
                            "members": [
                                {"name": "HeroName", "type": "System.String", "readable": True, "isStatic": False},
                                {"name": "ActionPoints", "type": "System.Int32", "readable": True, "isStatic": False},
                                {"name": "Health", "type": "System.Int32", "readable": True, "isStatic": False},
                                {"name": "Gold", "type": "System.Int32", "readable": True, "isStatic": False},
                            ]
                        }
                    if tn == "Game.Camera.CameraClock":
                        return {
                            "members": [
                                {"name": "elapsedTime", "type": "System.Single", "readable": True, "isStatic": False},
                            ]
                        }
                if tool_name == "find_objects_with_component":
                    if p.get("componentName") == "Game.Actors.HeroController":
                        return [{"id": 501, "name": "Hero_Fighter"}]
                    if p.get("componentName") == "Game.Camera.CameraClock":
                        return [{"id": 502, "name": "MainCamera"}]
                if tool_name == "get_component_member":
                    gid = str(p.get("id") or "")
                    mn = str(p.get("memberName") or "")
                    if gid == "501" and mn in mem_state:
                        return {"value": str(mem_state[mn])}
                    if gid == "502" and mn == "elapsedTime":
                        return {"value": str(mem_state["CameraTime"])}
                return {}

            bridge_diff.clear_bridge_diff_store(slug)
            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(bridge_diff, "APP_DATA_ROOT", appdata_root),
                patch.object(bridge_diff, "_make_live_http_caller", return_value=fake_bridge_caller),
                patch.object(endpoints_service, "_probe_live_bepinex_bridge", return_value=[]),
            ):
                core_config.invalidate_games_cache()

                # Missing baseline returns 400
                r_no_base = self.client.post(
                    f"/api/games/{slug}/bridge-diff/compare",
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_no_base.status_code, 400)

                # 1. Capture baseline before action
                r_base = self.client.post(
                    f"/api/games/{slug}/bridge-diff/baseline",
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_base.status_code, 200)
                base_data = r_base.json()
                self.assertGreaterEqual(base_data["captured_count"], 3)

                # Simulate in-game action: spend 1 AP (3 -> 2), take damage (22 -> 17), plus unrelated camera timer noise
                mem_state["ActionPoints"] = 2
                mem_state["Health"] = 17
                mem_state["CameraTime"] = 14.25

                # 2. Compare second snapshot
                r_cmp = self.client.post(
                    f"/api/games/{slug}/bridge-diff/compare",
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_cmp.status_code, 200)
                cmp_data = r_cmp.json()
                diffs = cmp_data["diffs"]
                by_field = {d["raw_field_path"]: d for d in diffs}
                self.assertIn("ActionPoints", by_field)
                self.assertIn("Health", by_field)
                self.assertNotIn("elapsedTime", by_field)
                self.assertEqual(by_field["ActionPoints"]["before_value"], "3")
                self.assertEqual(by_field["ActionPoints"]["after_value"], "2")
                self.assertEqual(by_field["ActionPoints"]["delta"], -1)
                self.assertEqual(by_field["Health"]["before_value"], "22")
                self.assertEqual(by_field["Health"]["after_value"], "17")
                self.assertEqual(by_field["Health"]["delta"], -5)

                # 3. Promote ActionPoints diff into persistent categorized catalog & endpoint_knowledge.json
                r_prom = self.client.post(
                    f"/api/games/{slug}/bridge-diff/promote",
                    json={"items": [{"diff_id": by_field["ActionPoints"]["diff_id"], "category": "player_character"}]},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_prom.status_code, 200)
                prom_data = r_prom.json()
                self.assertEqual(prom_data["promoted_count"], 1)
                promoted_ep_id = prom_data["promoted_ids"][0]
                report_eps = {e["id"]: e for e in prom_data["report"]["endpoints"]}
                self.assertIn(promoted_ep_id, report_eps)
                ep_rec = report_eps[promoted_ep_id]
                self.assertEqual(ep_rec["category"], "player_character")
                self.assertEqual(ep_rec["default_value"], "3")
                self.assertEqual(ep_rec["current_value"], "2")
                self.assertEqual(ep_rec["delivery_type"], "Bridge")

                # Verify baseline stored in game_defaults/<slug>.json and learned in endpoint_knowledge.json
                defs_file = appdata_root / "game_defaults" / f"{slug}.json"
                defs_json = json.loads(defs_file.read_text(encoding="utf-8"))
                self.assertEqual(defs_json["defaults"][promoted_ep_id], "3")
                k_hit = endpoint_catalog.lookup_endpoint_knowledge(
                    "unity",
                    ["Game.Actors.HeroController.ActionPoints"],
                    app_data_root=appdata_root,
                )
                self.assertIsNotNone(k_hit)
                self.assertEqual(k_hit["category"], "player_character")
                core_config.invalidate_games_cache()


if __name__ == "__main__":
    unittest.main()

"""
Automated smoke tests for The Modding Tool FastAPI controller, caching/ETag,
mod builder (including Harmony hooks), version manager, and backup restore.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.core.security import SESSION_HEADER, get_session_token  # noqa: E402
import backup_manager  # noqa: E402
import version_manager  # noqa: E402


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_index_html_has_core_tabs_and_palette(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        for marker in (
            "tab-btn-manager",
            "tab-btn-game",
            "tab-btn-modbuilder",
            "tab-btn-specs",
            "tab-btn-logs",
            "tab-btn-appmgmt",
            "tab-btn-help",
            "cmd-palette-overlay",
            "app-loading-overlay",
            "app-restarting-overlay",
            "scan-log-panel",
            "btn-toggle-scan-log",
            "btn-copy-scan-log",
            "btn-close-scan-log",
            "/static/app.css",
            "/static/js/main.js",
            'name="modtool-session-token"',
        ):
            self.assertIn(marker, html)
        self.assertNotIn("scan-progress-wrap", html)
        self.assertNotIn("scan-progress-bar", html)

    def test_static_assets_and_health(self) -> None:
        r_css = self.client.get("/static/app.css")
        self.assertEqual(r_css.status_code, 200)
        self.assertIn(":root", r_css.text)

        for js_name in ("main.js", "api.js", "library.js", "analyzer.js", "mcp.js", "app_mgmt.js"):
            r_js = self.client.get(f"/static/js/{js_name}")
            self.assertEqual(r_js.status_code, 200, f"Missing /static/js/{js_name}")
            self.assertIn("javascript", r_js.headers.get("content-type", "").lower())

        r_health = self.client.get("/api/health")
        self.assertEqual(r_health.status_code, 200)
        self.assertEqual(r_health.json().get("status"), "ok")

    def test_session_token_middleware(self) -> None:
        r_unauth = self.client.post("/api/app/dev-mode", json={"enabled": False})
        self.assertEqual(r_unauth.status_code, 403)

        r_auth = self.client.post(
            "/api/app/dev-mode",
            json={"enabled": False},
            headers={SESSION_HEADER: get_session_token()},
        )
        self.assertEqual(r_auth.status_code, 200)
        self.assertEqual(r_auth.json().get("status"), "ok")

    def test_games_etag_and_304(self) -> None:
        r1 = self.client.get("/api/games")
        self.assertEqual(r1.status_code, 200)
        etag = r1.headers.get("etag")
        self.assertTrue(etag)
        r2 = self.client.get("/api/games", headers={"If-None-Match": etag})
        self.assertEqual(r2.status_code, 304)

    def test_logs_etag_and_304(self) -> None:
        r1 = self.client.get("/api/logs?limit=20")
        self.assertEqual(r1.status_code, 200)
        etag = r1.headers.get("etag")
        self.assertTrue(etag)
        r2 = self.client.get("/api/logs?limit=20", headers={"If-None-Match": etag})
        self.assertEqual(r2.status_code, 304)

    def test_version_endpoint(self) -> None:
        r = self.client.get("/api/app/version")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("version", data)
        self.assertIn("channel", data)
        v = version_manager.load_version_data()
        self.assertEqual(v["version"], data["version"])

    def test_app_ui_config_and_watcher_status(self) -> None:
        r1 = self.client.get("/api/app-ui-config")
        self.assertEqual(r1.status_code, 200)
        self.assertIn("config", r1.json())
        r2 = self.client.get("/api/app/watcher/status")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("running", r2.json())

    def test_conflicts_and_steam_assets(self) -> None:
        r1 = self.client.get("/api/games/battleground/conflicts")
        self.assertEqual(r1.status_code, 200)
        self.assertIn("plugins", r1.json())
        r2 = self.client.get("/api/games/battleground/steam-assets")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("has_assets", r2.json())

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

    def test_git_push_up_to_date_and_failure_audit(self) -> None:
        import json
        from unittest.mock import patch
        from app.core import audit as core_audit
        from app.routers import git_sync

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / ".git").mkdir()
            (repo_dir / ".gitignore").write_text(".venv/\n__pycache__/\n*.pyc\n.env\n*.log\n", encoding="utf-8")

            fake_cfg = {
                "owner": "octocat",
                "repo": "modtool",
                "branch": "main",
                "token": "ghp_secret123",
                "installed_sha": "",
                "last_checked": "",
            }

            def mock_up_to_date(step_name, args, cwd, **kwargs):
                if step_name == "remote":
                    return {"step": step_name, "returncode": 0, "stdout": "https://github.com/octocat/modtool.git\n", "stderr": ""}
                if step_name == "rev_list":
                    return {"step": step_name, "returncode": 0, "stdout": "0\n", "stderr": ""}
                return {"step": step_name, "returncode": 0, "stdout": "", "stderr": ""}

            with (
                patch.object(git_sync, "load_repo_config", return_value=dict(fake_cfg)),
                patch.object(git_sync, "_resolve_git_app_root", return_value=repo_dir),
                patch.object(git_sync, "run_git_step", side_effect=mock_up_to_date),
            ):
                r_ok = self.client.post(
                    "/api/app/git-push",
                    json={"commit_message": "", "branch": "main"},
                    headers={SESSION_HEADER: get_session_token()},
                )
            self.assertEqual(r_ok.status_code, 200)
            ok_data = r_ok.json()
            self.assertTrue(ok_data.get("ok"))
            self.assertEqual(ok_data.get("status"), "up_to_date")

            def mock_push_fail(step_name, args, cwd, **kwargs):
                if step_name == "remote":
                    return {"step": step_name, "returncode": 0, "stdout": "https://github.com/octocat/modtool.git\n", "stderr": ""}
                if step_name == "diff_cached":
                    return {"step": step_name, "returncode": 1, "stdout": "", "stderr": ""}
                if step_name == "push":
                    return {
                        "step": "push",
                        "returncode": 128,
                        "stdout": "",
                        "stderr": "fatal: Authentication failed for 'https://github.com/octocat/modtool.git/' token=ghp_secret123",
                    }
                return {"step": step_name, "returncode": 0, "stdout": "", "stderr": ""}

            temp_audit = repo_dir / "audit.jsonl"
            temp_app_audit = repo_dir / "audit_app.jsonl"
            temp_game_audit = repo_dir / "audit_game.jsonl"
            with (
                patch.object(git_sync, "load_repo_config", return_value=dict(fake_cfg)),
                patch.object(git_sync, "_resolve_git_app_root", return_value=repo_dir),
                patch.object(git_sync, "run_git_step", side_effect=mock_push_fail),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                r_fail = self.client.post(
                    "/api/app/git-push",
                    json={"commit_message": "test push", "branch": "main"},
                    headers={SESSION_HEADER: get_session_token()},
                )
            self.assertEqual(r_fail.status_code, 400)
            fail_data = r_fail.json()
            self.assertFalse(fail_data.get("ok"))
            self.assertEqual(fail_data.get("step"), "push")
            self.assertEqual(fail_data.get("exit_code"), 128)
            self.assertEqual(fail_data.get("hint"), "GitHub authentication failed")
            self.assertNotIn("ghp_secret123", fail_data.get("stderr", ""))
            self.assertIn("***", fail_data.get("stderr", ""))

            entries = [json.loads(ln) for ln in temp_audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertTrue(any(
                e.get("action") == "GIT_PUSH_FAILED"
                and e.get("details", {}).get("step") == "push"
                and "***" in e.get("details", {}).get("stderr", "")
                for e in entries
            ))

    def test_action_failed_audit_and_client_error(self) -> None:
        import json
        from unittest.mock import patch
        from app.core import audit as core_audit
        from app.routers import system as system_router

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"

            with (
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                # 1. Direct log_action_error(source, action, endpoint, status_code, message, detail=None) redacts secrets
                entry = core_audit.log_action_error(
                    "unit.test",
                    "test.action",
                    "/api/unit-test",
                    500,
                    RuntimeError("Auth failed https://user:pass123@example.com ghp_secret123456"),
                    detail={"token": "ghp_anothersecret99", "note": "Bearer my_jwt_token"},
                )
                self.assertEqual(entry["action"], "ACTION_FAILED")
                self.assertEqual(entry["status"], "error")
                self.assertNotIn("pass123", entry["summary"])
                self.assertNotIn("ghp_secret123456", entry["summary"])
                self.assertEqual(entry["details"]["detail"]["token"], "***")
                self.assertNotIn("my_jwt_token", entry["details"]["detail"]["note"])

                # 2. HTTPException (404) on /api/* logs ACTION_FAILED and returns standard error JSON
                r_404 = self.client.get("/api/does-not-exist-audit-test")
                self.assertEqual(r_404.status_code, 404)
                d_404 = r_404.json()
                self.assertFalse(d_404.get("ok"))
                self.assertIn("message", d_404)
                self.assertIn("hint", d_404)
                self.assertIn("detail", d_404)

                # 3. Direct 403 from SessionTokenMiddleware on /api/audit/client-error logs ACTION_FAILED and returns standard error JSON
                r_403 = self.client.post("/api/audit/client-error", json={"message": "no token"})
                self.assertEqual(r_403.status_code, 403)
                d_403 = r_403.json()
                self.assertFalse(d_403.get("ok"))
                self.assertIn("message", d_403)
                self.assertIn("hint", d_403)
                self.assertIn("detail", d_403)

                # 4. Validation error (422) on /api/* logs ACTION_FAILED and returns standard error JSON
                r_422 = self.client.post(
                    "/api/app/dev-mode",
                    json={"enabled": {"bad": "type"}},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_422.status_code, 422)
                d_422 = r_422.json()
                self.assertFalse(d_422.get("ok"))
                self.assertIn("message", d_422)
                self.assertIn("hint", d_422)
                self.assertIn("detail", d_422)

                # 5. Unhandled Exception (500) on /api/* logs ACTION_FAILED and returns sanitized standard error JSON
                no_raise_client = TestClient(app, raise_server_exceptions=False)
                with patch.object(
                    system_router,
                    "_load_workspace_module",
                    side_effect=RuntimeError("boom token=ghp_supersecret12345"),
                ):
                    r_500 = no_raise_client.get("/api/app/version")
                self.assertEqual(r_500.status_code, 500)
                d_500 = r_500.json()
                self.assertFalse(d_500.get("ok"))
                self.assertIn("message", d_500)
                self.assertIn("hint", d_500)
                self.assertIn("detail", d_500)
                self.assertNotIn("ghp_supersecret12345", json.dumps(d_500))

                # 6. POST /api/audit/client-error with session token logs sanitized ACTION_FAILED
                r_client = self.client.post(
                    "/api/audit/client-error",
                    json={
                        "source": "ui.window.onerror",
                        "message": "Uncaught TypeError with Bearer secret_abc123",
                        "stack": "at fn (http://127.0.0.1:8765/static/js/main.js:10:2) ghp_clientsecret99",
                        "url": "http://127.0.0.1:8765/",
                        "line": 10,
                        "column": 2,
                    },
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_client.status_code, 200)
                c_data = r_client.json()
                self.assertEqual(c_data.get("status"), "ok")
                self.assertEqual(c_data.get("entry", {}).get("action"), "ACTION_FAILED")
                self.assertNotIn("secret_abc123", json.dumps(c_data))
                self.assertNotIn("ghp_clientsecret99", json.dumps(c_data))

            raw_text = temp_audit.read_text(encoding="utf-8")
            self.assertNotIn("ghp_secret123456", raw_text)
            self.assertNotIn("ghp_supersecret12345", raw_text)
            self.assertNotIn("ghp_clientsecret99", raw_text)
            self.assertNotIn("secret_abc123", raw_text)
            entries = [json.loads(ln) for ln in raw_text.splitlines() if ln.strip()]
            self.assertGreaterEqual(len(entries), 6)
            self.assertTrue(all(e.get("action") == "ACTION_FAILED" for e in entries))
            self.assertTrue(any(e.get("details", {}).get("status_code") == 403 for e in entries))
            self.assertTrue(any(e.get("details", {}).get("status_code") == 404 for e in entries))
            self.assertTrue(any(e.get("details", {}).get("status_code") == 422 for e in entries))
            self.assertTrue(any(e.get("details", {}).get("status_code") == 500 for e in entries))
            self.assertTrue(any(e.get("details", {}).get("source") == "ui.window.onerror" for e in entries))

    def test_tracked_action_assets_and_audit_jsonl_action_failed(self) -> None:
        import json
        from unittest.mock import patch
        from app.core import audit as core_audit

        # Verify shared progress/error CSS and wired JS helpers are served cleanly
        css_resp = self.client.get("/static/app.css")
        self.assertEqual(css_resp.status_code, 200)
        for css_marker in (".action-status-slot", ".is-running", ".is-success", ".is-error", ".action-status-copy"):
            self.assertIn(css_marker, css_resp.text)

        for js_name in ("api.js", "analyzer.js", "library.js", "mcp.js", "app_mgmt.js"):
            js_resp = self.client.get(f"/static/js/{js_name}")
            self.assertEqual(js_resp.status_code, 200)
            self.assertIn("runTrackedAction", js_resp.text)
        self.assertIn("/api/audit/client-error", self.client.get("/static/js/api.js").text)

        # Verify ACTION_FAILED entries are written cleanly to audit.jsonl
        with tempfile.TemporaryDirectory() as tmp:
            audit_file = Path(tmp) / "audit.jsonl"
            app_audit_file = Path(tmp) / "audit_app.jsonl"
            game_audit_file = Path(tmp) / "audit_game.jsonl"
            with (
                patch.object(core_audit, "AUDIT_LOG_PATH", audit_file),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", app_audit_file),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", game_audit_file),
            ):
                r_fail = self.client.post(
                    "/api/game/analyze-assets",
                    json={"root_path": str(Path(tmp) / "missing_game_dir")},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_fail.status_code, 404)

                r_client = self.client.post(
                    "/api/audit/client-error",
                    json={
                        "source": "ui.action.btn-scan-assets",
                        "message": "Game directory not found on disk",
                        "url": "http://127.0.0.1:8765/",
                        "details": {"pending_text": "Scanning game directory", "trigger_id": "btn-scan-assets"},
                    },
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_client.status_code, 200)

            self.assertTrue(audit_file.is_file())
            logged = [json.loads(ln) for ln in audit_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            failed_entries = [e for e in logged if e.get("action") == "ACTION_FAILED"]
            self.assertGreaterEqual(len(failed_entries), 2)
            self.assertTrue(any(e.get("details", {}).get("endpoint") == "/api/game/analyze-assets" for e in failed_entries))
            self.assertTrue(any(e.get("details", {}).get("source") == "ui.action.btn-scan-assets" for e in failed_entries))

    def test_backup_restore_integrity(self) -> None:
        idx = backup_manager.load_index()
        versions = idx.get("versions", [])
        self.assertGreaterEqual(len(versions), 1)
        latest = versions[-1]
        latest_id = latest["version_id"]
        manifest = json.loads((backup_manager.BACKUPS_DIR / latest["folder"] / "manifest.json").read_text(encoding="utf-8"))
        expected = manifest.get("files", {})
        self.assertGreaterEqual(len(expected), 1)
        with tempfile.TemporaryDirectory() as tmp:
            backup_manager.restore_version(latest_id, Path(tmp))
            for rel, meta in expected.items():
                restored = Path(tmp) / rel
                self.assertTrue(restored.is_file(), rel)
                self.assertEqual(backup_manager.sha256_file(restored), meta["sha256"], rel)

    def test_appdata_path_and_legacy_migration(self) -> None:
        import os
        from unittest.mock import patch
        from app.core import config as core_config

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

                # Second call must not overwrite newer files in TheModdingTool
                (resolved / "managed_games.json").write_text('{"games": ["updated"]}', encoding="utf-8")
                self.assertFalse(core_config.migrate_legacy_appdata(resolved, legacy_dir))
                self.assertIn("updated", (resolved / "managed_games.json").read_text(encoding="utf-8"))

    def test_steam_reference_csv_and_packaging_config(self) -> None:
        from app.routers import library as library_router
        import package_windows

        self.assertEqual(library_router.STEAM_REF_CSV.parts[-3:], ("app", "data", "steam_games_reference.csv"))
        rows = library_router.load_steam_reference_sheet()
        self.assertGreater(len(rows), 0)

        cmd = package_windows.build_pyinstaller_command()
        self.assertIn("TheModdingTool", cmd)
        self.assertIn("--icon", cmd)
        self.assertTrue(any("app/data" in arg and "steam_games_reference.csv" in arg for arg in cmd))

    def test_branding_upload_pillow_cropping(self) -> None:
        import io
        from PIL import Image
        from app.routers import branding as branding_router

        buf = io.BytesIO()
        Image.new("RGBA", (400, 200), (20, 120, 220, 255)).save(buf, format="PNG")
        raw = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            fav_png = branding_router._save_processed_favicon(raw, ".png", tmp_dir)
            self.assertEqual(fav_png.name, "favicon.png")
            with Image.open(fav_png) as im_png:
                self.assertEqual(im_png.size, (64, 64))
            ico_path = tmp_dir / "app.ico"
            self.assertTrue(ico_path.is_file())
            with Image.open(ico_path) as im_ico:
                self.assertEqual(im_ico.format, "ICO")

            banner_png = branding_router._save_processed_banner(raw, ".png", tmp_dir / "banner")
            self.assertEqual(banner_png.name, "banner.png")
            with Image.open(banner_png) as im_ban:
                self.assertEqual(im_ban.size, (1500, 300))

    def test_bepinex_bridge_discovery_paths(self) -> None:
        from unittest.mock import patch
        from app.core import config as core_config
        from app.routers import asset_audit

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

        # Verify %LOCALAPPDATA%\TheModdingTool\tools\bepinex-mcp\ fallback works when workspace bridge is absent
        with tempfile.TemporaryDirectory() as tmp:
            fake_ws = Path(tmp) / "ws"
            fake_tools = Path(tmp) / "tools"
            fake_game = Path(tmp) / "game"
            (fake_ws).mkdir()
            (fake_game).mkdir()
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

    def test_offline_page_service_worker_and_ui_controls(self) -> None:
        r_off = self.client.get("/offline")
        self.assertEqual(r_off.status_code, 200)
        for marker in (
            "The Modding Tool — Offline",
            "moddingtool://start",
            "Start Server",
            "Retry Connection",
            "/api/health",
        ):
            self.assertIn(marker, r_off.text)

        r_sw = self.client.get("/sw.js")
        self.assertEqual(r_sw.status_code, 200)
        self.assertEqual(r_sw.headers.get("service-worker-allowed"), "/")
        self.assertIn("javascript", r_sw.headers.get("content-type", "").lower())
        self.assertIn("/offline", r_sw.text)
        self.assertIn("/static/app.css", r_sw.text)

        r_idx = self.client.get("/")
        self.assertEqual(r_idx.status_code, 200)
        for ui_marker in (
            "btn-header-stop-server",
            "btn-am-stop-server",
            "btn-register-protocol",
            "stopAppServer()",
            "registerProtocolLauncher()",
        ):
            self.assertIn(ui_marker, r_idx.text)

        main_js = self.client.get("/static/js/main.js").text
        self.assertIn("/sw.js", main_js)

        app_mgmt_js = self.client.get("/static/js/app_mgmt.js").text
        self.assertIn("/api/system/shutdown", app_mgmt_js)
        self.assertIn("/api/system/register-protocol", app_mgmt_js)
        self.assertIn('"/offline"', app_mgmt_js)

        bat_text = (ROOT / "start_app.bat").read_text(encoding="utf-8")
        self.assertIn("--no-browser", bat_text)
        self.assertIn("8765", bat_text)

    def test_system_shutdown_and_protocol_registration(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.core import audit as core_audit
        from app.routers import system as system_router

        self.assertEqual(self.client.post("/api/system/shutdown").status_code, 403)
        self.assertEqual(self.client.post("/api/system/register-protocol").status_code, 403)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"

            written_keys: list[tuple[str, str, object]] = []

            class FakeRegKey:
                def __init__(self, subkey: str) -> None:
                    self.subkey = subkey

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            fake_winreg = SimpleNamespace(
                HKEY_CURRENT_USER="HKCU",
                KEY_SET_VALUE=2,
                REG_SZ=1,
                CreateKeyEx=lambda root, subkey, res, access: FakeRegKey(subkey),
                SetValueEx=lambda key, name, reserved, reg_type, value: written_keys.append(
                    (key.subkey, name, value)
                ),
            )

            with (
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
                patch.object(system_router, "_schedule_process_shutdown") as mock_sched,
                patch.dict(sys.modules, {"winreg": fake_winreg}),
                patch.object(sys, "platform", "win32"),
            ):
                r_shut = self.client.post(
                    "/api/system/shutdown",
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_shut.status_code, 200)
                self.assertEqual(r_shut.json(), {"ok": True})
                mock_sched.assert_called_once()

                r_proto = self.client.post(
                    "/api/system/register-protocol",
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_proto.status_code, 200)
                pdata = r_proto.json()
                self.assertTrue(pdata.get("ok"))
                self.assertEqual(pdata.get("registry_path"), r"HKCU\Software\Classes\moddingtool")
                self.assertIn("start_app.bat", pdata.get("command", ""))
                self.assertIn("--no-browser", pdata.get("command", ""))
                self.assertNotIn("%1", pdata.get("command", ""))

            self.assertIn((r"Software\Classes\moddingtool", "URL Protocol", ""), written_keys)
            cmd_entries = [
                v for k, n, v in written_keys
                if k == r"Software\Classes\moddingtool\shell\open\command" and n == ""
            ]
            self.assertEqual(len(cmd_entries), 1)
            self.assertIn("--no-browser", str(cmd_entries[0]))
            self.assertNotIn("%1", str(cmd_entries[0]))

            logged = [json.loads(ln) for ln in temp_audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertTrue(any(e.get("action") == "SERVER_STOPPED" for e in logged))
            self.assertTrue(any(e.get("action") == "app.protocol.register" for e in logged))

    def test_unreal_metadata_rescan_and_ini_settings(self) -> None:
        from unittest.mock import patch
        from app.core import audit as core_audit
        from app.core import config as core_config
        from app.routers import library as library_router
        from app.routers import system as system_router

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

    def test_unity_bridge_discovery_and_all_router_endpoints(self) -> None:
        from unittest.mock import patch
        from app.core import audit as core_audit
        from app.core import config as core_config
        from app.routers import branding as branding_router
        from app.routers import library as library_router
        from app.routers import mcp_bridge as mcp_router

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"
            temp_ui_cfg = tmp_dir / "app_ui_config.json"
            temp_ui_settings = tmp_dir / "ui_settings.json"
            temp_built_mods = tmp_dir / "built_mods"
            temp_games_store = tmp_dir / "managed_games.json"

            # Mock Unity IL2CPP game for fingerprint & bridge discovery
            unity_game = tmp_dir / "UnityIl2CppSample"
            (unity_game / "UnitySample_Data" / "il2cpp_data" / "Metadata").mkdir(parents=True)
            (unity_game / "GameAssembly.dll").write_bytes(b"MZ-IL2CPP")
            (unity_game / "UnitySample_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(b"META")

            # Mock Unreal game for /api/mods/build (uses make_slug + inspect_game_modding_info)
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
                # 1. library router
                self.assertEqual(self.client.get("/api/games").status_code, 200)
                self.assertEqual(self.client.get("/api/games/scan-progress").status_code, 200)
                self.assertEqual(self.client.get("/api/library").status_code, 200)

                # 2. asset_audit router (+ bridges/bepinex-mcp discovery)
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

                # 3. mcp_bridge router
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

                # 4. git_sync router
                self.assertEqual(self.client.get("/api/app/backups").status_code, 200)
                self.assertEqual(self.client.get("/api/app/repo-config").status_code, 200)
                self.assertEqual(self.client.get("/api/app/git-status").status_code, 200)

                # 5. branding router
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

                # 6. system router
                self.assertEqual(self.client.get("/api/defaults").status_code, 200)
                self.assertEqual(self.client.get("/api/app/diagnostics").status_code, 200)
                r_tools = self.client.get("/api/app/tools/status")
                self.assertEqual(r_tools.status_code, 200)
                tools_by_id = {t["id"]: t for t in r_tools.json().get("tools", [])}
                self.assertIn("bridges\\bepinex-mcp", tools_by_id["bepinex_mcp_plugin"]["path"])
                self.assertEqual(self.client.get("/api/app/dev-mode").status_code, 200)

    def test_non_game_exclusions_and_unreal_editor_ini_filter(self) -> None:
        import os
        from unittest.mock import patch
        from app.core import audit as core_audit
        from app.core import config as core_config
        from app.routers import library as library_router

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            # 1. Mock Steam library with 228980, Dedicated Server, and a real game
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

            # 2. Direct is_non_game_entry & custom-folder /api/games/scan checks
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

            # 3. discover_local_unreal_ini_profiles excludes non-game LOCALAPPDATA roots & WindowsEditor
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
        from unittest.mock import patch
        from app.core import audit as core_audit
        from app.core import config as core_config
        from app.routers import library as library_router
        from app.routers import system as system_router

        html = self.client.get("/").text
        for marker in (
            "header-quick-load",
            "global-game-select",
            "onGlobalGameSelected()",
            "info-slug-badge",
            "info-engine-badge",
        ):
            self.assertIn(marker, html)

        lib_js = self.client.get("/static/js/library.js").text
        self.assertIn("export function renderActiveGamePanel", lib_js)
        self.assertIn("import { ENTITIES, VARIABLES, syncModBuilderWithGame }", lib_js)
        self.assertIn("renderActiveGamePanel(localGame", lib_js)
        self.assertIn("renderActiveGamePanel(data.game)", lib_js)

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

    def test_js_dom_ids_es_module_contracts_and_cache_busting(self) -> None:
        import re
        from app.main import get_asset_version

        expected_ver = get_asset_version()
        r_idx = self.client.get("/")
        self.assertEqual(r_idx.status_code, 200)
        html = r_idx.text
        self.assertNotIn("{{ asset_version }}", html)
        self.assertIn(f"/static/app.css?v={expected_ver}", html)
        self.assertIn(f"/static/js/main.js?v={expected_ver}", html)

        r_sw = self.client.get("/sw.js")
        self.assertEqual(r_sw.status_code, 200)
        self.assertIn("modtool-offline-v2", r_sw.text)
        self.assertIn('url.pathname.startsWith("/static/")', r_sw.text)

        raw_template = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        html_ids = set(re.findall(r"""\bid=["']([^"']+)["']""", raw_template))

        js_dir = ROOT / "app" / "static" / "js"
        for js_path in sorted(js_dir.glob("*.js")):
            txt = js_path.read_text(encoding="utf-8")
            for m in re.finditer(r"""getElementById\(\s*["']([^"']+)["']\s*\)""", txt):
                el_id = m.group(1)
                self.assertIn(el_id, html_ids, f"{js_path.name} references missing DOM id '{el_id}'")

            for m in re.finditer(r"""import\s+\{([^}]+)\}\s+from\s+["']\./([^"']+)["']""", txt):
                target_file = js_dir / m.group(2)
                self.assertTrue(target_file.is_file(), f"{js_path.name} imports missing module '{m.group(2)}'")
                target_txt = target_file.read_text(encoding="utf-8")
                exports = set(
                    re.findall(r"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z0-9_$]+)", target_txt)
                )
                for raw_sym in m.group(1).split(","):
                    sym = raw_sym.strip().split(" as ")[0].strip()
                    if sym:
                        self.assertIn(sym, exports, f"{js_path.name} imports missing export '{sym}' from '{m.group(2)}'")

        lib_txt = (js_dir / "library.js").read_text(encoding="utf-8")
        self.assertIn("function _classifyGameEngine(game)", lib_txt)
        self.assertIn("unreal-settings-section", lib_txt)
        self.assertIn("card-provision-restore", lib_txt)
        self.assertIn("card-live-mcp", lib_txt)

    def test_atomic_write_json_no_orphan_temp_files(self) -> None:
        import os
        from unittest.mock import patch
        from app.core import config as core_config

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            target = tmp_dir / "managed_games.json"

            payload_v1 = {"active_game_id": "g1", "games": [{"id": "g1", "name": "Game One"}]}
            core_config.atomic_write_json(target, payload_v1)
            self.assertTrue(target.is_file())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload_v1)
            self.assertEqual(list(tmp_dir.iterdir()), [target])

            payload_v2 = {"active_game_id": "g2", "games": [{"id": "g2", "name": "Game Two"}]}
            core_config.atomic_write_json(target, payload_v2)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload_v2)
            self.assertEqual(list(tmp_dir.iterdir()), [target])

            with patch.object(os, "replace", side_effect=OSError("simulated swap failure")):
                with self.assertRaises(OSError):
                    core_config.atomic_write_json(target, {"corrupt": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload_v2)
            self.assertEqual(list(tmp_dir.iterdir()), [target])

    def test_changelog_exists_and_append_helper(self) -> None:
        import re

        cl_path = ROOT / "CHANGELOG.md"
        self.assertTrue(cl_path.is_file())
        cl_text = cl_path.read_text(encoding="utf-8")
        self.assertRegex(cl_text, r"(?m)^## \d{4}-\d{2}-\d{2} \d{2}:\d{2} — v\d+\.\d+\.\d+")
        self.assertIn("**Prompt**:", cl_text)
        self.assertIn("**AG Feedback**:", cl_text)
        self.assertIn("**Version Changes**:", cl_text)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_cl = Path(tmp) / "CHANGELOG.md"
            version_manager.append_changelog_entry(
                prompt="Test prompt one",
                feedback="AG verified change one",
                changes=["Updated file_a.py"],
                version="0.1.40",
                timestamp="2026-11-25 11:00",
                changelog_path=tmp_cl,
            )
            version_manager.append_changelog_entry(
                prompt="Test prompt two",
                feedback="AG verified change two",
                changes=["Updated file_b.py"],
                version="0.1.41",
                timestamp="2026-11-25 11:25",
                changelog_path=tmp_cl,
            )
            out = tmp_cl.read_text(encoding="utf-8")
            idx_new = out.find("## 2026-11-25 11:25 — v0.1.41")
            idx_old = out.find("## 2026-11-25 11:00 — v0.1.40")
            self.assertNotEqual(idx_new, -1)
            self.assertNotEqual(idx_old, -1)
            self.assertLess(idx_new, idx_old)
            self.assertIn("**Prompt**: Test prompt two", out)
            self.assertIn("**AG Feedback**: AG verified change two", out)
            self.assertIn("- Updated file_b.py", out)

    def test_github_branch_pull_and_push_confirm_flow(self) -> None:
        import urllib.request
        from unittest.mock import MagicMock, patch
        from app.core import audit as core_audit
        from app.routers import git_sync

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / ".git").mkdir()
            (repo_dir / ".gitignore").write_text(".venv/\n__pycache__/\n*.pyc\n.env\n*.log\n", encoding="utf-8")

            fake_cfg = {
                "owner": "octocat",
                "repo": "modtool",
                "branch": "main",
                "token": "ghp_secret123",
                "installed_sha": "",
                "last_checked": "",
            }

            class _FakeResp:
                def __init__(self, payload: object) -> None:
                    self._raw = json.dumps(payload).encode("utf-8")

                def read(self) -> bytes:
                    return self._raw

                def __enter__(self) -> "_FakeResp":
                    return self

                def __exit__(self, *args: object) -> bool:
                    return False

            def fake_run_git(git_bin, args, cwd, **kwargs):
                joined = " ".join(args)
                if "rev-parse --abbrev-ref HEAD" in joined:
                    return (0, "main\n", "")
                if "refs/heads/" in joined:
                    return (0, "main\nlocal-exp\n", "")
                if "refs/remotes/origin/" in joined:
                    return (0, "origin/HEAD\norigin/main\n", "")
                return (0, "", "")

            temp_audit = repo_dir / "audit.jsonl"
            temp_app_audit = repo_dir / "audit_app.jsonl"
            temp_game_audit = repo_dir / "audit_game.jsonl"

            with (
                patch.object(git_sync, "load_repo_config", return_value=dict(fake_cfg)),
                patch.object(git_sync, "save_repo_config", MagicMock()),
                patch.object(git_sync, "_resolve_git_app_root", return_value=repo_dir),
                patch.object(git_sync, "_run_git_cmd", side_effect=fake_run_git),
                patch.object(
                    urllib.request,
                    "urlopen",
                    return_value=_FakeResp([{"name": "main"}, {"name": "feature/ui"}, {"name": "release"}]),
                ),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                r_st = self.client.get("/api/app/git-status")
                self.assertEqual(r_st.status_code, 200)
                st = r_st.json()
                self.assertIn("main", st.get("branches", []))
                self.assertIn("local-exp", st.get("branches", []))

                r_br = self.client.post(
                    "/api/app/git-branches",
                    json={"owner": "octocat", "repo": "modtool"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_br.status_code, 200)
                br = r_br.json()
                self.assertEqual(br.get("status"), "ok")
                self.assertEqual(br.get("remote_branches"), ["main", "feature/ui", "release"])
                self.assertEqual(br.get("branches"), ["main", "feature/ui", "release", "local-exp"])

            captured_push_args: list[list[str]] = []

            def fake_push_step(step_name, args, cwd, **kwargs):
                if step_name == "remote":
                    return {"step": step_name, "returncode": 0, "stdout": "https://github.com/octocat/modtool.git\n", "stderr": ""}
                if step_name == "rev_parse_branch":
                    return {"step": step_name, "returncode": 0, "stdout": "main\n", "stderr": ""}
                if step_name == "diff_cached":
                    return {"step": step_name, "returncode": 1, "stdout": "", "stderr": ""}
                if step_name == "rev_parse":
                    return {"step": step_name, "returncode": 0, "stdout": "abc1234def5678\n", "stderr": ""}
                if step_name == "push":
                    captured_push_args.append(list(args))
                return {"step": step_name, "returncode": 0, "stdout": "", "stderr": ""}

            with (
                patch.object(git_sync, "load_repo_config", return_value=dict(fake_cfg)),
                patch.object(git_sync, "save_repo_config", MagicMock()),
                patch.object(git_sync, "_resolve_git_app_root", return_value=repo_dir),
                patch.object(git_sync, "run_git_step", side_effect=fake_push_step),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
            ):
                r_push = self.client.post(
                    "/api/app/git-push",
                    json={"commit_message": "push branch test", "branch": "feature/ui"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_push.status_code, 200)
                self.assertEqual(r_push.json().get("branch"), "feature/ui")
                self.assertEqual(captured_push_args, [["push", "-u", "origin", "HEAD:feature/ui"]])

        html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="btn-gh-pull-branches"', html)
        self.assertIn('id="gh-push-branch"', html)
        self.assertIn('id="gh-push-confirm-overlay"', html)
        self.assertIn('id="btn-gh-push-cancel"', html)
        self.assertIn("Cancel Push", html)
        self.assertIn('id="btn-gh-push-confirm"', html)

        js = (ROOT / "app" / "static" / "js" / "app_mgmt.js").read_text(encoding="utf-8")
        self.assertIn("export async function pullGitHubBranches()", js)
        self.assertIn("export function closeGitPushConfirmModal()", js)
        self.assertIn("export async function confirmPushAppToGitHub()", js)
        self.assertIn("`Push to ${branch}`", js)


if __name__ == "__main__":
    unittest.main()

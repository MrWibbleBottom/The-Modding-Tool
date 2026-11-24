"""
Automated smoke tests for ModToolPoC FastAPI controller, caching/ETag,
mod builder (including Harmony hooks), version manager, and backup restore.
"""

from __future__ import annotations

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
            "/static/app.css",
            "/static/js/main.js",
            'name="modtool-session-token"',
        ):
            self.assertIn(marker, html)

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
        latest_id = versions[-1]["version_id"]
        with tempfile.TemporaryDirectory() as tmp:
            backup_manager.restore_version(latest_id, Path(tmp))
            self.assertTrue((Path(tmp) / "poc-ui" / "controller.py").is_file())
            self.assertTrue((Path(tmp) / "poc-ui" / "index.html").is_file())


if __name__ == "__main__":
    unittest.main()

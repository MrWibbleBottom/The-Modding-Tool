"""Security, audit-error redaction, session middleware, and atomic write tests."""

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
from app.routers import system as system_router  # noqa: E402


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

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

    def test_action_failed_audit_and_client_error(self) -> None:
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

                r_404 = self.client.get("/api/does-not-exist-audit-test")
                self.assertEqual(r_404.status_code, 404)
                d_404 = r_404.json()
                self.assertFalse(d_404.get("ok"))
                self.assertIn("message", d_404)
                self.assertIn("hint", d_404)
                self.assertIn("detail", d_404)

                r_403 = self.client.post("/api/audit/client-error", json={"message": "no token"})
                self.assertEqual(r_403.status_code, 403)
                d_403 = r_403.json()
                self.assertFalse(d_403.get("ok"))
                self.assertIn("message", d_403)
                self.assertIn("hint", d_403)
                self.assertIn("detail", d_403)

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
        css_resp = self.client.get("/static/app.css")
        self.assertEqual(css_resp.status_code, 200)
        for css_marker in (".action-status-slot", ".is-running", ".is-success", ".is-error", ".action-status-copy"):
            self.assertIn(css_marker, css_resp.text)

        for js_name in ("api.js", "analyzer.js", "game_table.js", "active_game.js", "unreal_panel.js", "mcp.js", "git_backup.js", "app_mgmt.js"):
            js_resp = self.client.get(f"/static/js/{js_name}")
            self.assertEqual(js_resp.status_code, 200)
            self.assertIn("runTrackedAction", js_resp.text)
        self.assertIn("/api/audit/client-error", self.client.get("/static/js/api.js").text)

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

    def test_atomic_write_json_no_orphan_temp_files(self) -> None:
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


if __name__ == "__main__":
    unittest.main()

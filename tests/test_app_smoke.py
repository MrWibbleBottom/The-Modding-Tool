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

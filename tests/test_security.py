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

    def test_game_snapshot_path_traversal_rejection(self) -> None:
        from app.services import game_snapshots

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            snaps_root = tmp_dir / "game_snapshots"
            game_dir = tmp_dir / "SampleGame"
            (game_dir / "BepInEx" / "plugins").mkdir(parents=True)
            (game_dir / "doorstop_config.ini").write_text("[General]\nenabled=true\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                game_snapshots.ensure_within(tmp_dir / "outside.txt", game_dir)
            with self.assertRaises(ValueError):
                game_snapshots.ensure_within(game_dir / ".." / "outside.txt", game_dir)

            snap = game_snapshots.create_game_snapshot(
                "sample_game",
                game_dir,
                label="Baseline",
                engine="unity",
                snapshots_root=snaps_root,
                audit=False,
            )
            snap_id = snap["snapshot_id"]
            manifest_path = Path(snap["snapshot_dir"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            # Tamper rel_path to attempt writing outside game_dir
            manifest["files"]["game/doorstop_config.ini"]["rel_path"] = "../../evil.ini"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                game_snapshots.restore_game_snapshot(
                    "sample_game",
                    snap_id,
                    target_game_dir=game_dir,
                    snapshots_root=snaps_root,
                    audit=False,
                )

            # Invalid slug or snapshot_id segments must be rejected via HTTP 400
            r_bad_snap = self.client.post(
                "/api/games/sample_game/snapshots/bad..id/restore",
                headers={SESSION_HEADER: get_session_token()},
            )
            self.assertEqual(r_bad_snap.status_code, 400)

    def test_sanitize_legacy_paths_and_appdata_sanitization(self) -> None:
        raw_plugin = r"D:\Downloads\Battleground\bepinex-mcp\plugins\BepInExMCP.IL2CPP\bin\Release\net6.0"
        expected_plugin = r"D:\Downloads\The-Modding-Tool\bridges\bepinex-mcp\plugins\BepInExMCP.IL2CPP\bin\Release\net6.0"
        self.assertEqual(core_config.sanitize_legacy_paths(raw_plugin), expected_plugin)

        sample_obj = {
            "mcp_app": "D:/Downloads/Battleground/bepinex-mcp/mcp/ModdersHelperApp.py",
            "venv_python": r"D:\Downloads\Battleground\bepinex-mcp\.venv\Scripts\python.exe",
            "app_data": r"C:\Users\demo\AppData\Local\ModToolPoC\managed_games.json",
            "tools_dir": r"C:\Users\demo\AppData\Local\TheModdingTool\tools\bepinex-mcp",
            "release_url": "https://github.com/rkuhn153/bepinex-mcp",
            "nested": [raw_plugin],
        }
        cleaned = core_config.sanitize_legacy_paths(sample_obj)
        self.assertEqual(
            cleaned["mcp_app"],
            "D:/Downloads/The-Modding-Tool/bridges/bepinex-mcp/mcp/ModdersHelperApp.py",
        )
        self.assertEqual(cleaned["venv_python"], r"D:\Downloads\The-Modding-Tool\.venv\Scripts\python.exe")
        self.assertEqual(cleaned["app_data"], r"C:\Users\demo\AppData\Local\TheModdingTool\managed_games.json")
        self.assertEqual(cleaned["tools_dir"], r"C:\Users\demo\AppData\Local\TheModdingTool\tools\bepinex-mcp")
        self.assertEqual(cleaned["release_url"], "https://github.com/rkuhn153/bepinex-mcp")
        self.assertEqual(cleaned["nested"], [expected_plugin])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            legacy_dir = tmp_dir / "ModToolPoC"
            new_dir = tmp_dir / "TheModdingTool"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            stale_cfg = {
                "active_game_id": "battleground",
                "games": [
                    {
                        "id": "battleground",
                        "plugin_build_dir": raw_plugin,
                        "venv_python": r"D:\Downloads\Battleground\bepinex-mcp\.venv\Scripts\python.exe",
                    }
                ],
            }
            (legacy_dir / "managed_games.json").write_text(json.dumps(stale_cfg), encoding="utf-8")
            migrated = core_config.migrate_legacy_appdata(new_dir, legacy_dir)
            self.assertTrue(migrated)
            migrated_data = json.loads((new_dir / "managed_games.json").read_text(encoding="utf-8"))
            self.assertEqual(migrated_data["games"][0]["plugin_build_dir"], expected_plugin)
            self.assertEqual(
                migrated_data["games"][0]["venv_python"],
                r"D:\Downloads\The-Modding-Tool\.venv\Scripts\python.exe",
            )

            # Also verify load_managed_games() auto-cleans and atomically persists stale JSON on disk
            store_file = new_dir / "managed_games.json"
            store_file.write_text(json.dumps(stale_cfg), encoding="utf-8")
            with patch.object(core_config, "GAMES_STORE_PATH", store_file):
                loaded = core_config.load_managed_games()
            self.assertEqual(loaded["games"][0]["plugin_build_dir"], expected_plugin)
            persisted = json.loads(store_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["games"][0]["plugin_build_dir"], expected_plugin)

    def test_ct_xml_xxe_dtd_rejection_and_import_path_traversal(self) -> None:
        from app.routers import library as library_router
        from app.services import endpoint_catalog, endpoints as endpoints_service

        xxe_ct = """<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">
]>
<CheatTable>
  <CheatEntries>
    <CheatEntry>
      <Description>"&xxe;"</Description>
      <VariableType>4 Bytes</VariableType>
    </CheatEntry>
  </CheatEntries>
</CheatTable>
"""
        with self.assertRaises(ValueError):
            endpoint_catalog.parse_cheat_engine_ct(xxe_ct, file_name="evil.ct")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            game_dir = tmp_dir / "SecGame"
            game_dir.mkdir(parents=True, exist_ok=True)
            (tmp_dir / "outside.ct").write_text("<CheatTable><CheatEntries/></CheatTable>", encoding="utf-8")

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "sec_ct_game",
                        "name": "Security CT Game",
                        "game_dir": str(game_dir),
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
            ):
                core_config.invalidate_games_cache()
                r_xxe = self.client.post(
                    "/api/games/sec_ct_game/endpoints/import-community",
                    json={"filename": "evil.ct", "content": xxe_ct},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_xxe.status_code, 400)
                self.assertIn("forbidden", r_xxe.json().get("detail", "").lower())

                r_trav = self.client.post(
                    "/api/games/sec_ct_game/endpoints/import-community",
                    json={"file_path": "../../outside.ct"},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_trav.status_code, 400)
                core_config.invalidate_games_cache()

    def test_mod_vault_zip_slip_and_fomod_xxe_rejection(self) -> None:
        import io
        import zipfile
        from app.core.security import ensure_within
        from app.routers import library as library_router
        from app.services import endpoints as endpoints_service, mod_auditor

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault_base = tmp_dir / "vault"
            vault_base.mkdir()
            with self.assertRaises(ValueError):
                ensure_within(vault_base / "../../evil.dll", vault_base)

            appdata_root = tmp_dir / "appdata"
            game_dir = tmp_dir / "VaultSecGame"
            game_dir.mkdir(parents=True, exist_ok=True)
            slug = "vault_sec_game"

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": slug,
                        "name": "Vault Security Game",
                        "game_dir": str(game_dir),
                        "runtime": "Unity_IL2CPP",
                    }]
                }),
                encoding="utf-8",
            )

            # Zip-Slip payload
            slip_buf = io.BytesIO()
            with zipfile.ZipFile(slip_buf, "w") as zf:
                zf.writestr("../../evil.dll", b"MZ-EVIL")

            # Malicious XXE inside FOMOD ModuleConfig.xml
            xxe_buf = io.BytesIO()
            with zipfile.ZipFile(xxe_buf, "w") as zf:
                zf.writestr(
                    "fomod/ModuleConfig.xml",
                    '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">]><config><moduleName>&xxe;</moduleName></config>',
                )

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(mod_auditor, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
            ):
                core_config.invalidate_games_cache()

                r_slip = self.client.post(
                    f"/api/games/{slug}/stored-mods/upload",
                    files={"file": ("zipslip.zip", slip_buf.getvalue(), "application/zip")},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_slip.status_code, 400)
                self.assertFalse((tmp_dir / "evil.dll").exists())
                self.assertFalse((appdata_root / "evil.dll").exists())

                r_xxe = self.client.post(
                    f"/api/games/{slug}/stored-mods/upload",
                    files={"file": ("evil_fomod.zip", xxe_buf.getvalue(), "application/zip")},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_xxe.status_code, 400)
                self.assertIn("doctype", r_xxe.json().get("detail", "").lower())
                core_config.invalidate_games_cache()

    def test_mod_vault_large_pak_passes_and_zip_bomb_rejected(self) -> None:
        import io
        import zipfile
        import zlib
        from app.core import constants as core_const
        from app.core.security import SESSION_HEADER, get_session_token
        from app.services import endpoints as endpoints_service, mod_auditor

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            appdata_root = tmp_dir / "appdata"
            game_dir = tmp_dir / "LargePakUnreal"
            (game_dir / "BendGame" / "Content" / "Paks" / "~mods").mkdir(parents=True, exist_ok=True)
            slug = "large_pak_unreal"

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": slug,
                        "name": "Large Pak Unreal",
                        "game_dir": str(game_dir),
                        "runtime": "Unreal",
                        "detected_engine": "Unreal",
                    }]
                }),
                encoding="utf-8",
            )

            # Build small valid zip containing a small ini and a simulated 650 MB .pak header (normal ~4.3:1 ratio)
            pak_payload = (
                b"../../../\x00"
                b"BendGame/Content/Player/Mesh/SK_Deacon.uasset\x00"
                + zlib.compress(b"\x00JacketTintScale\x00")
            )
            good_buf = io.BytesIO()
            with zipfile.ZipFile(good_buf, "w") as zf:
                zf.writestr("ModConfig.ini", b"[/Game/Mod]\nIntensity=2.5\n")
                zf.writestr("MegaTextures_P.pak", pak_payload)

            bomb_buf = io.BytesIO()
            with zipfile.ZipFile(bomb_buf, "w") as zf:
                zf.writestr("Bomb_P.pak", b"0" * 1024)

            orig_zipfile = zipfile.ZipFile

            class _MockSizedZipFile(orig_zipfile):
                def __init__(self, file, *args, **kwargs):
                    super().__init__(file, *args, **kwargs)
                    self._mode_tag = "good" if "good_large" in str(file) else ("bomb" if "zip_bomb" in str(file) else "")

                def infolist(self):
                    infos = super().infolist()
                    for info in infos:
                        if self._mode_tag == "good" and info.filename.endswith(".pak"):
                            info.file_size = 650 * 1024 * 1024
                            info.compress_size = 150 * 1024 * 1024
                        elif self._mode_tag == "bomb" and info.filename.endswith(".pak"):
                            info.file_size = 600 * 1024 * 1024
                            info.compress_size = 1 * 1024 * 1024
                    return infos

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(mod_auditor, "APP_DATA_ROOT", appdata_root),
                patch.object(endpoints_service, "APP_DATA_ROOT", appdata_root),
                patch.object(mod_auditor.zipfile, "ZipFile", _MockSizedZipFile),
            ):
                core_config.invalidate_games_cache()

                # 1. 650 MB uncompressed / 150 MB compressed -> allowed & audited via stream without unpacking .pak
                r_ok = self.client.post(
                    f"/api/games/{slug}/stored-mods/upload",
                    files={"files": ("good_large_650mb.zip", good_buf.getvalue(), "application/zip")},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_ok.status_code, 200, r_ok.text)
                rep = r_ok.json()["audit_report"]
                unpacked_root = Path(rep["vault_dir"]) / "unpacked"
                self.assertTrue((unpacked_root / "ModConfig.ini").is_file())
                self.assertFalse((unpacked_root / "MegaTextures_P.pak").exists())
                self.assertTrue(rep.get("archived_entries"))
                self.assertEqual(rep["mount_points"], ["../../../"])
                self.assertIn("JacketTintScale", rep["uproperty_symbols"])
                self.assertTrue(any(a["asset_name"] == "SK_Deacon.uasset" for a in rep["active_assets"]))

                # 2. 600 MB uncompressed / 1 MB compressed (600:1 > MAX_ZIP_BOMB_RATIO 100:1) -> rejected
                self.assertEqual(core_const.MAX_ZIP_BOMB_RATIO, 100.0)
                r_bomb = self.client.post(
                    f"/api/games/{slug}/stored-mods/upload",
                    files={"files": ("zip_bomb_600to1.zip", bomb_buf.getvalue(), "application/zip")},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_bomb.status_code, 400, r_bomb.text)
                self.assertIn("zip-bomb", r_bomb.json().get("detail", "").lower())
                core_config.invalidate_games_cache()


if __name__ == "__main__":
    unittest.main()

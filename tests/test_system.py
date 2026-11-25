"""System endpoints, Git sync/push, backup/restore, branding, changelog, and architecture tests."""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.core import audit as core_audit  # noqa: E402
from app.core import constants  # noqa: E402
from app.core.security import SESSION_HEADER, get_session_token  # noqa: E402
from app.engines import (  # noqa: E402
    GENERIC_ADAPTER,
    UNITY_ADAPTER,
    UNREAL_ADAPTER,
    EngineAdapter,
    detect_engine_for_dir,
)
from app.main import app  # noqa: E402
from app.routers import branding as branding_router  # noqa: E402
from app.routers import git_sync  # noqa: E402
from app.routers import system as system_router  # noqa: E402
from app.services import jobs as job_service  # noqa: E402
from app.services import discovery, game_store, mod_builder, pc_specs, tool_installer  # noqa: E402
import backup_manager  # noqa: E402
import package_windows  # noqa: E402
import version_manager  # noqa: E402


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

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

    def test_git_push_up_to_date_and_failure_audit(self) -> None:
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

    def test_branding_upload_pillow_cropping(self) -> None:
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

    def test_system_shutdown_and_protocol_registration(self) -> None:
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

    def test_changelog_exists_and_append_helper(self) -> None:
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

        html = self.client.get("/").text
        self.assertIn('id="btn-gh-pull-branches"', html)
        self.assertIn('id="gh-push-branch"', html)
        self.assertIn('id="gh-push-confirm-overlay"', html)
        self.assertIn('id="btn-gh-push-cancel"', html)
        self.assertIn("Cancel Push", html)
        self.assertIn('id="btn-gh-push-confirm"', html)

        js = (ROOT / "app" / "static" / "js" / "git_backup.js").read_text(encoding="utf-8")
        app_mgmt_js = (ROOT / "app" / "static" / "js" / "app_mgmt.js").read_text(encoding="utf-8")
        self.assertIn("export async function pullGitHubBranches()", js)
        self.assertIn("export function closeGitPushConfirmModal()", js)
        self.assertIn("export async function confirmPushAppToGitHub()", js)
        self.assertIn("`Push to ${branch}`", js)
        self.assertIn("pullGitHubBranches", app_mgmt_js)
        self.assertIn("confirmPushAppToGitHub", app_mgmt_js)

    def test_refactored_architecture_and_packaging_bundles(self) -> None:
        for const_name in (
            "_UE_QUALITY_LABELS",
            "NON_GAME_STEAM_APPIDS",
            "NON_GAME_NAME_PATTERNS",
            "NON_GAME_LOCALAPPDATA_FOLDERS",
            "ENGINE_TOOL_MATRIX",
            "PLUGIN_DEPLOY_FILES",
        ):
            self.assertTrue(hasattr(constants, const_name), f"Missing shared constant {const_name}")

        for adapter in (UNITY_ADAPTER, UNREAL_ADAPTER, GENERIC_ADAPTER):
            self.assertIsInstance(adapter, EngineAdapter)
            for method_name in ("detect", "fingerprint", "discover_configs", "check_readiness", "get_log_paths"):
                self.assertTrue(callable(getattr(adapter, method_name)))

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            self.assertEqual(detect_engine_for_dir(tdir).engine_id, "generic")

        for mod in (discovery, game_store, mod_builder, pc_specs, tool_installer):
            self.assertIsNotNone(mod)

        for rfile in (ROOT / "app" / "routers").glob("*.py"):
            src = rfile.read_text(encoding="utf-8")
            self.assertNotIn("app.routers", src, f"{rfile.name} should not cross-import another router")

        cmd = package_windows.build_pyinstaller_command()
        self.assertTrue(any("app/engines" in arg for arg in cmd))
        self.assertTrue(any("app/services" in arg for arg in cmd))
        self.assertTrue(any("app/templates/partials" in arg for arg in cmd))
        self.assertIn("app.engines", cmd)
        self.assertIn("app.services", cmd)

    def test_background_job_runner_expiry_and_api(self) -> None:
        j = job_service.create_job("unit_test", "Starting")
        jid = j["job_id"]
        self.assertEqual(j["status"], "running")
        job_service.update_job(jid, status="running", percent=55, stage="Working", log_line="tick 1")
        snap = job_service.get_job(jid)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["percent"], 55)
        self.assertEqual(snap["log_lines"], ["tick 1"])

        job_service.update_job(jid, status="done", percent=100, stage="Done", result={"ok": True})
        r_get = self.client.get(f"/api/jobs/{jid}")
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.json().get("status"), "done")
        self.assertEqual(r_get.json().get("result"), {"ok": True})

        r_missing = self.client.get("/api/jobs/nonexistent_job_id_xyz")
        self.assertEqual(r_missing.status_code, 404)

        removed = job_service.prune_expired_jobs(
            max_age_seconds=1800,
            now_monotonic=time.monotonic() + 1805,
        )
        self.assertGreaterEqual(removed, 1)
        self.assertIsNone(job_service.get_job(jid))

        bg = job_service.start_background_job(
            "async_test",
            lambda report: (report(percent=40, stage="Step"), {"value": 99})[1],
            initial_stage="Queued",
        )
        deadline = time.monotonic() + 5.0
        final_job = None
        while time.monotonic() < deadline:
            r_poll = self.client.get(f"/api/jobs/{bg['job_id']}")
            self.assertEqual(r_poll.status_code, 200)
            payload = r_poll.json()
            if payload.get("status") in ("done", "error"):
                final_job = payload
                break
            time.sleep(0.05)
        self.assertIsNotNone(final_job)
        self.assertEqual(final_job["status"], "done")
        self.assertEqual(final_job["result"], {"value": 99})


if __name__ == "__main__":
    unittest.main()

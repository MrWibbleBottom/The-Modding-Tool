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
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.core import audit as core_audit  # noqa: E402
from app.core import config as core_config  # noqa: E402
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
from app.routers import library as library_router  # noqa: E402
from app.routers import system as system_router  # noqa: E402
from app.services import game_snapshots  # noqa: E402
from app.services import jobs as job_service  # noqa: E402
from app.services import discovery, game_store, mod_builder, pc_optimizer, pc_specs, tool_installer  # noqa: E402
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

    def test_verify_integrity_pc_optimisation_full_restore_and_ai_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ue_dir = tmp_dir / "OptUnrealGame"
            cfg_dir = ue_dir / "Saved" / "Config" / "WindowsNoEditor"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            gus_ini = cfg_dir / "GameUserSettings.ini"
            gus_ini.write_text(
                "[/Script/Engine.GameUserSettings]\nResolutionSizeX=3840\nResolutionSizeY=2160\nbUseVSync=True\n"
                "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.GlobalIlluminationQuality=3\nsg.TextureQuality=3\n",
                encoding="utf-8",
            )
            (ue_dir / "Saved" / "Logs").mkdir(parents=True, exist_ok=True)
            (ue_dir / "Saved" / "Logs" / "OptUnrealGame.log").write_text(
                "LogInit: Display: Booting\nLogScript: Error: Sample crash detail\n",
                encoding="utf-8",
            )
            (ue_dir / "Proj" / "Content" / "Paks" / "~mods").mkdir(parents=True, exist_ok=True)
            (ue_dir / "Proj" / "Content" / "Paks" / "~mods" / "Sample_P.pak").write_bytes(b"PAK")

            unity_dir = tmp_dir / "CustomUnityGame"
            (unity_dir / "BepInEx" / "plugins").mkdir(parents=True, exist_ok=True)
            (unity_dir / "CustomUnityGame_Data").mkdir(parents=True, exist_ok=True)
            (unity_dir / "CustomUnityGame_Data" / "boot.config").write_text("wait-for-native-debugger=0\n", encoding="utf-8")
            (unity_dir / "doorstop_config.ini").write_text("enabled=true\n", encoding="utf-8")

            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "active_game_id": "opt_unreal",
                    "games": [
                        {
                            "id": "opt_unreal",
                            "name": "Opt Unreal",
                            "game_dir": str(ue_dir),
                            "steam_appid": "123456",
                            "runtime": "Unreal Engine 4/5",
                        },
                        {
                            "id": "custom_unity",
                            "name": "Custom Unity",
                            "game_dir": str(unity_dir),
                            "source": "epic",
                            "runtime": "Unity_IL2CPP",
                        },
                    ],
                }),
                encoding="utf-8",
            )
            snaps_root = tmp_dir / "game_snapshots"
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"
            fake_specs = {
                "cpu": {"name": "Test 8C CPU", "physical_cores": 8, "logical_threads": 16},
                "gpu": {"name": "NVIDIA RTX 3060 8GB", "vram_total_gb": 8.0},
                "ram": {"total_gb": 16.0, "available_gb": 10.0},
                "display": {"width": 1920, "height": 1080, "resolution_label": "1920x1080", "refresh_rate_hz": 144},
                "tier": {"name": "Mainstream 8GB"},
            }

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(library_router, "GAMES_STORE_PATH", store_path),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
                patch.object(library_router, "_launch_platform_uri", return_value=True) as mock_launch,
                patch.object(library_router, "scrape_pc_specs", return_value=fake_specs),
                patch.object(pc_optimizer, "scrape_pc_specs", return_value=fake_specs),
            ):
                core_config.invalidate_games_cache()

                # 1. Verify Steam Integrity + audit log entry
                r_steam = self.client.post(
                    "/api/games/opt_unreal/verify-integrity",
                    json={"launch": True},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_steam.status_code, 200)
                sdata = r_steam.json()
                self.assertEqual(sdata.get("platform"), "steam")
                self.assertEqual(sdata.get("validate_uri"), "steam://validate/123456")
                self.assertTrue(sdata.get("launched"))
                mock_launch.assert_called_once_with("steam://validate/123456")

                r_epic = self.client.post(
                    "/api/games/custom_unity/verify-integrity",
                    json={"launch": False},
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_epic.status_code, 200)
                self.assertEqual(r_epic.json().get("platform"), "epic")
                self.assertTrue(r_epic.json().get("instructions"))

                logged = [json.loads(ln) for ln in temp_audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
                self.assertTrue(any(e.get("action") == "VERIFY_INTEGRITY" and e.get("details", {}).get("validate_uri") == "steam://validate/123456" for e in logged))

                # 2. PC Optimisation traffic lights
                r_pcopt = self.client.get(f"/api/games/opt_unreal/pc-optimisation?custom_ini_path={gus_ini}")
                self.assertEqual(r_pcopt.status_code, 200)
                opt_data = r_pcopt.json()
                self.assertTrue(opt_data.get("is_unreal"))
                self.assertGreater(len(opt_data.get("rows", [])), 0)
                statuses = {r["status"] for r in opt_data["rows"]}
                self.assertTrue(bool(statuses & {"red", "amber"}))
                self.assertIn("green_count", opt_data.get("summary", {}))

                # 3. Snapshots + Full Restore to earliest baseline
                s1 = game_snapshots.create_game_snapshot(
                    "custom_unity", unity_dir, label="Baseline 1", engine="unity", snapshots_root=snaps_root, audit=False
                )
                time.sleep(0.02)
                (unity_dir / "doorstop_config.ini").write_text("enabled=false\n", encoding="utf-8")
                s2 = game_snapshots.create_game_snapshot(
                    "custom_unity", unity_dir, label="Later Snap 2", engine="unity", snapshots_root=snaps_root, audit=False
                )
                self.assertNotEqual(s1["snapshot_id"], s2["snapshot_id"])
                r_full = self.client.post(
                    "/api/games/custom_unity/full-restore",
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_full.status_code, 200)
                self.assertEqual(r_full.json().get("baseline_snapshot", {}).get("snapshot_id"), s1["snapshot_id"])
                self.assertEqual((unity_dir / "doorstop_config.ini").read_text(encoding="utf-8"), "enabled=true\n")

                # 4. Downloadable AI Info Pack .zip
                r_pack = self.client.post(
                    "/api/games/opt_unreal/ai-pack",
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_pack.status_code, 200)
                self.assertIn("application/zip", r_pack.headers.get("content-type", ""))
                with zipfile.ZipFile(io.BytesIO(r_pack.content), "r") as zf:
                    names = set(zf.namelist())
                    for expected_member in (
                        "game_summary.md",
                        "app_info.json",
                        "installed_mods.json",
                        "snapshots.json",
                        "logs/game_log_errors.log",
                    ):
                        self.assertIn(expected_member, names)
                    self.assertIn("Opt Unreal", zf.read("game_summary.md").decode("utf-8"))
                    self.assertIn("Sample crash detail", zf.read("logs/game_log_errors.log").decode("utf-8"))
                core_config.invalidate_games_cache()

    def test_github_release_resolver_and_resilient_batch_install(self) -> None:
        import io
        import httpx
        from app.services import game_store, tool_installer

        cat_uexp = constants.TOOL_RELEASE_CATALOG["unity_explorer_il2cpp"]
        self.assertEqual(cat_uexp["github_repos"], ["yukieiji/UnityExplorer", "sinai-dev/UnityExplorer"])

        # 1. Primary repo match picks UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip over Mono/Standalone
        def handler_primary(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/yukieiji/UnityExplorer/releases/latest":
                self.assertEqual(request.headers.get("accept"), "application/vnd.github+json")
                self.assertTrue(request.headers.get("user-agent"))
                return httpx.Response(200, json={
                    "tag_name": "4.10.2",
                    "assets": [
                        {"name": "UnityExplorer.BepInEx5.Mono.zip", "browser_download_url": "https://dl.test/mono.zip"},
                        {"name": "UnityExplorer.Standalone.IL2CPP.zip", "browser_download_url": "https://dl.test/standalone.zip"},
                        {"name": "UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip", "browser_download_url": "https://dl.test/UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip"},
                    ],
                })
            return httpx.Response(404)

        orig_client = httpx.Client
        with patch.object(
            httpx,
            "Client",
            side_effect=lambda *a, **kw: orig_client(transport=httpx.MockTransport(handler_primary)),
        ):
            chosen = tool_installer.resolve_github_release_asset(
                cat_uexp["github_repos"],
                cat_uexp["asset_match"],
                fallback_url=cat_uexp["fallback_url"],
            )
        self.assertEqual(chosen, "https://dl.test/UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip")

        # 2. Fallback from yukieiji/UnityExplorer -> sinai-dev/UnityExplorer (/releases fallback when /latest is 404)
        def handler_fallback_repo(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p.startswith("/repos/yukieiji/UnityExplorer"):
                return httpx.Response(404)
            if p == "/repos/sinai-dev/UnityExplorer/releases/latest":
                return httpx.Response(404)
            if p == "/repos/sinai-dev/UnityExplorer/releases":
                return httpx.Response(200, json=[{
                    "tag_name": "4.9.0",
                    "prerelease": False,
                    "assets": [
                        {"name": "UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip", "browser_download_url": "https://github.com/sinai-dev/UnityExplorer/releases/download/4.9.0/UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip"},
                        {"name": "UnityExplorer.BepInEx5.Mono.zip", "browser_download_url": "https://github.com/sinai-dev/UnityExplorer/releases/download/4.9.0/UnityExplorer.BepInEx5.Mono.zip"},
                    ],
                }])
            return httpx.Response(404)

        with patch.object(
            httpx,
            "Client",
            side_effect=lambda *a, **kw: orig_client(transport=httpx.MockTransport(handler_fallback_repo)),
        ):
            chosen_fb = tool_installer.resolve_github_release_asset(
                cat_uexp["github_repos"],
                cat_uexp["asset_match"],
                fallback_url=cat_uexp["fallback_url"],
            )
        self.assertEqual(
            chosen_fb,
            "https://github.com/sinai-dev/UnityExplorer/releases/download/4.9.0/UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip",
        )

        # 3. Rate-limit fallback to fallback_url
        def handler_ratelimit(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "API rate limit exceeded"})

        with patch.object(
            httpx,
            "Client",
            side_effect=lambda *a, **kw: orig_client(transport=httpx.MockTransport(handler_ratelimit)),
        ):
            offline_url = tool_installer.resolve_github_release_asset(
                cat_uexp["github_repos"],
                cat_uexp["asset_match"],
                fallback_url="https://fallback.example/uexp.zip",
            )
        self.assertEqual(offline_url, "https://fallback.example/uexp.zip")

        # 4. Batch tool installation continues when dnspy_ex returns 404 while il2cpp_dumper & unity_explorer_il2cpp succeed
        dumper_buf = io.BytesIO()
        with zipfile.ZipFile(dumper_buf, "w") as zf:
            zf.writestr("Il2CppDumper.exe", b"MZ")
        dumper_bytes = dumper_buf.getvalue()

        uexp_buf = io.BytesIO()
        with zipfile.ZipFile(uexp_buf, "w") as zf:
            zf.writestr("plugins/UnityExplorer.BIE.IL2CPP.CoreCLR.dll", b"DLL")
        uexp_bytes = uexp_buf.getvalue()

        def handler_batch_sync(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/repos/Perfare/Il2CppDumper/releases/latest":
                return httpx.Response(200, json={
                    "tag_name": "v6.7.46",
                    "assets": [{"name": "Il2CppDumper-net6-v6.7.46.zip", "browser_download_url": "https://dl.test/Il2CppDumper-net6.zip"}],
                })
            if p == "/repos/yukieiji/UnityExplorer/releases/latest":
                return httpx.Response(200, json={
                    "tag_name": "4.10.2",
                    "assets": [{"name": "UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip", "browser_download_url": "https://dl.test/UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip"}],
                })
            if p.startswith("/repos/dnSpyEx/dnSpy"):
                return httpx.Response(404)
            return httpx.Response(404)

        def handler_batch_async(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if u == "https://dl.test/Il2CppDumper-net6.zip":
                return httpx.Response(200, content=dumper_bytes)
            if u == "https://dl.test/UnityExplorer.BepInEx6.IL2CPP.CoreCLR.zip":
                return httpx.Response(200, content=uexp_bytes)
            return httpx.Response(404)

        orig_async_client = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            game_dir = tmp_dir / "BatchUnityGame"
            (game_dir / "BepInEx" / "plugins").mkdir(parents=True, exist_ok=True)
            (game_dir / "GameAssembly.dll").write_bytes(b"MZ")
            tools_root = tmp_dir / "tools"
            snaps_root = tmp_dir / "game_snapshots"
            store_path = tmp_dir / "managed_games.json"
            store_path.write_text(
                json.dumps({
                    "games": [{
                        "id": "batch_unity",
                        "name": "Batch Unity Game",
                        "game_dir": str(game_dir),
                        "runtime": "Unity_IL2CPP",
                    }]
                }),
                encoding="utf-8",
            )
            temp_audit = tmp_dir / "audit.jsonl"
            temp_app_audit = tmp_dir / "audit_app.jsonl"
            temp_game_audit = tmp_dir / "audit_game.jsonl"

            with (
                patch.object(core_config, "GAMES_STORE_PATH", store_path),
                patch.object(tool_installer, "TOOLS_ROOT", tools_root),
                patch.object(game_store, "TOOLS_ROOT", tools_root),
                patch.object(game_snapshots, "GAME_SNAPSHOTS_DIR", snaps_root),
                patch.object(core_audit, "AUDIT_LOG_PATH", temp_audit),
                patch.object(core_audit, "AUDIT_APP_LOG_PATH", temp_app_audit),
                patch.object(core_audit, "AUDIT_GAME_LOG_PATH", temp_game_audit),
                patch.object(system_router, "scrape_pc_specs", return_value={"gpu": {"vram_total_gb": 8.0}}),
                patch.object(httpx, "Client", side_effect=lambda *a, **kw: orig_client(transport=httpx.MockTransport(handler_batch_sync))),
                patch.object(httpx, "AsyncClient", side_effect=lambda *a, **kw: orig_async_client(transport=httpx.MockTransport(handler_batch_async))),
            ):
                core_config.invalidate_games_cache()
                r_batch = self.client.post(
                    "/api/tools/install-recommended",
                    json={
                        "game_id": "batch_unity",
                        "game_dir": str(game_dir),
                        "tool_ids": ["il2cpp_dumper", "dnspy_ex", "unity_explorer_il2cpp"],
                        "force": True,
                    },
                    headers={SESSION_HEADER: get_session_token()},
                )
                self.assertEqual(r_batch.status_code, 200)
                bdata = r_batch.json()
                self.assertEqual(bdata["status"], "partial")
                self.assertEqual(bdata["installed_count"], 2)
                self.assertEqual(bdata["failed_count"], 1)
                self.assertEqual([x["tool_id"] for x in bdata["installed"]], ["il2cpp_dumper", "unity_explorer_il2cpp"])
                self.assertEqual(bdata["failed"][0]["tool_id"], "dnspy_ex")
                self.assertIn("dnSpyEx/dnSpy", bdata["failed"][0]["reason"])
                self.assertTrue((tools_root / "Il2CppDumper" / "Il2CppDumper.exe").is_file())
                self.assertTrue((game_dir / "BepInEx" / "plugins" / "UnityExplorer.BIE.IL2CPP.CoreCLR.dll").is_file())
                apps_map = {a["id"]: a for a in bdata["game"]["modding_info"]["modding_apps"]}
                self.assertTrue(apps_map["unity_explorer_il2cpp"]["installed_in_game"])
                core_config.invalidate_games_cache()


if __name__ == "__main__":
    unittest.main()

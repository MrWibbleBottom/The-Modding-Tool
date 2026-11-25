"""Frontend HTML partial, ES module import/export, DOM ID, and inline handler contract tests."""

from __future__ import annotations

import ast
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, get_asset_version  # noqa: E402


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_index_html_has_core_tabs_and_palette(self) -> None:
        partials_dir = ROOT / "app" / "templates" / "partials"
        for name in (
            "header.html",
            "game_manager.html",
            "active_game.html",
            "mcp_bridge.html",
            "asset_audit.html",
            "app_mgmt.html",
        ):
            self.assertTrue((partials_dir / name).is_file(), f"Missing template partial {name}")

        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertNotIn("{%", html)
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

        for js_name in (
            "main.js",
            "api.js",
            "state.js",
            "game_table.js",
            "active_game.js",
            "unreal_panel.js",
            "library.js",
            "analyzer.js",
            "mcp.js",
            "git_backup.js",
            "app_mgmt.js",
        ):
            r_js = self.client.get(f"/static/js/{js_name}")
            self.assertEqual(r_js.status_code, 200, f"Missing /static/js/{js_name}")
            self.assertIn("javascript", r_js.headers.get("content-type", "").lower())

        r_health = self.client.get("/api/health")
        self.assertEqual(r_health.status_code, 200)
        self.assertEqual(r_health.json().get("status"), "ok")

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

    def test_js_dom_ids_es_module_contracts_and_cache_busting(self) -> None:
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

        templates_dir = ROOT / "app" / "templates"
        index_html_files = [templates_dir / "index.html", *sorted((templates_dir / "partials").glob("*.html"))]
        all_id_occurrences: list[tuple[str, str]] = []
        inline_handlers: set[str] = set()
        for tpl_path in index_html_files:
            raw_tpl = tpl_path.read_text(encoding="utf-8")
            for m in re.finditer(r"""\bid=["']([^"']+)["']""", raw_tpl):
                all_id_occurrences.append((tpl_path.name, m.group(1)))
            for m in re.finditer(r"""\bon(?:click|change|input)=["']([^"']+)["']""", raw_tpl):
                for fn in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", m.group(1)):
                    if fn not in ("if", "confirm", "alert", "document", "window", "open", "click", "getElementById"):
                        inline_handlers.add(fn)

        dup_ids = {k: v for k, v in Counter(eid for _, eid in all_id_occurrences).items() if v > 1}
        self.assertEqual(dup_ids, {}, f"Duplicate DOM IDs found across HTML templates: {dup_ids}")
        html_ids = {eid for _, eid in all_id_occurrences}

        js_dir = ROOT / "app" / "static" / "js"

        def _collect_exports(target_file: Path, seen: set[Path] | None = None) -> set[str]:
            seen = seen or set()
            if target_file in seen or not target_file.is_file():
                return set()
            seen.add(target_file)
            txt = target_file.read_text(encoding="utf-8")
            found = set(re.findall(r"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z0-9_$]+)", txt))
            for block in re.findall(r"""export\s*\{([^}]+)\}\s*;""", txt):
                for raw_sym in block.split(","):
                    alias = raw_sym.strip().split(" as ")[-1].strip()
                    if alias:
                        found.add(alias)
            for block, mod_name in re.findall(r"""export\s*\{([^}]+)\}\s*from\s*["']\./([^"']+)["']""", txt):
                sub_exports = _collect_exports(js_dir / mod_name, seen)
                for raw_sym in block.split(","):
                    orig = raw_sym.strip().split(" as ")[0].strip()
                    alias = raw_sym.strip().split(" as ")[-1].strip()
                    if orig and orig in sub_exports:
                        found.add(alias)
            return found

        all_exported_fns: set[str] = set()
        for js_path in sorted(js_dir.glob("*.js")):
            txt = js_path.read_text(encoding="utf-8")
            all_exported_fns.update(_collect_exports(js_path))

            for m in re.finditer(r"""getElementById\(\s*["']([^"']+)["']\s*\)""", txt):
                el_id = m.group(1)
                self.assertIn(el_id, html_ids, f"{js_path.name} references missing DOM id '{el_id}'")

            for m in re.finditer(r"""(?:import|export)\s+\{([^}]+)\}\s+from\s+["']\./([^"']+)["']""", txt):
                target_file = js_dir / m.group(2)
                self.assertTrue(target_file.is_file(), f"{js_path.name} references missing module '{m.group(2)}'")
                target_exports = _collect_exports(target_file)
                for raw_sym in m.group(1).split(","):
                    sym = raw_sym.strip().split(" as ")[0].strip()
                    if sym:
                        self.assertIn(sym, target_exports, f"{js_path.name} references missing export '{sym}' from '{m.group(2)}'")

        missing_handlers = sorted(inline_handlers - all_exported_fns)
        self.assertEqual(missing_handlers, [], f"Missing global window handler(s) for inline HTML events: {missing_handlers}")

        state_txt = (js_dir / "state.js").read_text(encoding="utf-8")
        active_txt = (js_dir / "active_game.js").read_text(encoding="utf-8")
        self.assertIn("export function classifyGameEngine(game)", state_txt)
        self.assertIn("unreal-settings-section", (js_dir / "unreal_panel.js").read_text(encoding="utf-8"))
        self.assertIn("card-provision-restore", active_txt)
        self.assertIn("card-live-mcp", active_txt)

    def test_no_legacy_workspace_names_or_paths(self) -> None:
        scan_files: list[Path] = []
        for folder in (ROOT / "app", ROOT / "tests"):
            for p in folder.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix.lower() in (".py", ".html", ".js", ".css", ".bat"):
                    scan_files.append(p)
        for root_file in ("package_windows.py", "backup_manager.py", "version_manager.py", "run_server.py", "start_app.bat"):
            fp = ROOT / root_file
            if fp.is_file():
                scan_files.append(fp)

        legacy_poc_rx = re.compile(r"poc[-_]ui|POC_DIR", re.IGNORECASE)
        legacy_venv_rx = re.compile(r"bepinex-mcp[/\\]\.venv", re.IGNORECASE)
        unprefixed_ws_mcp_rx = re.compile(r"(?:The-Modding-Tool|Battleground)[/\\]bepinex-mcp(?![/\\]?bridges)", re.IGNORECASE)

        cfg_allowed_lines: set[int] = set()
        cfg_tree = ast.parse((ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8"))
        for node in cfg_tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ("_sanitize_legacy_string", "migrate_legacy_appdata"):
                cfg_allowed_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in ("_LEGACY_UNPREFIXED_MCP_RE", "_LEGACY_MCP_VENV_RE"):
                        cfg_allowed_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

        for fpath in sorted(set(scan_files)):
            rel = fpath.relative_to(ROOT).as_posix()
            if rel in ("tests/test_frontend_contracts.py", "tests/test_security.py"):
                continue
            txt = fpath.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(legacy_poc_rx.search(txt), f"Legacy poc-ui/POC_DIR reference found in {rel}")

            for lineno, line in enumerate(txt.splitlines(), 1):
                if rel == "app/core/config.py" and lineno in cfg_allowed_lines:
                    continue
                if rel == "app/static/js/state.js" and "replace" in line:
                    continue
                if "Battleground" in line:
                    self.fail(f"Legacy 'Battleground' reference found at {rel}:{lineno}: {line.strip()}")
                if "ModToolPoC" in line and not (rel == "tests/test_discovery.py" and "ModToolPoC" in line):
                    self.fail(f"Legacy 'ModToolPoC' reference found at {rel}:{lineno}: {line.strip()}")
                if legacy_venv_rx.search(line):
                    self.fail(f"Legacy bridges/bepinex-mcp/.venv reference found at {rel}:{lineno}: {line.strip()}")
                if unprefixed_ws_mcp_rx.search(line):
                    self.fail(f"Un-prefixed workspace bepinex-mcp path found at {rel}:{lineno}: {line.strip()}")

        main_js = (ROOT / "app" / "static" / "js" / "main.js").read_text(encoding="utf-8")
        self.assertIn("sanitizeBrowserLocalStorage()", main_js)

    def test_active_game_five_section_navigation_and_controls(self) -> None:
        html = self.client.get("/").text
        ordered_ids = [
            "ag-tab-btn-general",
            "ag-tab-btn-setup",
            "ag-tab-btn-mods",
            "ag-tab-btn-modding",
            "ag-tab-btn-pcopt",
        ]
        positions = [html.find(f'id="{tid}"') for tid in ordered_ids]
        self.assertTrue(all(pos >= 0 for pos in positions), f"Missing Active Game subnav tab(s): {ordered_ids}")
        self.assertEqual(positions, sorted(positions), "Active Game 5-tab subnav is out of required order")

        for pane_id in ("ag-pane-general", "ag-pane-setup", "ag-pane-mods", "ag-pane-modding", "ag-pane-pcopt"):
            self.assertIn(f'id="{pane_id}"', html)
        for btn_id in ("btn-full-restore-game", "btn-create-ai-pack", "btn-verify-integrity", "cm-mods-tbody", "pcopt-rows-tbody"):
            self.assertIn(f'id="{btn_id}"', html)


if __name__ == "__main__":
    unittest.main()

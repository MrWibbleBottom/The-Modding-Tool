# Changelog

Chronological record of user prompts, AG feedback, and version changes (filed newest-first with date stamp followed by version number).

## 2026-11-25 11:31 — v0.1.40

- **Prompt**: In the GitHub Manager, add an option to pull available branches, add the available branches to the push option as a prepopulated pulldown menu, and add a pop-up warning before pushing stating which branch is being pushed to with `"Push to [branch]"` and `"Cancel Push"`.
- **AG Feedback**: Added remote/local branch discovery in the backend (`GET /api/app/git-status` and `POST /api/app/git-branches`) plus `HEAD:<branch>` target push support, added the **Pull Available Branches** button and prepopulated `#gh-push-branch` dropdown, and added a confirmation modal (`#gh-push-confirm-overlay`) warning which branch is targeted with `Push to <branch>` and `Cancel Push` actions.
- **Version Changes**:
  - Updated `app/routers/git_sync.py` with `_collect_git_branches()`, `_merge_branch_lists()`, branch lists in `GET /api/app/git-status`, `POST /api/app/git-branches` (GitHub REST API + `git ls-remote` fallback), and `HEAD:<branch>` refspec support in `POST /api/app/git-push`.
  - Updated `app/templates/index.html` with `#btn-gh-pull-branches`, `#gh-push-branch` prepopulated select, and `#gh-push-confirm-overlay` warning modal (`#btn-gh-push-confirm` and `#btn-gh-push-cancel`).
  - Updated `app/static/js/app_mgmt.js`, `app/static/js/api.js`, and `app/static/js/main.js` with `_populatePushBranches()`, `pullGitHubBranches()`, `pushAppToGitHub()` modal prompt, `closeGitPushConfirmModal()`, `confirmPushAppToGitHub()`, startup branch pre-population, and `Escape` dismissal.
  - Added `test_github_branch_pull_and_push_confirm_flow` in `tests/test_app_smoke.py`.

## 2026-11-25 11:22 — v0.1.40

- **Prompt**: Add a change log file recording the user prompt, feedback from AG, and new version changes, filed with a date stamp followed by the version number.
- **AG Feedback**: Added `CHANGELOG.md` in the repository root using reverse-chronological `YYYY-MM-DD HH:MM — vX.Y.Z` headings, added `append_changelog_entry()` and `--changelog` CLI support to `version_manager.py`, and added `.agents/rules/changelog.md` so future turns keep `CHANGELOG.md` updated automatically.
- **Version Changes**:
  - Created `CHANGELOG.md` documenting session prompts, AG feedback, and changes alongside the `version.json` baseline (`v0.1.0`–`v0.1.40`).
  - Updated `version_manager.py` with `CHANGELOG_FILE`, `append_changelog_entry()`, and `--changelog` / `--prompt` / `--feedback` / `--changes` CLI flags.
  - Created `.agents/rules/changelog.md` (`always_on`) to maintain `CHANGELOG.md` on future prompts.

## 2026-11-25 11:18 — v0.1.40

- **Prompt**: Implement Phase 1 reliability and UI clarity improvements: asset cache-busting & network-first Service Worker, atomic JSON state writes, progressive disclosure on Active Game, and frontend DOM/import contract tests.
- **AG Feedback**: Implemented all Phase 1 improvements one file at a time, fixed two DOM ID mismatches caught by the contract scan (`mcp-entity-select` and `output-pre`), and verified 25/25 tests passing in `tests/test_app_smoke.py` plus HTTP `200 {"status":"ok"}` on `/api/health`.
- **Version Changes**:
  - `app/main.py` & `app/templates/index.html`: Added `get_asset_version()` (`version.json` + latest `app/static/` mtime) and `?v={{ asset_version }}` query strings on `/static/app.css` and `/static/js/main.js`.
  - `app/static/sw.js`: Bumped cache to `modtool-offline-v2` with network-first handling for `/` and `/static/*`.
  - `app/core/config.py` & routers (`branding.py`, `git_sync.py`, `system.py`, `library.py`): Added `atomic_write_json()` (temp file, `flush()`, `os.fsync()`, `os.replace()`) and switched `managed_games.json` and app configs/caches to atomic writes.
  - `app/static/js/library.js`: Added `_classifyGameEngine()` and progressive disclosure in `renderActiveGamePanel()` / `renderTopGameInfoPanel()` / `renderUnrealSettingsSection()`.
  - `app/static/js/mcp.js` & `app/static/js/api.js`: Fixed `mcp-entity-select` → `mcp-target-entity` and `output-pre` → `fp-output`.
  - `tests/test_app_smoke.py`: Added `test_js_dom_ids_es_module_contracts_and_cache_busting` and `test_atomic_write_json_no_orphan_temp_files`.

## 2026-11-25 10:34 — v0.1.40

- **Prompt**: Change Game Manager 'Show Log' to 'Show Game Scan Log'.
- **AG Feedback**: Updated the scan log toggle button label in both the template and JS toggle sync helper; verified 23/23 tests pass and `/api/health` returns `200 OK`.
- **Version Changes**:
  - `app/templates/index.html`: Changed `#btn-toggle-scan-log` default label to `Show Game Scan Log`.
  - `app/static/js/library.js`: Updated `_syncScanLogToggleButton()` to toggle between `Hide Game Scan Log` and `Show Game Scan Log`.

## 2026-11-25 10:33 — v0.1.40

- **Prompt**: Fix Game Manager "Load Game" workflow so clicking Load Game or choosing from Quick Load Game immediately updates the Game Info & Modding Requirements panel and all active-game views.
- **AG Feedback**: Identified that `apiPost()` returns parsed JSON directly while `selectManagedGame()` and other `library.js` callers checked `res.ok && res.data`, `library.js` lacked `ENTITIES`/`VARIABLES` imports from `mcp.js`, and `#header-quick-load` / `#global-game-select` were missing from `index.html`. Fixed all three and verified with a new multi-game switch regression test (`23 passed`).
- **Version Changes**:
  - `app/routers/library.py`: Exposed both `POST /api/games/select` and `POST /api/games/load`, ensured `name` fallback in `enrich_game_status()`, and ensured missing `cached_modding_info` triggers scraping on load.
  - `app/templates/index.html`: Restored `#header-quick-load` (`#global-game-select`) and added `#info-slug-badge` and `#info-engine-badge`.
  - `app/static/js/library.js`: Added shared `renderActiveGamePanel(game)` with background metadata auto-fetch (`_autoPopulateGameMetadata`), wired into Load Game, Quick Load, and initial load, and fixed all `apiPost` response checks and `mcp.js` imports.
  - `tests/test_app_smoke.py`: Added `test_load_game_switches_full_profile_and_ui_wiring`.

## 2026-11-25 10:25 — v0.1.40

- **Prompt**: Clean up the duplicate progress bar and convert the giant scan output box into a separate closeable log panel in the Game Manager panel.
- **AG Feedback**: Removed the secondary `#scan-progress-wrap` bar, routed all Game Manager scan actions through `runTrackedAction`'s `setProgress()`, and replaced `<pre id="scan-output">` with a hidden-by-default closeable `#scan-log-panel`; verified 22/22 tests pass.
- **Version Changes**:
  - `app/templates/index.html`: Removed `#scan-progress-wrap`, added `#btn-toggle-scan-log`, `#btn-prune-non-games`, `#btn-scan-path`, and `#scan-log-panel` (`Copy` + `×` close controls).
  - `app/static/app.css`: Added `.scan-log-panel`, `.scan-log-header`, `.scan-log-actions`, `.scan-log-close`, and `.scan-log-pre` styles.
  - `app/static/js/library.js`: Removed `#scan-progress-*` DOM updates, mapped scan phases through `runTrackedAction` `setProgress()`, and added `setScanOutputLog()`, `toggleScanOutputLog()`, `closeScanOutputLog()`, and `copyScanOutputLog()`.
  - `tests/test_app_smoke.py`: Updated `test_index_html_has_core_tabs_and_palette` assertions.

## 2026-11-25 10:23 — v0.1.40

- **Prompt**: Update game discovery and Unreal Engine scanning so non-games (Unreal Engine editor installs, Steam redistributables, Proton/runtimes, dedicated servers, SDKs, soundtracks, utilities) are excluded.
- **AG Feedback**: Added centralized `is_non_game_entry()`, updated `discover_local_unreal_ini_profiles()` to skip non-game `%LOCALAPPDATA%` roots and `WindowsEditor`, and fixed `scan_for_games()` `custom_path` container vs game-root detection caught during regression testing (`22 passed`).
- **Version Changes**:
  - `app/routers/library.py`: Added `NON_GAME_STEAM_APPIDS`, `NON_GAME_NAME_PATTERNS`, `NON_GAME_LOCALAPPDATA_FOLDERS`, and `is_non_game_entry()`; wired filtering into `is_game_candidate()`, `scan_steam_installed()`, `scan_epic_installed()`, `scan_manual_folders()`, and `discover_local_unreal_ini_profiles()`.
  - `tests/test_app_smoke.py`: Added `test_non_game_exclusions_and_unreal_editor_ini_filter`.

## 2026-11-25 10:13 — v0.1.40

- **Prompt**: Run a full post-refactor and post-rename verification pass (undefined AST names, legacy `Battleground`/`poc-ui`/`ModToolPoC`/unprefixed `bepinex-mcp` sweep, packaging path checks, expanded smoke tests).
- **AG Feedback**: AST scan caught undefined names in `library.py` (`_UE_QUALITY_LABELS`), `mcp_bridge.py` (`make_slug`, `inspect_game_modding_info`), and `system.py` (`REQUIRED_PLUGIN_FILES`, `inspect_game_modding_info`, `enrich_game_status`), plus `PermissionError` guards in `discover_local_unreal_ini_profiles()` and `scrape_pc_specs()` and 4 stale `d:\Downloads\Battleground\` paths in `index.html`; verified all `package_windows.py` paths exist and 21/21 smoke tests pass.
- **Version Changes**:
  - `app/routers/library.py`: Imported `_UE_QUALITY_LABELS` from `app.routers.system`; guarded `discover_local_unreal_ini_profiles()` with `OSError` handling.
  - `app/routers/system.py`: Switched `REQUIRED_PLUGIN_FILES` → `PLUGIN_DEPLOY_FILES`, added local `library` imports in `install_modding_tool()`, and guarded `drv_path.is_dir()` in `scrape_pc_specs()`.
  - `app/routers/mcp_bridge.py`: Imported `inspect_game_modding_info` and `make_slug` from `app.routers.library`.
  - `app/templates/index.html`: Updated 4 default paths from `d:\Downloads\Battleground\...` to `d:\Downloads\The-Modding-Tool\...`.
  - `tests/test_app_smoke.py`: Added `test_unreal_metadata_rescan_and_ini_settings` and `test_unity_bridge_discovery_and_all_router_endpoints`.

## 2026-11-25 09:59 — v0.1.40

- **Prompt**: Implement an Offline Control Page with Start/Stop Server support using a Service Worker and a user-level Windows custom URL protocol (`moddingtool://start`).
- **AG Feedback**: Implemented `/api/system/shutdown` (logging `SERVER_STOPPED`), `register_protocol_handler()` + `/api/system/register-protocol`, `start_app.bat --no-browser`, `/offline`, `/sw.js`, and UI controls (`Stop Server`, `Register moddingtool:// Launcher`); verified 19/19 smoke tests pass and confirmed live browser stop behavior in `task-129`.
- **Version Changes**:
  - `app/routers/system.py`: Added `register_protocol_handler()`, `POST /api/system/register-protocol`, and `POST /api/system/shutdown`.
  - `app/main.py`: Registered protocol handler on startup in `lifespan` and added `GET /offline` and `GET /sw.js` (`Service-Worker-Allowed: /`).
  - `app/templates/offline.html` & `app/static/sw.js`: Created dark-themed offline control page polling `/api/health` every `1.5s` and Service Worker precaching `/offline` and `/static/app.css`.
  - `app/templates/index.html`, `app/static/js/app_mgmt.js`, `app/static/js/main.js`: Registered `/sw.js` and added `stopAppServer()` and `registerProtocolLauncher()` controls.
  - `run_server.py` & `start_app.bat`: Added `MODTOOL_SHUTDOWN_SIGNAL_FILE` clean exit handling and `--no-browser` background launch mode.
  - `tests/test_app_smoke.py`: Added `test_offline_page_service_worker_and_ui_controls` and `test_system_shutdown_and_protocol_registration`.

## 2026-11-25 09:50 — v0.1.40

- **Prompt**: App hasn't started — analyse and advise, then apply fixes and start the server.
- **AG Feedback**: Diagnosed that synchronous `Start-Process` commands on Windows are terminated by Job Object cleanup (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) on command exit and that `start_app.bat` opened the browser before Uvicorn finished binding port `8765`. Applied fixes and started the server as a persistent daemon (`IsDaemon: true`, `BypassSandbox: true`).
- **Version Changes**:
  - `run_server.py`: Added `--open-browser` health-polling launcher and flushed supervisor output.
  - `start_app.bat`: Replaced premature `start "" "http://127.0.0.1:8765"` with `run_server.py --open-browser`.
  - `.agents/rules/keep-server-alive.md`: Updated server restart rule to use `IsDaemon: true` + `BypassSandbox: true`.

## 2026-11-24 — v0.1.0 .. v0.1.40

- **Prompt**: Baseline semantic versioning & incremental backup history tracked in `version.json`.
- **AG Feedback**: Recorded 40 pre-launch patch iterations (`v0.1.0` initial setup on `2026-11-24T08:09:37` through `v0.1.40` on `2026-11-24T09:38:19`) during PoC development and modular FastAPI refactoring.
- **Version Changes**:
  - `version.json`: Pre-launch SemVer history from `v0.1.0` to `v0.1.40`.

import { apiFetch, apiGet, apiPost, runTrackedAction, state } from "./api.js";
import {
  STEAM_ASSETS_JS_CACHE,
  classifyGameEngine,
  escHtml,
  getUiVisibility,
  libState,
  resolveBtn,
  upsertManagedGameInState,
} from "./state.js";
import { ENTITIES, VARIABLES, mcpTestBridge, runFingerprintAndRescan, syncModBuilderWithGame } from "./mcp.js";
import { importUnrealSettings, renderUnrealSettingsSection } from "./unreal_panel.js";
import { refreshGamesList, renderGamesTable } from "./game_table.js";

export async function rescanActiveGameMetadata() {
  const res = await apiPost("/api/games/rescan", { game_id: state.activeGameId });
  const data = res?.data ?? res;
  if (data && data.game) {
    upsertManagedGameInState(data.game);
    renderActiveGamePanel(data.game, { allowAutoFetch: false });
    await refreshGamesList(false);
  }
}

export function renderTopGameInfoPanel(game) {
  if (!game) return;
  const info = game.modding_info || {};
  const isMissing = Boolean(game.missing_on_disk || info.missing_on_disk);
  const missingBadge = document.getElementById("info-missing-badge");
  if (missingBadge) {
    missingBadge.style.display = isMissing ? "inline-block" : "none";
  }

  const titleEl = document.getElementById("info-title");
  if (titleEl) titleEl.textContent = game.name || game.id || "Unknown Game";
  const slugBadge = document.getElementById("info-slug-badge");
  if (slugBadge) slugBadge.textContent = game.id || "unknown";
  const engineBadge = document.getElementById("info-engine-badge");
  if (engineBadge) engineBadge.textContent = info.mcp_category || game.runtime || "PC";

  const { isUnity, isUnreal } = classifyGameEngine(game);

  const devPrefix = (info.developer && info.developer !== "Unknown") ? `${info.developer} — ` : "";
  document.getElementById("info-product").textContent =
    `${devPrefix}${info.product_name || game.name} (${info.executable_rel || info.executable || "N/A"})`;
  document.getElementById("info-platform").textContent =
    `${info.platform || "Windows PC"} [${info.architecture || "x64"}]`;

  const setLbl = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };

  if (isUnity) {
    setLbl("lbl-info-engine-ver", "Unity Engine Build Version");
    setLbl("lbl-info-installed-loader", "Installed Loader & Doorstop");
    setLbl("lbl-info-interop", "Generated IL2CPP Interop");
    setLbl("lbl-info-build-guid", "Unity Build GUID");

    document.getElementById("info-unity-ver").textContent =
      `Unity ${info.unity_version || "Unknown"} (${info.scripting_assemblies_count || 0} game assemblies)`;
    const bepStr = info.bepinex_installed
      ? `BepInEx ${info.bepinex_version || "6.x"}`
      : "BepInEx Not Installed";
    const dsStr = info.doorstop_installed
      ? `Doorstop v${info.doorstop_version || "installed"}`
      : "No Doorstop";
    document.getElementById("info-installed-loader").textContent = `${bepStr} | ${dsStr}`;
    document.getElementById("info-interop").textContent = info.interop_assemblies_count > 0
      ? `${info.interop_assemblies_count} DLLs (hash: ${(info.assembly_hash || "").slice(0, 10)}...)`
      : "Not generated yet";
    document.getElementById("info-build-guid").textContent = info.build_guid || "Unknown";
  } else if (isUnreal) {
    setLbl("lbl-info-engine-ver", "Engine & Content Archives");
    setLbl("lbl-info-installed-loader", "Installed Unreal Loader");
    setLbl("lbl-info-interop", "Unreal Mods & Config");
    setLbl("lbl-info-build-guid", "Unreal Project Name");

    document.getElementById("info-unity-ver").textContent =
      `Unreal Engine 4/5 (${info.pak_count || 0} .pak archive(s))`;
    document.getElementById("info-installed-loader").textContent =
      info.ue4ss_installed ? "RE-UE4SS Installed (Win64)" : "RE-UE4SS Not Installed";
    const ueIniFound = Boolean(info.unreal_settings?.ini_path);
    document.getElementById("info-interop").textContent =
      `${info.ue_mods_count || 0} Lua Mod(s) | ${ueIniFound ? "GameUserSettings.ini Found" : "No INI Imported"}`;
    document.getElementById("info-build-guid").textContent =
      info.detected_project_name || info.product_name || game.name;
  } else {
    setLbl("lbl-info-engine-ver", "Engine & Runtime Category");
    setLbl("lbl-info-installed-loader", "Installed Mod Loader");
    setLbl("lbl-info-interop", "Modding Workspace");
    setLbl("lbl-info-build-guid", "Primary Executable");

    document.getElementById("info-unity-ver").textContent =
      info.mcp_category || game.runtime || "Native PC";
    document.getElementById("info-installed-loader").textContent =
      info.bepinex_installed ? `BepInEx ${info.bepinex_version || ""}` : "Standard / Data-Driven";
    document.getElementById("info-interop").textContent =
      info.executable_rel || info.executable || "Config / Data Overrides";
    document.getElementById("info-build-guid").textContent =
      info.executable || "N/A";
  }

  document.getElementById("info-backend").textContent =
    info.scripting_backend || game.runtime || "Unknown";
  document.getElementById("info-req-loader").textContent =
    info.required_modding_tool || "Unknown";
  document.getElementById("info-req-plugin").textContent =
    info.required_bridge_plugin || "Unknown";

  document.getElementById("info-gfx-xr").textContent =
    `${info.graphics_api || "Standard"} | XR: ${info.xr_sdk || "None"}`;

  document.getElementById("info-sheet-cat").textContent =
    `${info.mcp_category || game.runtime || "PC"} — ${info.sheet_reference_match || "Auto-detected"}`;
  document.getElementById("info-mcp-stack").textContent =
    info.recommended_mcp_stack || "—";
  document.getElementById("info-mcp-discovery").textContent =
    info.discovery_mcp_tools || "—";
  document.getElementById("info-mcp-modding").textContent =
    info.modding_mcp_tools || "—";

  // Update Quick Status Indicators Bar (Platform-Agnostic)
  const setPill = (id, cls, text) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `pill ${cls}`;
    el.textContent = text;
  };

  const acBlocked = info.mcp_category === "Online / Anti-Cheat" || (info.modding_readiness || "").startsWith("Blocked");
  // 1. Game Installed
  const gameOnDisk = Boolean(info.dir_exists !== false && !game.missing_on_disk);
  const hasExe = Boolean(info.executable && info.executable !== "Not found");
  if (!gameOnDisk) {
    setPill("ind-game-installed", "no", "Game Installed: Missing on Disk");
  } else if (hasExe) {
    setPill("ind-game-installed", "yes", `Game Installed: ${info.executable}`);
  } else {
    setPill("ind-game-installed", "yes", "Game Installed: Ready");
  }

  // 2. Modding Tools Installed
  const appsList = Array.isArray(info.modding_apps) ? info.modding_apps : [];
  const installedApps = appsList.filter(a => a.installed);
  const hasLoader = isUnity
    ? Boolean(info.doorstop_installed && info.bepinex_installed)
    : (isUnreal ? Boolean(info.ue4ss_installed) : false);
  if (acBlocked) {
    setPill("ind-modding-tools", "no", "Modding Tools: Blocked (Anti-Cheat)");
  } else if (hasLoader || installedApps.length > 0) {
    const parts = [];
    if (isUnity && hasLoader) parts.push(`BepInEx ${info.bepinex_version || ""}`.trim());
    else if (isUnreal && hasLoader) parts.push("UE4SS");
    if (installedApps.length > 0) parts.push(`${installedApps.length} App(s)`);
    setPill("ind-modding-tools", "yes", `Modding Tools: ${parts.join(" + ")}`);
  } else {
    setPill("ind-modding-tools", "warn", "Modding Tools: None Installed");
  }

  // 3. Mods Installed (Scanned Game Folders, separated into Game Mods vs Graphics/Upscaler Mods)
  const modsList = Array.isArray(info.installed_mods) ? info.installed_mods : [];
  const gameMods = modsList.filter(m => m.group !== "injector_overlay");
  const injectorMods = modsList.filter(m => m.group === "injector_overlay");
  const modsSum = info.installed_mods_summary || {};
  const gameModsCount = modsSum.game_mods_count ?? gameMods.length;
  const injectorModsCount = modsSum.injector_mods_count ?? injectorMods.length;
  const modsTotal = modsSum.total ?? modsList.length;
  const modsManagers = Array.isArray(modsSum.managed_by) ? modsSum.managed_by : [];
  const modsFolders = Array.isArray(modsSum.folders_scanned) ? modsSum.folders_scanned : [];
  const modsSummaryEl = document.getElementById("installed-mods-summary");
  const modsMetaEl = document.getElementById("installed-mods-meta");
  const modsTbody = document.getElementById("info-installed-mods-tbody");
  const injTbody = document.getElementById("info-injector-mods-tbody");
  const gameCountBadge = document.getElementById("game-mods-count-badge");
  const injCountBadge = document.getElementById("injector-mods-count-badge");

  if (modsTotal > 0) {
    const mgrSuffix = modsManagers.length ? ` (${modsManagers.join(", ")})` : "";
    const breakdown = injectorModsCount > 0
      ? `${gameModsCount} Game + ${injectorModsCount} Graphics/Upscaler`
      : `${gameModsCount} Game`;
    setPill("ind-mods-installed", "yes", `Mods Installed: ${breakdown}${mgrSuffix}`);
  } else {
    setPill("ind-mods-installed", "na", "Mods Installed: 0");
  }

  // 4. ReShade Installed
  const rs = info.reshade || {};
  if (rs.installed) {
    const verTag = rs.version ? ` v${rs.version}` : "";
    const addons = Array.isArray(rs.addons) ? rs.addons : [];
    const shaderText = `${rs.shaders_count || 0} Shader(s)`;
    setPill("ind-reshade-installed", "yes", `ReShade${verTag}: ${addons.length} Addon(s) | ${shaderText}`);
  } else {
    setPill("ind-reshade-installed", "na", "ReShade: Not Installed");
  }

  // 5. Game Settings Optimised for PC Specs (Unreal INI)
  const ueSettings = info.unreal_settings || null;
  const optPillEl = document.getElementById("ind-settings-optimised");
  if (optPillEl) optPillEl.style.display = isUnreal ? "inline-flex" : "none";
  if (isUnreal && ueSettings) {
    const tlSum = ueSettings.traffic_light_summary || {};
    const overall = tlSum.overall_light || ueSettings.spec_comparison?.overall_light || "";
    if (!ueSettings.ini_found) {
      setPill("ind-settings-optimised", "warn", "Settings Optimised: No INI Found");
    } else if (overall === "green") {
      setPill("ind-settings-optimised", "yes", "Settings Optimised: Optimal for PC");
    } else if (overall === "red") {
      setPill("ind-settings-optimised", "no", "Settings Optimised: Bottleneck");
    } else {
      setPill("ind-settings-optimised", "warn", "Settings Optimised: Sub-Optimal");
    }
  } else {
    setPill("ind-settings-optimised", "na", "Settings Optimised: N/A");
  }
  if (modsSummaryEl) {
    const mgrTag = modsManagers.length ? ` [Managed by ${modsManagers.join(", ")}]` : "";
    modsSummaryEl.textContent = `Installed Mods Detected (${gameModsCount} Game Mods | ${injectorModsCount} Graphics/Upscaler Mods)${mgrTag}`;
  }
  if (gameCountBadge) gameCountBadge.textContent = `${gameModsCount}`;
  if (injCountBadge) injCountBadge.textContent = `${injectorModsCount}`;
  if (modsMetaEl) {
    const folderTxt = modsFolders.length ? modsFolders.join(", ") : "None found";
    modsMetaEl.innerHTML = `Scanned folders: <code>${escHtml(folderTxt)}</code>`;
  }
  const _renderModRows = (list, emptyMsg) => {
    if (!list.length) return `<tr><td colspan="5">${emptyMsg}</td></tr>`;
    return list.map(m => {
      const sz = m.size_kb ? `${m.size_kb} KB` : "—";
      const statusBadge = m.enabled === false ? ' <span class="pill warn">Disabled</span>' : "";
      return `<tr>
        <td><strong>${escHtml(m.name)}</strong>${statusBadge}</td>
        <td><span class="badge">${escHtml(m.category)}</span></td>
        <td><code>${escHtml(m.file_name)}</code></td>
        <td>${sz}</td>
        <td style="font-size:0.72rem; color:var(--muted);"><code>${escHtml(m.path)}</code></td>
      </tr>`;
    }).join("");
  };
  if (modsTbody) modsTbody.innerHTML = _renderModRows(gameMods, "No game mods detected in scanned game folders.");
  if (injTbody) injTbody.innerHTML = _renderModRows(injectorMods, "No graphics, upscaler, or overlay mods detected.");

  // Render Crucial Modding Steps Checklist Cards
  const stepsEl = document.getElementById("info-crucial-steps");
  if (stepsEl) {
    const steps = Array.isArray(info.crucial_steps) ? info.crucial_steps : [];
    if (steps.length === 0) {
      stepsEl.innerHTML = `<div class="step-card pending"><div class="step-card-title">Metadata Not Scraped Yet</div><div class="step-card-label">Click Load Game or Fingerprint & Rescan to inspect modding files and bridge status.</div></div>`;
    } else {
      const stateMeta = {
        ok: { pillCls: "yes", pillText: "READY" },
        missing: { pillCls: "no", pillText: "MISSING" },
        pending: { pillCls: "warn", pillText: "PENDING" },
        blocked: { pillCls: "no", pillText: "BLOCKED" },
        na: { pillCls: "na", pillText: "EXTERNAL" }
      };
      stepsEl.innerHTML = steps.map(s => {
        const st = s.state || "pending";
        const m = stateMeta[st] || stateMeta.pending;
        return `<div class="step-card ${st}">
          <div class="step-card-top">
            <span class="step-card-title">${s.step || ""}</span>
            <span class="pill ${m.pillCls}">${m.pillText}</span>
          </div>
          <div class="step-card-label">${s.label || ""}</div>
          <div class="step-card-detail">${s.detail || ""}</div>
        </div>`;
      }).join("");
    }
  }

  state.lastRenderedGame = game;
  const provCard = document.getElementById("card-provision-restore");
  const mcpCard = document.getElementById("card-live-mcp");
  const uiVis = getUiVisibility();
  if (provCard) provCard.style.display = (isUnity && uiVis.sec_provision_restore !== false) ? "flex" : "none";
  if (mcpCard) mcpCard.style.display = (isUnity && uiVis.sec_live_mcp !== false) ? "flex" : "none";

  renderModdingAppsSection(game);
  renderUnrealSettingsSection(game);
}

export function renderCurrentGameModdingApps() {
  const activeGame = state.lastRenderedGame || state.managedGames.find(g => g.id === state.activeGameId) || state.managedGames[0];
  if (activeGame) {
    renderModdingAppsSection(activeGame);
  }
}

export function renderModdingAppsSection(game) {
  const container = document.getElementById("info-modding-apps");
  if (!container || !game) return;
  const info = game.modding_info || {};
  const allApps = Array.isArray(info.modding_apps) ? info.modding_apps : [];
  const mode = document.getElementById("tools-filter-mode")?.value || "recommended";
  const compatApps = allApps.filter(a => a.engine_compatible !== undefined ? a.engine_compatible === true : Boolean(a.recommended));
  const shownApps = mode === "all" ? allApps : compatApps;

  if (shownApps.length === 0) {
    const isBlockedAc = info.mcp_category === "Online / Anti-Cheat" || (info.modding_readiness || "").startsWith("Blocked");
    container.innerHTML = isBlockedAc
      ? `<div class="step-card blocked"><div class="step-card-title">Modding Tools Not Recommended (Anti-Cheat Protected)</div><div class="step-card-label">This title uses kernel/online anti-cheat (${escHtml(info.sheet_reference_match || info.mcp_category)}). Mod loaders are disabled by default; switch the dropdown to 'All Modding Apps' only if inspecting offline tools.</div></div>`
      : `<div class="step-card na"><div class="step-card-title">No Engine-Compatible Tools Matched</div><div class="step-card-label">Click Fingerprint &amp; Rescan or switch filter to inspect all tools.</div></div>`;
    return;
  }

  container.innerHTML = shownApps.map(app => {
    const inGame = Boolean(app.installed_in_game);
    const hostReady = Boolean(app.host_available !== undefined ? app.host_available : app.installed);
    const stClass = inGame ? "ok" : (hostReady ? "pending" : (app.recommended ? "pending" : "na"));
    const pillCls = inGame ? "yes" : (hostReady ? "warn" : "na");
    const pillText = inGame
      ? "Ready in Game"
      : (hostReady ? "Host Tool Ready (Not Installed in Game)" : "Recommended (Not Installed)");
    const recBadge = app.recommended
      ? `<span class="badge" style="color:#fbbf24; border-color:rgba(245,158,11,0.45); font-size:0.68rem;">Engine Compatible</span>`
      : `<span class="badge" style="font-size:0.68rem;">${app.category_badge || "Tool"}</span>`;
    const escapedTarget = (app.install_target || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    const escapedUrl = (app.release_url || "").replace(/'/g, "\\'");
    const btnClass = app.installed ? "secondary btn-sm" : "success btn-sm";

    return `<div class="step-card ${stClass}">
      <div class="step-card-top">
        <span class="step-card-title">${app.name}</span>
        <span class="pill ${pillCls}">${pillText}</span>
      </div>
      <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin:2px 0;">
        ${recBadge}
        <span style="font-size:0.73rem; color:var(--muted);">${app.status_text || ""}</span>
      </div>
      <div style="font-size:0.75rem; color:#cbd5e1; line-height:1.35;">${app.description || ""}</div>
      <div class="step-card-detail" title="Install target directory">Path: ${app.install_target || ""}</div>
      <div class="btn-group" style="margin-top:6px;">
        <button class="${btnClass}" id="btn-tool-${app.id}" onclick="installModdingTool('${app.id}')">${app.button_label || "Install"}</button>
        <button class="secondary btn-sm" onclick="openToolFolder('${escapedTarget}')">Open Folder</button>
        <button class="secondary btn-sm" onclick="window.open('${escapedUrl}', '_blank')">Release Page</button>
      </div>
    </div>`;
  }).join("");
}

export async function installModdingTool(toolId, forceOverwrite = false) {
  const out = document.getElementById("tools-install-output");
  const btn = resolveBtn(`btn-tool-${toolId}`);
  const gameDir = document.getElementById("fp-game-dir")?.value || "";
  if (out) {
    out.style.display = "block";
    out.textContent = `Installing / deploying modding tool '${toolId}'...`;
  }
  try {
    await runTrackedAction(
      btn,
      async () => {
        let res = await apiPost("/api/tools/install", {
          tool_id: toolId,
          game_id: state.activeGameId,
          game_dir: gameDir,
          force: Boolean(forceOverwrite),
        });
        let data = res?.data ?? res;
        if (data?.status === "conflict_warning" && data?.requires_confirmation) {
          const warnLines = (data.warnings || []).map(w => `• ${w.title}: ${w.message}`).join("\n");
          _renderConflictNotice(data.conflict_guard);
          if (out) {
            out.textContent = `CONFLICT GUARD WARNING:\n${warnLines}\n\nConfirm installation to take a safety snapshot and proceed.`;
          }
          if (!confirm(`Mod Manager / Proxy DLL Conflict Detected:\n\n${warnLines}\n\nProceed anyway (a safety snapshot will be created first)?`)) {
            return data;
          }
          res = await apiPost("/api/tools/install", {
            tool_id: toolId,
            game_id: state.activeGameId,
            game_dir: gameDir,
            force: true,
          });
          data = res?.data ?? res;
        }
        if (out) {
          const snapLine = data.snapshot_id ? `\nPre-change snapshot: ${data.snapshot_id}` : "";
          out.textContent = `${data.message}\nSHA-256: ${data.sha256}\nTarget Folder: ${data.install_dir}${snapLine}`;
        }
        if (data?.game) {
          applyGameSettingsToUI(data.game);
        }
        await Promise.all([refreshGamesList(false), loadGameSnapshots(state.activeGameId)]);
        return data;
      },
      {
        pendingText: `Installing ${toolId}...`,
        successText: (d) => d?.status === "conflict_warning"
          ? "Installation cancelled due to conflict warning"
          : (d?.message || `Installed ${toolId}`),
      },
    );
  } catch (err) {
    if (out) out.textContent = `Error installing '${toolId}': ${err?.message || err}`;
  }
}

export async function installAllRecommendedTools() {
  const game = state.lastRenderedGame || state.managedGames.find(g => g.id === state.activeGameId) || state.managedGames[0];
  if (!game) return;
  const apps = ((game.modding_info || {}).modding_apps || []).filter(a => a.recommended);
  const out = document.getElementById("tools-install-output");
  const btn = resolveBtn("btn-install-all-rec");
  if (apps.length === 0) {
    if (out) {
      out.style.display = "block";
      out.textContent = "No recommended modding apps found for this game.";
    }
    return;
  }
  const results = [];
  try {
    await runTrackedAction(
      btn,
      async ({ setProgress }) => {
        for (let i = 0; i < apps.length; i++) {
          const app = apps[i];
          setProgress(Math.round((i / apps.length) * 100), `Installing ${app.name} (${i + 1}/${apps.length})...`);
          if (out) {
            out.style.display = "block";
            out.textContent = `Installing ${app.name} (${app.id})...\n` + results.join("\n");
          }
          const res = await apiPost("/api/tools/install", {
            tool_id: app.id,
            game_id: state.activeGameId,
            game_dir: document.getElementById("fp-game-dir")?.value || game.game_dir || "",
          });
          const data = res?.data ?? res;
          results.push(`[OK] ${app.name}: ${data?.message || "Installed"}`);
        }
        setProgress(100, `Installed ${apps.length} recommended app(s)`);
        if (out) {
          out.textContent = `Finished installing recommended apps for ${game.name}:\n` + results.join("\n");
        }
        await refreshGamesList(true);
        await loadSetupChecklist(state.activeGameId);
        return results;
      },
      {
        pendingText: `Installing ${apps.length} recommended modding app(s)...`,
        successText: `Installed ${apps.length} recommended app(s) for ${game.name}`,
        mode: "determinate",
      },
    );
  } catch (err) {
    if (out) out.textContent = `Recommended tool install failed: ${err?.message || err}`;
  }
}

export async function openToolFolder(pathStr) {
  if (!pathStr) return;
  const out = document.getElementById("tools-install-output");
  try {
    await apiPost("/api/tools/open-folder", { path: pathStr });
  } catch (err) {
    if (out) {
      out.style.display = "block";
      out.textContent = `Could not open folder '${pathStr}': ${err?.message || err}`;
    }
  }
}

async function _autoPopulateGameMetadata(gameId) {
  if (!gameId || libState.autoRescanInFlight.has(gameId)) return;
  libState.autoRescanInFlight.add(gameId);
  try {
    const data = await apiPost("/api/games/rescan", { game_id: gameId });
    if (data && data.game) {
      upsertManagedGameInState(data.game);
      if (state.activeGameId === gameId) {
        renderActiveGamePanel(data.game, { allowAutoFetch: false });
      }
      await refreshGamesList(false);
    }
  } catch (err) {
    console.warn(`Background metadata fetch failed for ${gameId}:`, err);
  } finally {
    libState.autoRescanInFlight.delete(gameId);
  }
}

export function renderActiveGamePanel(game, { allowAutoFetch = true } = {}) {
  if (!game) return;
  const prevId = state.lastRenderedGame?.id;
  const gameChanged = Boolean(prevId && prevId !== game.id);

  state.activeGameId = game.id;
  upsertManagedGameInState(game);

  const activeBadge = document.getElementById("active-game-badge");
  if (activeBadge) activeBadge.textContent = `Loaded: ${game.name || game.id}`;
  const tabBadge = document.getElementById("game-tab-title-badge");
  if (tabBadge) tabBadge.textContent = game.name || game.id;
  const globalSel = document.getElementById("global-game-select");
  if (globalSel && globalSel.value !== game.id) {
    globalSel.value = game.id;
  }

  if (gameChanged) {
    const fpOut = document.getElementById("fp-output");
    if (fpOut) { fpOut.style.display = "none"; fpOut.textContent = ""; }
    const toolsOut = document.getElementById("tools-install-output");
    if (toolsOut) { toolsOut.style.display = "none"; toolsOut.textContent = ""; }
    const provOut = document.getElementById("prov-output");
    if (provOut) {
      provOut.textContent = "Ready to validate SHA-256, back up collisions, extract BepInEx + BepInExMCP.IL2CPP.dll, or restore cleanly.";
    }
    const notice = document.getElementById("adapter-status-notice");
    if (notice) notice.style.display = "none";
    const logDrawer = document.getElementById("game-log-tail-drawer");
    if (logDrawer) logDrawer.style.display = "none";
  }

  renderTopGameInfoPanel(game);
  const { isUnity, isUnreal } = classifyGameEngine(game);
  const bdlBtn = document.getElementById("btn-unity-bdl-loop");
  if (bdlBtn) bdlBtn.style.display = isUnity ? "" : "none";
  const provCard = document.getElementById("card-provision-restore");
  const mcpCard = document.getElementById("card-live-mcp");
  const ueSection = document.getElementById("unreal-settings-section");
  const uiVis = getUiVisibility();
  if (provCard) provCard.style.display = (isUnity && uiVis.sec_provision_restore !== false) ? "flex" : "none";
  if (mcpCard) mcpCard.style.display = (isUnity && uiVis.sec_live_mcp !== false) ? "flex" : "none";
  if (ueSection && !isUnreal) ueSection.style.display = "none";

  const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  setVal("fp-game-dir", game.game_dir || "");
  setVal("prov-zip-path", game.bepinex_zip_path || "");
  setVal("prov-sha256", game.expected_sha256 || "");
  setVal("prov-plugin-dir", game.plugin_build_dir || "");
  setVal("mcp-python", game.venv_python_path || "");
  setVal("mcp-app", game.mcp_app_path || "");

  const targetEnt = document.getElementById("mcp-target-entity");
  if (targetEnt && game.default_entity && ENTITIES[game.default_entity]) {
    targetEnt.value = game.default_entity;
  }
  const targetVar = document.getElementById("mcp-variable-select");
  if (targetVar && game.default_variable && VARIABLES[game.default_variable]) {
    targetVar.value = game.default_variable;
  }
  setVal("mcp-go-id", game.default_go_id || "-269924");
  setVal("mcp-comp-name", game.default_component || "Jade.GameView.CameraTargetCharacter");
  setVal("mcp-member-name", game.default_member || "targetPiece.stats.Health.maxValue");
  setVal("mcp-member-value", game.default_value || "23");
  setVal("mcp-profile-name", game.default_profile || "battlemarked_stat_hp_max");
  if (game.default_selector && Object.keys(game.default_selector).length > 0) {
    setVal("mcp-selector", JSON.stringify(game.default_selector));
  }

  setVal("gm-id", game.id || "");
  setVal("gm-name", game.name || "");
  setVal("gm-game-dir", game.game_dir || "");
  setVal("gm-runtime", game.runtime || "Unity_IL2CPP");
  setVal("gm-zip-path", game.bepinex_zip_path || "");
  setVal("gm-sha256", game.expected_sha256 || "");
  setVal("gm-plugin-dir", game.plugin_build_dir || "");
  setVal("gm-notes", game.notes || "");
  setVal("gm-def-comp", game.default_component || "");
  setVal("gm-def-member", game.default_member || "");
  setVal("gm-def-val", game.default_value || "");

  const tagsEl = document.getElementById("game-user-tags");
  const notesEl = document.getElementById("game-user-notes");
  if (tagsEl) tagsEl.value = Array.isArray(game.tags) ? game.tags.join(", ") : "";
  if (notesEl) notesEl.value = game.user_notes || "";

  syncModBuilderWithGame(game);
  loadGameSteamAssets(game);
  renderGamesTable();
  renderReadinessStrip(game);
  if (gameChanged || state.activeGameSnapshotsSlug !== game.id) {
    loadGameSnapshots(game.id);
    loadSetupChecklist(game.id);
    loadCurrentModsSection(game.id);
    loadPcOptimisation(false, game.id);
  } else {
    const sec = state.activeGameSection || "general";
    if (sec === "setup") loadSetupChecklist(game.id);
    if (sec === "mods") loadCurrentModsSection(game.id);
    if (sec === "pcopt") loadPcOptimisation(false, game.id);
  }
  switchActiveGameSection(state.activeGameSection || "general", { skipFetch: true });

  const hasCachedMeta = Boolean(game.metadata_scraped || game.modding_info?.metadata_scraped);
  const canInspectDisk = Boolean(game.dir_exists !== false && !game.missing_on_disk);
  if (allowAutoFetch && !hasCachedMeta && canInspectDisk) {
    _autoPopulateGameMetadata(game.id);
  }
}

export function applyGameSettingsToUI(game) {
  renderActiveGamePanel(game);
}

export async function loadGameSteamAssets(game) {
  const banner = document.getElementById("game-steam-banner");
  const heroImg = document.getElementById("game-steam-hero-img");
  const logoImg = document.getElementById("game-steam-logo-img");
  const exeIcon = document.getElementById("game-steam-exe-icon");
  const titleEl = document.getElementById("game-steam-banner-title");
  const appidBadge = document.getElementById("game-steam-appid-badge");
  const exeBadge = document.getElementById("game-steam-exe-badge");
  const headerImg = document.getElementById("game-steam-header-img");
  const capsuleImg = document.getElementById("game-steam-capsule-img");
  if (!banner || !game || !game.id) return;

  banner.style.display = "none";
  if (heroImg) heroImg.removeAttribute("src");
  if (logoImg) { logoImg.style.display = "none"; logoImg.removeAttribute("src"); }
  if (exeIcon) { exeIcon.style.display = "none"; exeIcon.removeAttribute("src"); }
  if (headerImg) { headerImg.style.display = "none"; headerImg.removeAttribute("src"); }
  if (capsuleImg) { capsuleImg.style.display = "none"; capsuleImg.removeAttribute("src"); }
  if (exeBadge) exeBadge.style.display = "none";

  try {
    let data = STEAM_ASSETS_JS_CACHE.get(game.id);
    if (!data) {
      const res = await apiFetch(`/api/games/${encodeURIComponent(game.id)}/steam-assets`);
      data = await res.json();
      STEAM_ASSETS_JS_CACHE.set(game.id, data);
    }
    if (!data || !data.has_assets) return;

    banner.style.display = "block";
    if (titleEl) {
      titleEl.textContent = game.name || game.id;
      titleEl.style.display = data.logo_url ? "none" : "block";
    }
    if (exeIcon && !data.logo_url && data.exe_icon_url) {
      exeIcon.src = data.exe_icon_url;
      exeIcon.style.display = "block";
    }
    if (appidBadge) {
      appidBadge.textContent = data.appid ? `Steam AppID ${data.appid}` : "Local Game Folder Assets";
    }
    if (exeBadge && data.primary_exe) {
      exeBadge.textContent = data.primary_exe;
      exeBadge.style.display = "inline-flex";
    }
    if (heroImg && data.hero_url) {
      heroImg.src = data.hero_url;
      heroImg.style.display = "block";
    } else if (heroImg && data.header_url) {
      heroImg.src = data.header_url;
      heroImg.style.display = "block";
    } else if (heroImg) {
      heroImg.style.display = "none";
    }
    if (logoImg && data.logo_url) {
      logoImg.src = data.logo_url;
      logoImg.style.display = "block";
    }
    if (headerImg && data.header_url) {
      headerImg.src = data.header_url;
      headerImg.style.display = "block";
    }
    if (capsuleImg && data.capsule_url) {
      capsuleImg.src = data.capsule_url;
      capsuleImg.style.display = "block";
    }
  } catch (err) {
    console.error("Failed to load Steam assets:", err);
  }
}

export async function saveActiveGameNotes() {
  const tagsRaw = document.getElementById("game-user-tags")?.value || "";
  const notes = document.getElementById("game-user-notes")?.value || "";
  const tags = tagsRaw.split(",").map(s => s.trim()).filter(Boolean);
  try {
    await apiPost(`/api/games/${encodeURIComponent(state.activeGameId)}/notes`, { tags, user_notes: notes });
    const out = document.getElementById("fp-output");
    if (out) {
      out.style.display = "block";
      out.textContent = "Saved game tags and notes.";
    }
    await refreshGamesList(false);
  } catch (err) {
    alert(`Failed to save notes: ${err.message || err}`);
  }
}

export async function launchActiveGame() {
  const out = document.getElementById("fp-output");
  try {
    const data = await apiPost(`/api/games/${encodeURIComponent(state.activeGameId)}/launch`, {});
    if (out) {
      out.style.display = "block";
      out.textContent = data?.summary || "Game launched.";
    }
  } catch (err) {
    if (out) { out.style.display = "block"; out.textContent = `Launch failed: ${err.message || err}`; }
  }
}

export async function checkActiveGameConflicts() {
  const out = document.getElementById("fp-output");
  try {
    const res = await apiFetch(`/api/games/${encodeURIComponent(state.activeGameId)}/conflicts`);
    const data = await res.json();
    if (out) {
      out.style.display = "block";
      out.textContent =
        `Mod Conflict & Load-Order Check (${data.plugin_count} plugin(s), ${data.issue_count} issue(s)):\n` +
        (data.issues?.length ? "ISSUES:\n- " + data.issues.join("\n- ") + "\n" : "No conflicts detected.\n") +
        (data.plugins?.length ? "INSTALLED:\n- " + data.plugins.map(p => `${p.name} (${p.path})`).join("\n- ") : "");
    }
  } catch (err) {
    if (out) { out.style.display = "block"; out.textContent = `Conflict check failed: ${err.message || err}`; }
  }
}

export function _renderConflictNotice(guard) {
  const noticeEl = document.getElementById("snapshot-conflict-notice");
  if (!noticeEl) return;
  const warnings = Array.isArray(guard?.warnings) ? guard.warnings : [];
  if (!warnings.length) {
    noticeEl.hidden = true;
    noticeEl.innerHTML = "";
    return;
  }
  noticeEl.hidden = false;
  noticeEl.innerHTML =
    `<strong>Conflict Guard Alert:</strong> ` +
    warnings.map(w => `<span><strong>${escHtml(w.title)}:</strong> ${escHtml(w.message)}</span>`).join("<br />");
}

function _setReadinessStep(stepKey, statusCls, detailText, fixLabel = "", showFix = false) {
  const card = document.getElementById(`rs-step-${stepKey}`);
  const detail = document.getElementById(`rs-detail-${stepKey}`);
  const btn = document.getElementById(`rs-fix-${stepKey}`);
  if (card) card.className = `readiness-step ${statusCls}`;
  if (detail) detail.textContent = detailText;
  if (btn) {
    btn.hidden = !showFix;
    if (fixLabel) btn.textContent = fixLabel;
  }
}

export function renderReadinessStrip(game) {
  if (!game) return;
  const info = game.modding_info || {};
  const { isUnity, isUnreal } = classifyGameEngine(game);
  const dirOk = Boolean(game.dir_exists !== false && !game.missing_on_disk);
  const scraped = Boolean(game.metadata_scraped || info.metadata_scraped);
  const acBlocked = info.mcp_category === "Online / Anti-Cheat" || (info.modding_readiness || "").startsWith("Blocked");

  // 1. Fingerprinted
  if (!dirOk) {
    _setReadinessStep("fingerprint", "blocked", "Game directory missing on disk", "Rescan", true);
  } else if (scraped) {
    _setReadinessStep("fingerprint", "done", `${info.mcp_category || game.detected_engine || "Detected"} (${info.scripting_backend || game.runtime || "PC"})`, "Run Fingerprint", false);
  } else {
    _setReadinessStep("fingerprint", "pending", "Engine / runtime not fingerprinted yet", "Run Fingerprint", true);
  }

  // 2. Clean Snapshot
  const snapList = state.activeGameSnapshotsSlug === game.id && Array.isArray(state.activeGameSnapshots)
    ? state.activeGameSnapshots
    : null;
  const snapCount = snapList ? snapList.length : Number(game.snapshots_count || 0);
  if (snapCount > 0) {
    const latest = snapList?.[0]?.snapshot_id ? ` · latest ${snapList[0].snapshot_id}` : "";
    _setReadinessStep("snapshot", "done", `${snapCount} snapshot(s) ready${latest}`, "Create Snapshot", false);
  } else {
    _setReadinessStep("snapshot", "pending", "No clean snapshot captured yet", "Create Snapshot", true);
  }

  // 3. Modding Stack Ready (+ Conflict Guard badge)
  const guard = info.conflict_guard || {};
  const conflictBadge = document.getElementById("rs-conflict-badge");
  const hasConflicts = Boolean(guard.has_conflicts && Array.isArray(guard.warnings) && guard.warnings.length > 0);
  if (conflictBadge) {
    conflictBadge.hidden = !hasConflicts;
    if (hasConflicts) {
      const mgrs = Array.isArray(guard.detected_managers) && guard.detected_managers.length
        ? guard.detected_managers.join("/")
        : "Proxy Conflict";
      conflictBadge.textContent = mgrs;
      conflictBadge.title = guard.summary || "Conflict Guard warning detected";
    }
  }
  _renderConflictNotice(hasConflicts ? guard : null);

  if (acBlocked) {
    _setReadinessStep("stack", "blocked", "Blocked by anti-cheat", "Inspect", true);
  } else if (isUnity) {
    const loaderOk = Boolean(info.doorstop_installed && info.bepinex_installed);
    _setReadinessStep(
      "stack",
      loaderOk ? (hasConflicts ? "warn" : "done") : "pending",
      loaderOk ? `BepInEx ${info.bepinex_version || "6.x"} + Doorstop ready` : "BepInEx + Doorstop not installed",
      "Install Loader",
      !loaderOk,
    );
  } else if (isUnreal) {
    const ueStackOk = Boolean(info.ue4ss_installed || info.unreal_settings?.ini_found);
    _setReadinessStep(
      "stack",
      ueStackOk ? (hasConflicts ? "warn" : "done") : "pending",
      ueStackOk
        ? `${info.ue4ss_installed ? "RE-UE4SS" : "Unreal INI"} verified`
        : "No RE-UE4SS or writable INI verified yet",
      "Rescan Config",
      !ueStackOk,
    );
  } else {
    _setReadinessStep(
      "stack",
      scraped && dirOk ? "done" : "pending",
      scraped ? `${info.mcp_category || "Generic"} workspace ready` : "Pending metadata scan",
      "Rescan",
      !(scraped && dirOk),
    );
  }

  // 4. Workspace / Bridge Ready
  if (acBlocked) {
    _setReadinessStep("bridge", "blocked", "Live modding disabled (Anti-Cheat)", "", false);
  } else if (isUnity) {
    const bridgeReady = Boolean(info.mcp_bridge_installed && (info.roslyn_Compiler_installed || !game.has_game_assembly));
    _setReadinessStep(
      "bridge",
      bridgeReady ? "done" : "pending",
      bridgeReady ? "MCP bridge plugin ready (test live session below)" : "BepInExMCP bridge plugin not deployed",
      bridgeReady ? "Test Bridge" : "Deploy Bridge",
      !bridgeReady,
    );
  } else if (isUnreal) {
    const iniFound = Boolean(info.unreal_settings?.ini_found);
    _setReadinessStep(
      "bridge",
      iniFound ? "done" : "pending",
      iniFound ? "Unreal .ini & ~mods workspace writable" : "GameUserSettings.ini not imported yet",
      "Rescan INI",
      !iniFound,
    );
  } else {
    _setReadinessStep(
      "bridge",
      scraped && dirOk ? "done" : "pending",
      scraped ? "Data override workspace ready" : "Run fingerprint to verify paths",
      "Rescan",
      !(scraped && dirOk),
    );
  }
}

export async function loadGameSnapshots(slug = state.activeGameId) {
  if (!slug) return;
  const listEl = document.getElementById("snapshot-list");
  const summaryEl = document.getElementById("snapshot-drawer-summary");
  try {
    const res = await apiGet(`/api/games/${encodeURIComponent(slug)}/snapshots`);
    const data = res?.data || {};
    const snaps = Array.isArray(data.snapshots) ? data.snapshots : [];
    state.activeGameSnapshots = snaps;
    state.activeGameSnapshotsSlug = slug;
    if (summaryEl) {
      summaryEl.textContent = `Snapshots & Rollback (${snaps.length})`;
    }
    if (listEl) {
      if (!snaps.length) {
        listEl.innerHTML = '<li class="snapshot-empty">No snapshots captured yet for this game.</li>';
      } else {
        listEl.innerHTML = snaps.map(s => `
          <li class="snapshot-item">
            <div>
              <strong>${escHtml(s.label || s.snapshot_id)}</strong>
              <span class="badge" style="margin-left:6px;">${s.file_count ?? 0} file(s)</span>
              <div style="font-size:0.7rem; color:var(--muted);">${escHtml(s.snapshot_id)} · ${escHtml(s.created_at || "")}</div>
            </div>
            <button type="button" class="secondary btn-sm" onclick="restoreGameSnapshot('${escHtml(s.snapshot_id)}')">Rollback</button>
          </li>
        `).join("");
      }
    }
    const activeGame = state.lastRenderedGame || state.managedGames.find(g => g.id === state.activeGameId);
    if (activeGame && activeGame.id === slug) {
      renderReadinessStrip(activeGame);
    }
  } catch (err) {
    console.warn("Failed to load game snapshots:", err);
  }
}

export async function createGameSnapshot(explicitLabel = "") {
  const btn = resolveBtn("btn-create-snapshot");
  const labelInput = document.getElementById("snapshot-label-input");
  const label = explicitLabel || (labelInput?.value || "").trim();
  const slug = state.activeGameId;
  if (!slug) return;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost(`/api/games/${encodeURIComponent(slug)}/snapshots`, { label });
        if (labelInput && !explicitLabel) labelInput.value = "";
        await loadGameSnapshots(slug);
        return res;
      },
      {
        pendingText: "Creating lightweight game snapshot...",
        successText: (d) => `Snapshot '${d?.snapshot_id || ""}' saved (${d?.file_count ?? 0} file(s))`,
      },
    );
  } catch {
    // Error handled by runTrackedAction
  }
}

export async function restoreGameSnapshot(snapshotId) {
  const btn = resolveBtn("");
  const slug = state.activeGameId;
  if (!slug || !snapshotId) return;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost(
          `/api/games/${encodeURIComponent(slug)}/snapshots/${encodeURIComponent(snapshotId)}/restore`,
          {},
        );
        await Promise.all([rescanActiveGameMetadata(), loadGameSnapshots(slug)]);
        return res;
      },
      {
        pendingText: `Rolling back to snapshot '${snapshotId}'...`,
        successText: (d) => d?.summary || `Restored snapshot '${snapshotId}'`,
      },
    );
  } catch {
    // Error handled by runTrackedAction
  }
}

export async function runReadinessFix(stepKey) {
  const game = state.lastRenderedGame || state.managedGames.find(g => g.id === state.activeGameId) || {};
  const { isUnity, isUnreal } = classifyGameEngine(game);
  if (stepKey === "fingerprint") {
    await runFingerprintAndRescan();
  } else if (stepKey === "snapshot") {
    await createGameSnapshot("Readiness Baseline Snapshot");
  } else if (stepKey === "stack") {
    if (isUnity) {
      const toolId = game.has_game_assembly ? "bepinex_il2cpp" : "bepinex_mono";
      await installModdingTool(toolId);
    } else if (isUnreal) {
      await importUnrealSettings("");
    } else {
      await runFingerprintAndRescan();
    }
  } else if (stepKey === "bridge") {
    if (isUnity) {
      const info = game.modding_info || {};
      if (!info.mcp_bridge_installed) {
        await installModdingTool("bepinex_mcp_il2cpp");
      } else {
        await mcpTestBridge();
      }
    } else if (isUnreal) {
      await importUnrealSettings("");
    } else {
      await runFingerprintAndRescan();
    }
  }
}

export async function refreshGameLogTail() {
  const slug = state.activeGameId;
  if (!slug) return null;
  const errOnly = Boolean(document.getElementById("log-tail-errors-only")?.checked);
  const pre = document.getElementById("game-log-tail-pre");
  const pathBadge = document.getElementById("log-tail-path-badge");
  const errPill = document.getElementById("log-tail-error-pill");

  const res = await apiGet(`/api/games/${encodeURIComponent(slug)}/log-tail?lines=100&errors_only=${errOnly ? "true" : "false"}`);
  if (!res.ok) {
    if (pre) pre.textContent = res.data?.detail || `Failed to read log tail (HTTP ${res.status})`;
    return null;
  }
  const d = res.data || {};
  if (pathBadge) pathBadge.textContent = d.log_path || "No game log file found yet";
  if (errPill) {
    const cnt = Number(d.error_count || 0);
    errPill.textContent = `${cnt} Error${cnt === 1 ? "" : "s"}`;
    errPill.className = cnt > 0 ? "pill no" : "pill yes";
  }
  if (pre) {
    const rawText = (d.text || "").trim();
    pre.textContent = rawText || (errOnly ? "(No matching error lines found in the active game log.)" : "(No game log file or log lines found yet. Launch the game with a mod loader installed.)");
    pre.scrollTop = pre.scrollHeight;
  }
  return d;
}

export async function openGameLogTailDrawer() {
  const drawer = document.getElementById("game-log-tail-drawer");
  if (drawer) drawer.style.display = "block";
  return refreshGameLogTail();
}

export async function toggleGameLogTail() {
  const drawer = document.getElementById("game-log-tail-drawer");
  if (!drawer) return;
  const opening = drawer.style.display === "none" || !drawer.style.display;
  drawer.style.display = opening ? "block" : "none";
  if (opening) {
    await refreshGameLogTail();
  }
}

export function closeGameLogTail() {
  const drawer = document.getElementById("game-log-tail-drawer");
  if (drawer) drawer.style.display = "none";
}

async function _copyTextWithFallback(text, fallbackLabel = "") {
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fallback below
  }
  const fpOut = document.getElementById("fp-output");
  if (fpOut) {
    fpOut.style.display = "block";
    fpOut.textContent = (fallbackLabel ? `${fallbackLabel}\n\n` : "") + text;
  }
  return false;
}

export async function copyCrashContext() {
  const btn = resolveBtn("btn-copy-crash-context");
  const slug = state.activeGameId;
  const game = state.lastRenderedGame || state.managedGames.find(g => g.id === slug) || {};
  if (!slug) return;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const errRes = await apiGet(`/api/games/${encodeURIComponent(slug)}/log-tail?lines=100&errors_only=true`);
        const fullRes = await apiGet(`/api/games/${encodeURIComponent(slug)}/log-tail?lines=30&errors_only=false`);
        const errData = errRes.data || {};
        const fullData = fullRes.data || {};
        const ctx = [
          `# Crash Context — ${game.name || slug}`,
          `- Game ID: ${slug}`,
          `- Engine / Runtime: ${game.runtime || game.modding_info?.mcp_category || "Unknown"}`,
          `- Active Log: ${fullData.log_path || errData.log_path || "None"}`,
          `- Total Error Matches: ${errData.error_count ?? fullData.error_count ?? 0}`,
          "",
          "## Error / Exception Lines",
          "```text",
          (errData.text || "").trim() || "(No explicit error lines detected.)",
          "```",
          "",
          "## Last 30 Log Lines",
          "```text",
          (fullData.text || "").trim() || "(No log tail available.)",
          "```",
        ].join("\n");
        await _copyTextWithFallback(ctx, "Crash Context:");
        return { error_count: errData.error_count ?? 0, chars: ctx.length };
      },
      {
        pendingText: "Gathering crash context from game logs...",
        successText: (d) => `Copied crash context (${d?.error_count ?? 0} error(s), ${d?.chars ?? 0} chars)`,
      },
    );
  } catch {
    // Error handled by runTrackedAction
  }
}

export async function copyAiModdingContext() {
  const btn = resolveBtn("btn-copy-ai-context");
  const slug = state.activeGameId;
  if (!slug) return;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiGet(`/api/games/${encodeURIComponent(slug)}/ai-context`);
        if (!res.ok) {
          throw new Error(res.data?.detail || `Failed to build AI context (HTTP ${res.status})`);
        }
        const md = res.data?.markdown || "";
        const copied = await _copyTextWithFallback(md, "AI Modding Context Pack:");
        return { ...res.data, copied };
      },
      {
        pendingText: "Building AI modding context pack...",
        successText: (d) => d?.copied
          ? `Copied AI context pack (${d?.char_count ?? 0} chars) to clipboard`
          : `Prepared AI context pack (${d?.char_count ?? 0} chars)`,
      },
    );
  } catch {
    // Error handled by runTrackedAction
  }
}

export function switchActiveGameSection(sectionKey = "general", { skipFetch = false } = {}) {
  const valid = ["general", "setup", "mods", "modding", "pcopt"];
  const key = valid.includes(sectionKey) ? sectionKey : "general";
  state.activeGameSection = key;
  for (const name of valid) {
    const btn = document.getElementById(`ag-tab-btn-${name}`);
    const pane = document.getElementById(`ag-pane-${name}`);
    const active = name === key;
    if (btn) btn.classList.toggle("active", active);
    if (pane) {
      pane.classList.toggle("active", active);
      pane.style.display = active ? "block" : "none";
    }
  }
  if (!skipFetch && state.activeGameId) {
    if (key === "setup") {
      loadSetupChecklist(state.activeGameId);
      loadGameSnapshots(state.activeGameId);
    } else if (key === "mods") {
      loadCurrentModsSection(state.activeGameId);
    } else if (key === "pcopt") {
      loadPcOptimisation(false, state.activeGameId);
    }
  }
}

export async function fullRestoreActiveGame() {
  const slug = state.activeGameId;
  const game = state.lastRenderedGame || state.managedGames.find(g => g.id === slug) || {};
  if (!slug) return;
  if (!confirm(`Full Restore (Rollback All) will revert '${game.name || slug}' to its earliest saved clean baseline snapshot and remove later mod/INI changes. Proceed?`)) {
    return;
  }
  const btn = resolveBtn("btn-full-restore-game");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost(`/api/games/${encodeURIComponent(slug)}/full-restore`, {});
        await Promise.all([
          rescanActiveGameMetadata(),
          loadGameSnapshots(slug),
          loadSetupChecklist(slug),
          loadCurrentModsSection(slug),
        ]);
        return res;
      },
      {
        pendingText: "Restoring earliest clean baseline snapshot...",
        successText: (d) => d?.summary || "Full Restore completed",
      },
    );
  } catch {
    // Handled by runTrackedAction
  }
}

export async function downloadAiInfoPack() {
  const slug = state.activeGameId;
  if (!slug) return;
  const btn = resolveBtn("btn-create-ai-pack");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiFetch(`/api/games/${encodeURIComponent(slug)}/ai-pack`, { method: "POST" });
        if (!res.ok) {
          let msg = `HTTP ${res.status}`;
          try {
            const j = await res.json();
            if (j?.detail) msg = j.detail;
          } catch {}
          throw new Error(msg);
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${slug}_ai_info_pack.zip`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1500);
        return { filename: `${slug}_ai_info_pack.zip`, bytes: blob.size };
      },
      {
        pendingText: "Packaging AI Info Pack (.zip)...",
        successText: (d) => `Downloaded ${d?.filename || "ai_info_pack.zip"} (${Math.max(1, Math.round((d?.bytes || 0) / 1024))} KB)`,
      },
    );
  } catch {
    // Handled by runTrackedAction
  }
}

export async function verifyActiveGameIntegrity() {
  const slug = state.activeGameId;
  if (!slug) return;
  const btn = resolveBtn("btn-verify-integrity");
  const out = document.getElementById("verify-integrity-output");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost(`/api/games/${encodeURIComponent(slug)}/verify-integrity`, { launch: true });
        if (out) {
          out.style.display = "block";
          const steps = Array.isArray(res?.instructions) ? res.instructions.join("\n") : "";
          out.textContent = `${res?.summary || "Integrity check prepared"}\n${steps}`;
        }
        return res;
      },
      {
        pendingText: "Checking platform integrity options...",
        successText: (d) => d?.summary || "Integrity verification ready",
      },
    );
  } catch (err) {
    if (out) {
      out.style.display = "block";
      out.textContent = `Verify integrity failed: ${err?.message || err}`;
    }
  }
}

export async function loadSetupChecklist(targetSlug = null) {
  const slug = targetSlug || state.activeGameId;
  const badge = document.getElementById("setup-checklist-badge");
  const grid = document.getElementById("setup-checklist-grid");
  if (!slug || !grid) return null;
  const res = await apiGet(`/api/games/${encodeURIComponent(slug)}/setup-status`);
  if (!res.ok || !res.data) return null;
  const d = res.data;
  if (badge) {
    badge.textContent = d.readiness_label || (d.all_ready ? "Ready" : "Setup Needed");
    badge.className = d.all_ready ? "pill yes" : "pill warn";
  }
  const steps = Array.isArray(d.steps) ? d.steps : [];
  grid.innerHTML = steps.map((st) => {
    const cls = st.status === "ready" ? "ok" : (st.status === "missing" ? "missing" : "pending");
    const pillCls = st.status === "ready" ? "yes" : (st.status === "missing" ? "no" : "warn");
    const pillTxt = st.status === "ready" ? "READY" : (st.status === "warning" ? "WARNING" : (st.status === "missing" ? "MISSING" : "PENDING"));
    return `
      <div class="step-card ${cls}">
        <div class="step-card-top">
          <span class="step-card-title">${escHtml(st.title || "")}</span>
          <span class="pill ${pillCls}">${pillTxt}</span>
        </div>
        <div class="step-card-detail">${escHtml(st.detail || "")}</div>
      </div>
    `;
  }).join("");
  return d;
}

export async function loadCurrentModsSection(targetSlug = null) {
  const slug = targetSlug || state.activeGameId;
  const tbody = document.getElementById("cm-mods-tbody");
  const pill = document.getElementById("cm-summary-pill");
  const mgrBadge = document.getElementById("cm-managers-badge");
  if (!slug || !tbody) return null;
  const res = await apiGet(`/api/games/${encodeURIComponent(slug)}/mods`);
  if (!res.ok || !res.data) {
    tbody.innerHTML = '<tr><td colspan="7">Could not scan installed mods.</td></tr>';
    return null;
  }
  const d = res.data;
  const mods = Array.isArray(d.mods) ? d.mods : [];
  if (pill) {
    pill.textContent = `${d.enabled_count ?? 0}/${d.total_count ?? mods.length} Enabled`;
    pill.className = mods.length ? "pill yes" : "pill na";
  }
  if (mgrBadge) {
    const tags = ["Filesystem"];
    if (d.vortex_detected) tags.push("Vortex");
    if (d.mo2_detected) tags.push("MO2");
    mgrBadge.textContent = tags.join(" + ");
  }
  if (!mods.length) {
    tbody.innerHTML = '<tr><td colspan="7">No BepInEx, MelonLoader, Unreal ~mods, UE4SS, or Vortex/MO2 mods detected.</td></tr>';
    return d;
  }
  tbody.innerHTML = mods.map((m, idx) => {
    const statusPill = m.enabled ? '<span class="pill yes">Enabled</span>' : '<span class="pill warn">Disabled</span>';
    const toggleBtn = m.can_toggle
      ? `<button type="button" class="secondary btn-sm cm-toggle-btn" data-idx="${idx}">${m.enabled ? "Disable" : "Enable"}</button>`
      : "";
    const removeBtn = m.can_remove
      ? `<button type="button" class="danger btn-sm cm-remove-btn" data-idx="${idx}">Remove</button>`
      : "";
    const actionHtml = (toggleBtn || removeBtn) ? `<div class="btn-group">${toggleBtn}${removeBtn}</div>` : '<span class="badge">Manifest</span>';
    return `
      <tr>
        <td><strong>${escHtml(m.name)}</strong></td>
        <td><span class="badge">${escHtml(m.engine_surface)}</span> <small>${escHtml(m.mod_type)}</small></td>
        <td>${statusPill}</td>
        <td>${escHtml(m.source || "Filesystem")}</td>
        <td>${m.size_kb ? `${m.size_kb} KB` : "—"}</td>
        <td><code>${escHtml(m.rel_path)}</code></td>
        <td>${actionHtml}</td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll(".cm-toggle-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const m = mods[Number(btn.dataset.idx)];
      if (!m) return;
      try {
        await runTrackedAction(
          btn,
          async () => {
            const resp = await apiPost(`/api/games/${encodeURIComponent(slug)}/mods/toggle`, {
              rel_path: m.rel_path,
              enabled: !m.enabled,
            });
            await loadCurrentModsSection(slug);
            return resp;
          },
          {
            pendingText: `${m.enabled ? "Disabling" : "Enabling"} ${m.name}...`,
            successText: (r) => r?.summary || "Updated mod status",
          },
        );
      } catch {
        // Error shown by runTrackedAction
      }
    });
  });

  tbody.querySelectorAll(".cm-remove-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const m = mods[Number(btn.dataset.idx)];
      if (!m) return;
      if (!confirm(`Remove '${m.rel_path}'? A pre-removal snapshot will be created automatically first.`)) return;
      try {
        await runTrackedAction(
          btn,
          async () => {
            const resp = await apiPost(`/api/games/${encodeURIComponent(slug)}/mods/remove`, {
              rel_path: m.rel_path,
            });
            await Promise.all([loadCurrentModsSection(slug), loadGameSnapshots(slug)]);
            return resp;
          },
          {
            pendingText: `Snapshotting & removing ${m.name}...`,
            successText: (r) => r?.summary || "Mod removed",
          },
        );
      } catch {
        // Error shown by runTrackedAction
      }
    });
  });
  return d;
}

export async function loadPcOptimisation(forceRefresh = false, targetSlug = null) {
  const slug = targetSlug || state.activeGameId;
  const tbody = document.getElementById("pcopt-rows-tbody");
  const specsEl = document.getElementById("pcopt-specs-summary");
  const overallPill = document.getElementById("pcopt-overall-pill");
  const gPill = document.getElementById("pcopt-green-pill");
  const aPill = document.getElementById("pcopt-amber-pill");
  const rPill = document.getElementById("pcopt-red-pill");
  const btnOptimal = document.getElementById("btn-pcopt-apply-optimal");
  const btnDefault = document.getElementById("btn-pcopt-apply-default");
  if (!slug || !tbody) return null;

  const res = await apiGet(`/api/games/${encodeURIComponent(slug)}/pc-optimisation?force_refresh=${forceRefresh ? "true" : "false"}`);
  if (!res.ok || !res.data) {
    tbody.innerHTML = '<tr><td colspan="6">Could not evaluate PC optimisation settings.</td></tr>';
    return null;
  }
  const d = res.data;
  const sp = d.specs_summary || {};
  const sum = d.summary || {};
  if (specsEl) {
    specsEl.innerHTML =
      `<strong>Hardware:</strong> ${escHtml(sp.cpu || "CPU")} (${sp.cores || "?"}C/${sp.threads || "?"}T) | ` +
      `<strong>GPU:</strong> ${escHtml(sp.gpu || "GPU")} (${sp.vram_gb ?? "?"} GB VRAM) | ` +
      `<strong>RAM:</strong> ${sp.ram_gb ?? "?"} GB | <strong>Display:</strong> ${escHtml(sp.display || "?")} @ ${sp.refresh_hz ?? "?"} Hz | ` +
      `<strong>Matched Tier:</strong> ${escHtml(d.matched_tier?.tier_name || "Auto")}`;
  }
  const ov = sum.overall_status || "green";
  if (overallPill) {
    overallPill.textContent = ov === "red" ? "RED — Bottleneck" : (ov === "amber" ? "AMBER — Sub-Optimal" : "GREEN — Optimal");
    overallPill.className = ov === "red" ? "pill no" : (ov === "amber" ? "pill warn" : "pill yes");
  }
  if (gPill) gPill.textContent = `${sum.green_count ?? 0} Green`;
  if (aPill) aPill.textContent = `${sum.amber_count ?? 0} Amber`;
  if (rPill) rPill.textContent = `${sum.red_count ?? 0} Red`;
  if (btnOptimal) btnOptimal.disabled = !d.can_apply_optimal;
  if (btnDefault) btnDefault.disabled = !d.can_apply_optimal;

  const rows = Array.isArray(d.rows) ? d.rows : [];
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6">No graphics or hardware settings found to evaluate.</td></tr>';
    return d;
  }
  tbody.innerHTML = rows.map((row, idx) => {
    const pillCls = row.status === "red" ? "no" : (row.status === "amber" ? "warn" : "yes");
    const lightTxt = (row.status || "green").toUpperCase();
    const actHtml = row.apply_supported
      ? `<div class="btn-group"><button type="button" class="success btn-sm pcopt-row-opt" data-idx="${idx}">Apply</button><button type="button" class="secondary btn-sm pcopt-row-def" data-idx="${idx}">Default</button></div>`
      : '<span class="badge">Advisory</span>';
    return `
      <tr>
        <td><span class="pill ${pillCls}">${lightTxt}</span></td>
        <td><strong>${escHtml(row.label)}</strong><div style="font-size:0.72rem; color:var(--muted);">${escHtml(row.category)} | <code>${escHtml(row.key)}</code></div></td>
        <td>${escHtml(row.current_value)}</td>
        <td style="color:#93c5fd;">${escHtml(row.recommended_value)}</td>
        <td style="font-size:0.76rem;">${escHtml(row.reason)}</td>
        <td>${actHtml}</td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll(".pcopt-row-opt").forEach((b) => {
    b.addEventListener("click", () => {
      const r = rows[Number(b.dataset.idx)];
      if (r) applyPcOptimisation("optimal", r.key, b);
    });
  });
  tbody.querySelectorAll(".pcopt-row-def").forEach((b) => {
    b.addEventListener("click", () => {
      const r = rows[Number(b.dataset.idx)];
      if (r) applyPcOptimisation("default", r.key, b);
    });
  });
  return d;
}

export async function applyPcOptimisation(mode = "optimal", settingKey = "", btnOverride = null) {
  const slug = state.activeGameId;
  if (!slug) return;
  const btn = btnOverride || resolveBtn(mode === "default" ? "btn-pcopt-apply-default" : "btn-pcopt-apply-optimal");
  const out = document.getElementById("pcopt-action-output");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost(`/api/games/${encodeURIComponent(slug)}/pc-optimisation/apply`, {
          mode,
          setting_key: settingKey || "",
        });
        if (out) {
          out.style.display = "block";
          out.textContent = res?.summary || "PC optimisation settings updated.";
        }
        await Promise.all([loadPcOptimisation(false, slug), loadGameSnapshots(slug)]);
        const active = state.lastRenderedGame || state.managedGames.find(g => g.id === slug);
        if (active && classifyGameEngine(active).isUnreal) {
          await importUnrealSettings("");
        }
        return res;
      },
      {
        pendingText: mode === "default" ? "Restoring default settings..." : "Applying optimal PC settings (auto-snapshot)...",
        successText: (d) => d?.summary || "Applied PC optimisation settings",
      },
    );
  } catch (err) {
    if (out) {
      out.style.display = "block";
      out.textContent = `PC optimisation apply failed: ${err?.message || err}`;
    }
  }
}

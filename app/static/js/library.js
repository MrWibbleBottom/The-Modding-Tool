import { apiFetch, apiGet, apiPost, runTrackedAction, setOutput, state, switchTab } from "./api.js";
import { loadLogs } from "./app_mgmt.js";
import { loadModdabilityGuide } from "./analyzer.js";
import { syncModBuilderWithGame } from "./mcp.js";

function _resolveLibBtn(fallbackId = "") {
  const evTarget = typeof window !== "undefined" ? window.event?.target : null;
  const fromEvent = evTarget && typeof evTarget.closest === "function" ? evTarget.closest("button") : null;
  return fromEvent || (fallbackId ? document.getElementById(fallbackId) : null);
}

let SORT_COLUMN = "name";
let SORT_DIRECTION = "asc";

let LIBRARY_ENTRIES = [];

export function toggleLibrarySubpanel(id) {
  for (const pid of ["lib-folder-bar", "lib-owned-bar", "lib-file-bar"]) {
    const el = document.getElementById(pid);
    if (!el) continue;
    el.style.display = (pid === id && el.style.display === "none") ? "block" : "none";
  }
}

export async function loadGameLibrary() {
  const res = await apiGet("/api/library");
  if (res.ok && res.data) {
    LIBRARY_ENTRIES = Array.isArray(res.data.entries) ? res.data.entries : [];
    const badge = document.getElementById("library-count-badge");
    if (badge) badge.textContent = String(LIBRARY_ENTRIES.length);
    renderGameLibraryCards();
  }
}

export async function scanLibraryInstalled(extraRoots = []) {
  const btn = _resolveLibBtn("btn-lib-scan-installed");
  const statusEl = document.getElementById("lib-status-bar");
  if (statusEl) statusEl.textContent = "Scanning Steam, Epic, GOG, and custom folders + fingerprinting engines...";
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/library/scan-installed", {
          custom_roots: extraRoots,
          save_custom_roots: true,
        });
        const data = res?.data ?? res;
        LIBRARY_ENTRIES = Array.isArray(data?.entries) ? data.entries : [];
        const badge = document.getElementById("library-count-badge");
        if (badge) badge.textContent = String(LIBRARY_ENTRIES.length);
        if (statusEl) {
          statusEl.innerHTML = `Discovered <strong>${data?.installed_count ?? 0}</strong> installed game(s) (${data?.total_count ?? LIBRARY_ENTRIES.length} total in library).`;
        }
        renderGameLibraryCards();
        return data;
      },
      {
        pendingText: "Scanning Steam, Epic, GOG, and custom libraries...",
        successText: (d) => `Discovered ${d?.installed_count ?? 0} installed game(s) (${d?.total_count ?? 0} total)`,
      },
    );
  } catch (err) {
    if (statusEl) statusEl.textContent = `Library scan failed: ${err?.message || err}`;
  }
}

export async function addCustomLibraryFolder() {
  const inp = document.getElementById("lib-custom-folder");
  const folder = (inp?.value || "").trim();
  if (!folder) return;
  await scanLibraryInstalled([folder]);
}

export async function importLibraryOwned() {
  const btn = _resolveLibBtn("btn-lib-run-owned");
  const statusEl = document.getElementById("lib-status-bar");
  if (statusEl) statusEl.textContent = "Importing owned Steam/GOG titles and querying PCGamingWiki metadata...";
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/library/import-owned", {
          steam_id: (document.getElementById("lib-steam-id")?.value || "").trim(),
          steam_api_key: (document.getElementById("lib-steam-key")?.value || "").trim(),
          include_gog: true,
          enrich_metadata: true,
        });
        const data = res?.data ?? res;
        LIBRARY_ENTRIES = Array.isArray(data?.entries) ? data.entries : [];
        const badge = document.getElementById("library-count-badge");
        if (badge) badge.textContent = String(LIBRARY_ENTRIES.length);
        if (statusEl) {
          statusEl.innerHTML = `Imported <strong>${data?.imported_count ?? 0}</strong> owned/uninstalled title(s) (${data?.total_count ?? LIBRARY_ENTRIES.length} total in library).`;
        }
        renderGameLibraryCards();
        return data;
      },
      {
        pendingText: "Importing owned Steam/GOG libraries & enriching PCGamingWiki...",
        successText: (d) => `Imported ${d?.imported_count ?? 0} owned title(s) (${d?.total_count ?? 0} total)`,
      },
    );
  } catch (err) {
    if (statusEl) statusEl.textContent = `Owned library import failed: ${err?.message || err}`;
  }
}

export function loadLibraryImportFile(inputEl) {
  const f = inputEl?.files?.[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    const ta = document.getElementById("lib-import-text");
    if (ta) ta.value = String(reader.result || "");
  };
  reader.readAsText(f);
}

export async function importLibraryFileText() {
  const txt = (document.getElementById("lib-import-text")?.value || "").trim();
  const statusEl = document.getElementById("lib-status-bar");
  if (!txt) return;
  const res = await apiPost("/api/library/import-file", { content: txt, enrich_metadata: true });
  if (res.ok && res.data) {
    LIBRARY_ENTRIES = Array.isArray(res.data.entries) ? res.data.entries : [];
    const badge = document.getElementById("library-count-badge");
    if (badge) badge.textContent = String(LIBRARY_ENTRIES.length);
    if (statusEl) {
      statusEl.innerHTML = `Imported <strong>${res.data.imported_count}</strong> title(s) from JSON/CSV (${res.data.total_count} total).`;
    }
    renderGameLibraryCards();
  }
}

export function renderGameLibraryCards() {
  const grid = document.getElementById("lib-cards-grid");
  if (!grid) return;
  const platF = document.getElementById("lib-filter-platform")?.value || "all";
  const statF = document.getElementById("lib-filter-status")?.value || "all";
  const engF = document.getElementById("lib-filter-engine")?.value || "all";
  const q = (document.getElementById("lib-filter-search")?.value || "").trim().toLowerCase();

  const filtered = LIBRARY_ENTRIES.filter(e => {
    const plat = (e.source_platform || "").toLowerCase();
    if (platF === "manual_import") {
      if (plat !== "manual" && plat !== "import") return false;
    } else if (platF !== "all" && plat !== platF) {
      return false;
    }
    if (statF !== "all" && e.install_state !== statF) return false;
    if (engF === "Restricted") {
      if (e.anti_cheat_status !== "Restricted") return false;
    } else if (engF === "Unity") {
      if (!(e.engine || "").startsWith("Unity")) return false;
    } else if (engF !== "all" && e.engine !== engF) {
      return false;
    }
    if (q) {
      const hay = `${e.title || ""} ${e.root_path || ""} ${e.engine || ""} ${e.recommended_adapter || ""} ${plat}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    grid.innerHTML = '<div class="step-card pending"><div class="step-card-title">No Matching Library Entries</div><div class="step-card-detail">Adjust your filters or run Scan Installed / Import Owned.</div></div>';
    return;
  }

  const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  grid.innerHTML = filtered.map((e, idx) => {
    const isInstalled = e.install_state === "installed";
    const isRestricted = e.anti_cheat_status === "Restricted";
    const cardCls = isRestricted ? "missing" : (isInstalled ? "ok" : "pending");
    const confLabel = e.detection_confidence === "verified_disk" ? "Verified" : "Estimated";
    const confPillCls = e.detection_confidence === "verified_disk" ? "yes" : "warn";
    const adapterCls = isRestricted ? "no" : (e.recommended_adapter === "bepinex-mcp" ? "yes" : "na");
    const clickable = isInstalled && !isRestricted;
    const idxInLib = LIBRARY_ENTRIES.indexOf(e);
    return `<div class="step-card ${cardCls}" ${clickable ? `style="cursor:pointer;" onclick="selectLibraryEntry(${idxInLib})"` : ""}>
      <div class="step-card-top">
        <span class="step-card-title">${esc(e.title)}</span>
        <span class="pill ${isInstalled ? "yes" : "na"}">${isInstalled ? "Installed" : "Not Installed"}</span>
      </div>
      <div style="display:flex; gap:6px; flex-wrap:wrap; margin:4px 0;">
        <span class="badge">${esc((e.source_platform || "").toUpperCase())}</span>
        <span class="badge">${esc(e.engine || "Unknown")}</span>
        <span class="pill ${confPillCls}">${confLabel}</span>
        <span class="pill ${adapterCls}">${esc(e.recommended_adapter || "unknown")}</span>
      </div>
      <div class="step-card-detail">${e.root_path ? esc(e.root_path) : "Not installed on disk (pre-install metadata)"}</div>
    </div>`;
  }).join("");
}

export function selectLibraryEntry(index) {
  const entry = LIBRARY_ENTRIES[index];
  if (!entry || entry.install_state !== "installed" || entry.anti_cheat_status === "Restricted" || !entry.root_path) {
    return;
  }
  const fpInput = document.getElementById("fp-game-dir");
  if (fpInput) fpInput.value = entry.root_path;

  const isUnity = (entry.engine || "").startsWith("Unity");
  const provCard = document.getElementById("card-provision-restore");
  const mcpCard = document.getElementById("card-live-mcp");
  if (provCard) provCard.style.display = isUnity ? "flex" : "none";
  if (mcpCard) mcpCard.style.display = isUnity ? "flex" : "none";

  const notice = document.getElementById("adapter-status-notice");
  const noticeTitle = document.getElementById("adapter-notice-title");
  const noticeBadge = document.getElementById("adapter-notice-badge");
  const noticeBody = document.getElementById("adapter-notice-body");
  if (notice && noticeTitle && noticeBadge && noticeBody) {
    notice.style.display = "flex";
    noticeTitle.textContent = `Selected Library Title: ${entry.title}`;
    noticeBadge.textContent = `${entry.engine} -> ${entry.recommended_adapter}`;
    noticeBody.innerHTML = isUnity
      ? `Unity engine detected (<code>${entry.engine}</code>). <strong>BepInEx provisioning</strong> and <strong>Live MCP</strong> controls are enabled below for <code>${entry.root_path}</code>.`
      : `Non-Unity engine detected (<code>${entry.engine}</code>). Recommended adapter: <strong>${entry.recommended_adapter}</strong>. BepInEx provisioning is hidden for this engine; target directory set to <code>${entry.root_path}</code>.`;
  }

  const titleEl = document.getElementById("info-title");
  if (titleEl) titleEl.textContent = `${entry.title} (${entry.id})`;
  switchTab("game");
}

export function toggleGameEditor() {
  const d = document.getElementById("game-editor-details");
  d.open = !d.open;
}

export async function browseIntoInput(inputId) {
  const inp = document.getElementById(inputId);
  const res = await apiPost("/api/games/browse", {
    initial_dir: inp ? inp.value : "",
    title: "Select Game or Platform Library Folder"
  });
  if (res.ok && res.data.status === "ok" && res.data.path) {
    inp.value = res.data.path;
  }
  return res.data;
}

export async function browseAndScan() {
  const picked = await browseIntoInput("scan-custom-path");
  if (picked && picked.status === "ok" && picked.path) {
    await scanPlatforms(picked.path);
  }
}

export async function scanCustomPathInput() {
  const customPath = document.getElementById("scan-custom-path").value.trim();
  await scanPlatforms(customPath);
}

let SCAN_PROGRESS_TIMER = null;
let SCAN_PROGRESS_CALLBACK = null;

export function updateScanProgressUI(data) {
  const wrap = document.getElementById("scan-progress-wrap");
  const label = document.getElementById("scan-progress-label");
  const pctEl = document.getElementById("scan-progress-pct");
  const bar = document.getElementById("scan-progress-bar");
  if (!data) return;
  const pct = Math.max(0, Math.min(100, Number(data.percent || 0)));
  const cur = Number(data.current || 0);
  const tot = Number(data.total || 0);
  const itemText = data.current_item ? ` — ${data.current_item}` : "";
  const phaseMsg = `${data.phase || "Scanning..."}${itemText}`;
  if (typeof SCAN_PROGRESS_CALLBACK === "function") {
    SCAN_PROGRESS_CALLBACK(pct, `${phaseMsg} (${pct}%)`);
  }
  if (!wrap) return;
  wrap.style.display = "block";
  if (label) label.textContent = phaseMsg;
  if (pctEl) pctEl.textContent = `${pct}% (${cur} / ${tot})`;
  if (bar) bar.style.width = `${pct}%`;
}

export function startScanProgressPolling(initialPhase = "Starting scan...", onProgress = null) {
  stopScanProgressPolling();
  SCAN_PROGRESS_CALLBACK = typeof onProgress === "function" ? onProgress : null;
  updateScanProgressUI({ phase: initialPhase, percent: 0, current: 0, total: 0, current_item: "" });
  SCAN_PROGRESS_TIMER = setInterval(async () => {
    try {
      const res = await apiFetch("/api/games/scan-progress");
      if (res.ok) {
        const data = await res.json();
        updateScanProgressUI(data);
      }
    } catch {}
  }, 200);
}

export function stopScanProgressPolling(finalPhase = "Scan complete") {
  if (SCAN_PROGRESS_TIMER) {
    clearInterval(SCAN_PROGRESS_TIMER);
    SCAN_PROGRESS_TIMER = null;
  }
  SCAN_PROGRESS_CALLBACK = null;
  const bar = document.getElementById("scan-progress-bar");
  const label = document.getElementById("scan-progress-label");
  const pctEl = document.getElementById("scan-progress-pct");
  if (bar) bar.style.width = "100%";
  if (label && finalPhase) label.textContent = finalPhase;
  if (pctEl && pctEl.textContent.startsWith("0%")) pctEl.textContent = "100%";
}

async function _executeScanAllGamesInfo(prefixText = "", onProgress = null) {
  const out = document.getElementById("scan-output");
  if (out) {
    out.style.display = "block";
    if (!prefixText) {
      out.textContent = "Scanning game folders on disk and populating Game Info metadata for all found games...";
    }
  }
  startScanProgressPolling("Scraping Game Info metadata...", onProgress);
  try {
    const res = await apiPost("/api/games/scan-info", {});
    const data = res?.data ?? res;
    const lines = (data?.results || []).map(
      r => `${r.name} (${r.id}): ${r.status}${r.mcp_category ? ` [${r.mcp_category} | Unity ${r.unity_version || "N/A"} | ${r.modding_readiness}]` : ""}`
    ).join("\n  ");
    const infoSummary = `${data?.summary || "Game info scan complete."}\nPopulated Game Info:\n  ${lines || "None"}`;
    if (out) out.textContent = prefixText ? `${prefixText}\n\n${infoSummary}` : infoSummary;
    stopScanProgressPolling("Game scan & info complete");
    await refreshGamesList(true);
    return data;
  } catch (err) {
    stopScanProgressPolling("Game info scan failed");
    if (out) out.textContent = (prefixText ? `${prefixText}\n\n` : "") + `Game info scan failed: ${err?.message || err}`;
    throw err;
  }
}

export async function scanPlatforms(customPath = "") {
  const out = document.getElementById("scan-output");
  const scanBtn = _resolveLibBtn("btn-game-scan");
  const scopeEl = document.getElementById("scan-unity-only");
  const discoveryScope = scopeEl ? scopeEl.value : "all";
  const scopeLabel = scopeEl && scopeEl.selectedOptions[0] ? scopeEl.selectedOptions[0].textContent : discoveryScope;
  if (out) {
    out.style.display = "block";
    out.textContent = customPath
      ? `Scanning folder '${customPath}' [Scope: ${scopeLabel}] and auto-populating Game Manager...`
      : `Scanning PC game platforms (Steam, Epic Games, GOG Galaxy, XboxGames, Ubisoft, EA) [Scope: ${scopeLabel}]...`;
  }
  try {
    await runTrackedAction(
      scanBtn,
      async ({ setProgress }) => {
        startScanProgressPolling("Discovering platform game folders...", setProgress);
        const res = await apiPost("/api/games/scan", {
          custom_path: customPath,
          auto_populate: true,
          unity_only: discoveryScope === "unity_all",
          discovery_scope: discoveryScope,
        });
        const data = res?.data ?? res;
        const rootsList = (data?.scanned_roots || []).map(r => `[${r.platform}] ${r.path}`).join("\n  ");
        const gamesList = (data?.discovered || []).map(g => `${g.name} [${g.runtime}] -> ${g.game_dir} (${g.scan_status})`).join("\n  ");
        const scanSummaryText =
          `${data?.summary || "Scan complete."}\n` +
          `Scanned Library Roots:\n  ${rootsList || "None"}\n` +
          `Discovered Games:\n  ${gamesList || "None"}`;
        if (out) out.textContent = `${scanSummaryText}\n\nAutomatically running Game info scan for all found games...`;
        const infoData = await _executeScanAllGamesInfo(scanSummaryText, setProgress);
        return { scan: data, info: infoData };
      },
      {
        pendingText: customPath ? `Scanning '${customPath}'...` : "Scanning game platforms & populating Game Info...",
        successText: (r) => r?.scan?.summary || "Platform & Game Info scan complete",
        mode: "determinate",
      },
    );
  } catch (err) {
    stopScanProgressPolling("Scan failed");
    if (out) out.textContent = `Scan failed: ${err?.message || err}`;
  }
}

export async function scanAllGamesInfo(prefixText = "") {
  const btn = _resolveLibBtn("btn-scan-game-info");
  try {
    await runTrackedAction(
      btn,
      async ({ setProgress }) => _executeScanAllGamesInfo(prefixText, setProgress),
      {
        pendingText: "Scraping Game Info metadata for managed games...",
        successText: (d) => d?.summary || "Game info scan complete",
        mode: "determinate",
      },
    );
  } catch {
    // Error UI and client-error audit handled by runTrackedAction
  }
}

export async function rescanActiveGameMetadata() {
  const res = await apiPost("/api/games/rescan", { game_id: state.activeGameId });
  const data = res?.data ?? res;
  if (data && data.game) {
    applyGameSettingsToUI(data.game);
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

  document.getElementById("info-title").textContent = `${game.name} (${game.id})`;

  const isUnity = Boolean(
    info.mcp_category === "Unity" ||
    (info.unity_version && info.unity_version !== "Unknown") ||
    (info.scripting_backend || "").includes("IL2CPP") ||
    (info.scripting_backend || "").includes("Mono")
  );
  const isUnreal = Boolean(
    info.is_unreal_engine ||
    info.mcp_category === "Unreal Engine 4/5" ||
    (info.scripting_backend || "").includes("Unreal")
  );

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
  const escHtml = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

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

  // 5. Game Settings Optimised for PC Specs
  const ueSettings = info.unreal_settings || null;
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
  const uiVis = (typeof APP_UI_CONFIG !== "undefined" && APP_UI_CONFIG.visibility) ? APP_UI_CONFIG.visibility : {};
  if (provCard) provCard.style.display = (isUnity && uiVis.sec_provision_restore !== false) ? "flex" : "none";
  if (mcpCard) mcpCard.style.display = (isUnity && uiVis.sec_live_mcp !== false) ? "flex" : "none";

  renderModdingAppsSection(game);
  renderUnrealSettingsSection(game);
}


export function renderReferenceArrayTableHtml(refArray, activeTierId) {
  if (!Array.isArray(refArray) || refArray.length === 0) {
    return '<tr><td colspan="9">No reference array loaded.</td></tr>';
  }
  return refArray.map(t => {
    const isMatched = t.tier_id === activeTierId;
    const st = t.scalability_targets || {};
    const rowStyle = isMatched ? 'background: rgba(16, 185, 129, 0.16); border-left: 3px solid #10b981;' : '';
    const matchTag = isMatched ? ' <span class="pill yes">ACTIVE TIER</span>' : '';
    return `<tr style="${rowStyle}">
      <td><strong>${t.tier_name || ""}</strong>${matchTag}</td>
      <td style="font-size:0.73rem;">${t.example_hardware || ""}</td>
      <td style="color:#60a5fa; font-weight:600;">${t.target_resolution || ""} (${t.recommended_res_scale || 100}% Scale)</td>
      <td>${(st["sg.TextureQuality"] || {}).label || "—"}</td>
      <td>${(st["sg.GlobalIlluminationQuality"] || {}).label || "—"}</td>
      <td>${(st["sg.ShadowQuality"] || {}).label || "—"}</td>
      <td>${(st["sg.ReflectionQuality"] || {}).label || "—"}</td>
      <td>VD: ${(st["sg.ViewDistanceQuality"] || {}).label || "—"} | Fol: ${(st["sg.FoliageQuality"] || {}).label || "—"}</td>
      <td style="color:#fbbf24; font-weight:600;">≤ ${t.vram_budget_ceiling_gb || "?"} GB</td>
    </tr>`;
  }).join("");
}

export function trafficLightToCardState(tl) {
  if (tl === "green") return { cardCls: "ok", pillCls: "yes" };
  if (tl === "red") return { cardCls: "missing", pillCls: "no" };
  return { cardCls: "pending", pillCls: "warn" };
}

export function renderUnrealSettingsSection(game) {
  const section = document.getElementById("unreal-settings-section");
  if (!section || !game) return;
  const info = game.modding_info || {};
  const isUnreal = Boolean(
    info.is_unreal_engine ||
    info.mcp_category === "Unreal Engine 4/5" ||
    (info.sheet_engine_tech || "").includes("Unreal") ||
    (info.scripting_backend || "").includes("Unreal") ||
    (game.runtime || "").includes("Unreal") ||
    info.unreal_settings
  );

  if (!isUnreal) {
    section.style.display = "none";
    return;
  }
  section.style.display = "block";
  const ue = info.unreal_settings || {};
  const activeTier = ue.active_reference_tier || {};
  const activeTierId = activeTier.tier_id || "tier_flagship_24gb_4090";

  // Sync Reference Tier selector
  const tierSel = document.getElementById("ue-ref-tier-select");
  if (tierSel && ue.active_reference_tier_id) {
    tierSel.value = ue.active_reference_tier_id;
  }

  // Populate local profile dropdown
  const profSel = document.getElementById("ue-local-profile-select");
  if (profSel) {
    const profiles = Array.isArray(ue.available_local_profiles) ? ue.available_local_profiles : [];
    const currentIni = (ue.ini_path || "").toLowerCase();
    profSel.innerHTML = profiles.map(p => {
      const isSel = (p.ini_path || "").toLowerCase() === currentIni;
      return `<option value="${p.ini_path}" ${isSel ? "selected" : ""}>[${p.project_name}] ${p.ini_path}</option>`;
    }).join("");
  }

  // Populate Reference Array Matrix table inside Unreal Settings section
  const refTbody = document.getElementById("ue-reference-array-tbody");
  if (refTbody && Array.isArray(ue.reference_array)) {
    refTbody.innerHTML = renderReferenceArrayTableHtml(ue.reference_array, activeTierId);
  }

  // Source banner
  const srcBanner = document.getElementById("ue-ini-source-banner");
  if (srcBanner) {
    const metrics = ue.summary_metrics || {};
    if (ue.ini_found && ue.ini_path) {
      srcBanner.innerHTML =
        `<strong>Imported Unreal Engine Config (Project <code>${ue.detected_project_name || "UE"}</code>):</strong> ` +
        `<code>${ue.ini_path}</code> | ` +
        `<strong>Resolution:</strong> ${metrics.resolution || "—"} (${metrics.fullscreen_mode || "—"}) | ` +
        `<strong>Avg Scalability:</strong> <span style="color:#34d399; font-weight:700;">${metrics.average_scalability_label || "Epic"}</span> | ` +
        `<strong>Est. VRAM Pool:</strong> ~${metrics.estimated_vram_gb || "—"} GB`;
    } else {
      srcBanner.innerHTML =
        `<strong>Unreal Project Detected (<code>${ue.detected_project_name || game.name}</code>):</strong> ` +
        `No saved <code>GameUserSettings.ini</code> found yet in <code>%LOCALAPPDATA%\\${ue.detected_project_name || ""}\\Saved\\Config</code> ` +
        `(launch the game once to generate its INI, or select any discovered config profile from the dropdown above).`;
    }
  }

  // Traffic-Light Summary Bar
  const tlSum = ue.traffic_light_summary || {};
  const comp = ue.spec_comparison || {};
  const overallLight = tlSum.overall_light || comp.overall_light || "green";
  const overallStyles = trafficLightToCardState(overallLight);
  const verdictPill = document.getElementById("ue-overall-verdict-pill");
  if (verdictPill) {
    verdictPill.className = `pill ${overallStyles.pillCls}`;
    verdictPill.textContent = tlSum.overall_verdict || comp.overall_verdict || "Comparison ready";
  }
  const gEl = document.getElementById("ue-tl-green-count");
  const aEl = document.getElementById("ue-tl-amber-count");
  const rEl = document.getElementById("ue-tl-red-count");
  const mEl = document.getElementById("ue-matched-tier-badge");
  if (gEl) gEl.textContent = `${tlSum.green_count ?? 0} Optimal`;
  if (aEl) aEl.textContent = `${tlSum.amber_count ?? 0} Sub-Optimal`;
  if (rEl) rEl.textContent = `${tlSum.red_count ?? 0} Bottleneck`;
  if (mEl) mEl.textContent = `${activeTier.matched_mode || "Matched Tier"}: ${activeTier.tier_name || "—"}`;

  // Per-category consolidated traffic-light pills
  const _summarizeCategoryLight = (items, getter) => {
    if (!Array.isArray(items) || items.length === 0) return { cls: "na", label: "N/A" };
    let hasRed = false, hasAmber = false;
    for (const it of items) {
      const tl = getter(it);
      if (tl === "red") hasRed = true;
      else if (tl === "amber") hasAmber = true;
    }
    if (hasRed) return { cls: "no", label: "Bottleneck" };
    if (hasAmber) return { cls: "warn", label: "Sub-Optimal" };
    return { cls: "yes", label: "Optimal" };
  };
  const hwCat = _summarizeCategoryLight(comp.items, it => it.traffic_light || (it.state === "ok" ? "green" : (it.state === "missing" ? "red" : "amber")));
  const perfCat = _summarizeCategoryLight(ue.performance_settings, it => it.traffic_light || "green");
  const fidCat = _summarizeCategoryLight(ue.fidelity_settings, it => it.traffic_light || "green");
  const hwPill = document.getElementById("ue-tl-hw-pill");
  const perfPill = document.getElementById("ue-tl-perf-pill");
  const fidPill = document.getElementById("ue-tl-fid-pill");
  if (hwPill) { hwPill.className = `pill ${hwCat.cls}`; hwPill.textContent = `Hardware: ${hwCat.label}`; }
  if (perfPill) { perfPill.className = `pill ${perfCat.cls}`; perfPill.textContent = `Performance: ${perfCat.label}`; }
  if (fidPill) { fidPill.className = `pill ${fidCat.cls}`; fidPill.textContent = `Fidelity: ${fidCat.label}`; }

  // 1. PC Specs vs Current Settings Comparison Cards
  const compGrid = document.getElementById("ue-comparison-grid");
  if (compGrid) {
    const items = Array.isArray(comp.items) ? comp.items : [];
    compGrid.innerHTML = items.map(item => {
      const tl = item.traffic_light || (item.state === "ok" ? "green" : (item.state === "missing" ? "red" : "amber"));
      const stMap = trafficLightToCardState(tl);
      return `<div class="step-card ${stMap.cardCls}">
        <div class="step-card-top">
          <span class="step-card-title">${item.aspect || ""}</span>
          <span class="pill ${stMap.pillCls}">${item.verdict || ""}</span>
        </div>
        <div style="font-size:0.75rem; color:#93c5fd; margin-top:2px;"><strong>Hardware Profile:</strong> ${item.pc_spec || "—"}</div>
        <div style="font-size:0.75rem; color:#fde68a;"><strong>Current Game Setting:</strong> ${item.game_setting || "—"}</div>
        <div style="font-size:0.75rem; color:#34d399;"><strong>Reference Target:</strong> ${item.recommended_setting || "—"}</div>
        <div style="font-size:0.73rem; color:#cbd5e1; margin-top:3px; line-height:1.35;">${item.recommendation || ""}</div>
      </div>`;
    }).join("");
  }

  // 2. Key Performance Settings (Traffic-Light Evaluated)
  const perfGrid = document.getElementById("ue-performance-grid");
  if (perfGrid) {
    const perfItems = Array.isArray(ue.performance_settings) ? ue.performance_settings : [];
    perfGrid.innerHTML = perfItems.map(p => {
      const stMap = trafficLightToCardState(p.traffic_light || "green");
      return `
      <div class="step-card ${stMap.cardCls}">
        <div class="step-card-top">
          <span class="step-card-title">${p.name || ""}</span>
          <span class="pill ${stMap.pillCls}">${p.light_badge || "OPTIMAL"}</span>
        </div>
        <div class="step-card-label" style="font-size:0.85rem;">Current: ${p.value || "—"}</div>
        <div style="font-size:0.75rem; color:#93c5fd;"><strong>Tier Target:</strong> ${p.recommended_value || "—"}</div>
        <div style="font-size:0.73rem; color:#e2e8f0; margin-top:2px;">${p.advice || p.note || ""}</div>
        <div class="step-card-detail">INI Key: ${p.key || ""} (${p.impact || ""})</div>
      </div>
    `;
    }).join("");
  }

  // 3. Key Visual Fidelity Settings (Traffic-Light Evaluated)
  const fidGrid = document.getElementById("ue-fidelity-grid");
  if (fidGrid) {
    const fidItems = Array.isArray(ue.fidelity_settings) ? ue.fidelity_settings : [];
    fidGrid.innerHTML = fidItems.map(f => {
      const stMap = trafficLightToCardState(f.traffic_light || "green");
      return `
      <div class="step-card ${stMap.cardCls}">
        <div class="step-card-top">
          <span class="step-card-title">${f.name || ""}</span>
          <span class="pill ${stMap.pillCls}">${f.light_badge || "OPTIMAL"}</span>
        </div>
        <div class="step-card-label" style="font-size:0.85rem;">Current: ${f.value || "—"}</div>
        <div style="font-size:0.75rem; color:#c084fc;"><strong>Tier Target:</strong> ${f.recommended_value || "—"}</div>
        <div style="font-size:0.73rem; color:#e2e8f0; margin-top:2px;">${f.advice || f.note || ""}</div>
        <div class="step-card-detail">INI Key: ${f.key || ""} (${f.impact || ""})</div>
      </div>
    `;
    }).join("");
  }
}

export async function onUnrealReferenceTierChanged() {
  const sel = document.getElementById("ue-local-profile-select");
  const chosenPath = sel ? sel.value : "";
  await importUnrealSettings(chosenPath);
}

export async function importUnrealSettings(customPath = "") {
  const srcBanner = document.getElementById("ue-ini-source-banner");
  const tierSel = document.getElementById("ue-ref-tier-select");
  const refTierId = tierSel ? tierSel.value : "auto";
  if (srcBanner) {
    srcBanner.textContent = "Evaluating Unreal Engine settings against Traffic-Light Hardware Reference Array...";
  }
  const res = await apiPost("/api/games/unreal-settings", {
    game_id: state.activeGameId,
    custom_ini_path: customPath,
    reference_tier_id: refTierId
  });
  if (res.ok && res.data.game) {
    applyGameSettingsToUI(res.data.game);
    await refreshGamesList(false);
  }
}

export async function onConfigProfileSelected() {
  const sel = document.getElementById("ue-local-profile-select");
  const chosenPath = sel ? sel.value : "";
  const activeGame = state.lastRenderedGame || state.managedGames.find(g => g.id === state.activeGameId);
  const info = activeGame?.modding_info || {};
  const isUnreal = Boolean(
    info.is_unreal_engine ||
    info.mcp_category === "Unreal Engine 4/5" ||
    (info.sheet_engine_tech || "").includes("Unreal") ||
    (info.scripting_backend || "").includes("Unreal") ||
    (activeGame?.runtime || "").includes("Unreal") ||
    info.unreal_settings
  );
  if (isUnreal) {
    await importUnrealSettings(chosenPath);
  } else if (chosenPath) {
    await rescanActiveGameMetadata();
  }
}

export async function importSelectedUnrealProfile() {
  await onConfigProfileSelected();
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
      ? `<div class="step-card blocked"><div class="step-card-title">Modding Tools Not Recommended (Anti-Cheat Protected)</div><div class="step-card-label">This title uses kernel/online anti-cheat (${_mbEsc(info.sheet_reference_match || info.mcp_category)}). Mod loaders are disabled by default; switch the dropdown to 'All Modding Apps' only if inspecting offline tools.</div></div>`
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

export async function installModdingTool(toolId) {
  const out = document.getElementById("tools-install-output");
  const btn = _resolveLibBtn(`btn-tool-${toolId}`);
  const gameDir = document.getElementById("fp-game-dir")?.value || "";
  if (out) {
    out.style.display = "block";
    out.textContent = `Installing / deploying modding tool '${toolId}'...`;
  }
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/tools/install", {
          tool_id: toolId,
          game_id: state.activeGameId,
          game_dir: gameDir,
        });
        const data = res?.data ?? res;
        if (out) {
          out.textContent = `${data.message}\nSHA-256: ${data.sha256}\nTarget Folder: ${data.install_dir}`;
        }
        if (data?.game) {
          applyGameSettingsToUI(data.game);
        }
        await refreshGamesList(false);
        return data;
      },
      {
        pendingText: `Installing ${toolId}...`,
        successText: (d) => d?.message || `Installed ${toolId}`,
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
  const btn = _resolveLibBtn("btn-install-all-rec");
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
  const out = document.getElementById("tools-install-output");
  const res = await apiPost("/api/tools/open-folder", { path: pathStr });
  if (!res.ok && out) {
    out.style.display = "block";
    out.textContent = `Could not open folder '${pathStr}': ${JSON.stringify(res.data)}`;
  }
}

export function applyGameSettingsToUI(game) {
  if (!game) return;
  state.activeGameId = game.id;
  document.getElementById("active-game-badge").textContent = `Loaded: ${game.name}`;
  document.getElementById("game-tab-title-badge").textContent = game.name;
  renderTopGameInfoPanel(game);

  // Populate Fingerprint / Provision / MCP inputs
  document.getElementById("fp-game-dir").value = game.game_dir || "";
  document.getElementById("prov-zip-path").value = game.bepinex_zip_path || "";
  document.getElementById("prov-sha256").value = game.expected_sha256 || "";
  document.getElementById("prov-plugin-dir").value = game.plugin_build_dir || "";
  document.getElementById("mcp-python").value = game.venv_python_path || "";
  document.getElementById("mcp-app").value = game.mcp_app_path || "";

  if (game.default_entity && ENTITIES[game.default_entity]) {
    document.getElementById("mcp-target-entity").value = game.default_entity;
  }
  if (game.default_variable && VARIABLES[game.default_variable]) {
    document.getElementById("mcp-variable-select").value = game.default_variable;
  }
  document.getElementById("mcp-go-id").value = game.default_go_id || "-269924";
  document.getElementById("mcp-comp-name").value = game.default_component || "Jade.GameView.CameraTargetCharacter";
  document.getElementById("mcp-member-name").value = game.default_member || "targetPiece.stats.Health.maxValue";
  document.getElementById("mcp-member-value").value = game.default_value || "23";
  document.getElementById("mcp-profile-name").value = game.default_profile || "battlemarked_stat_hp_max";
  if (game.default_selector && Object.keys(game.default_selector).length > 0) {
    document.getElementById("mcp-selector").value = JSON.stringify(game.default_selector);
  }

  // Populate Game Manager editor form
  document.getElementById("gm-id").value = game.id || "";
  document.getElementById("gm-name").value = game.name || "";
  document.getElementById("gm-game-dir").value = game.game_dir || "";
  document.getElementById("gm-runtime").value = game.runtime || "Unity_IL2CPP";
  document.getElementById("gm-zip-path").value = game.bepinex_zip_path || "";
  document.getElementById("gm-sha256").value = game.expected_sha256 || "";
  document.getElementById("gm-plugin-dir").value = game.plugin_build_dir || "";
  document.getElementById("gm-notes").value = game.notes || "";
  document.getElementById("gm-def-comp").value = game.default_component || "";
  document.getElementById("gm-def-member").value = game.default_member || "";
  document.getElementById("gm-def-val").value = game.default_value || "";

  const tagsEl = document.getElementById("game-user-tags");
  const notesEl = document.getElementById("game-user-notes");
  if (tagsEl) tagsEl.value = Array.isArray(game.tags) ? game.tags.join(", ") : "";
  if (notesEl) notesEl.value = game.user_notes || "";

  syncModBuilderWithGame(game);
  loadGameSteamAssets(game);
}

const STEAM_ASSETS_JS_CACHE = new Map();

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

export function getSortValue(game, col) {
  const mi = game.modding_info || {};
  switch (col) {
    case "id": return (game.id || "").toLowerCase();
    case "name": return (game.name || "").toLowerCase();
    case "category": return (mi.mcp_category || game.runtime || "").toLowerCase();
    case "unity_version": return (mi.unity_version && mi.unity_version !== "Unknown" ? mi.unity_version : (mi.scripting_backend || "")).toLowerCase();
    case "stack": return (mi.recommended_mcp_stack || "").toLowerCase();
    case "readiness": return (mi.modding_readiness || "").toLowerCase();
    default: return (game.name || "").toLowerCase();
  }
}

export function setSortColumn(col) {
  if (SORT_COLUMN === col) {
    SORT_DIRECTION = SORT_DIRECTION === "asc" ? "desc" : "asc";
  } else {
    SORT_COLUMN = col;
    SORT_DIRECTION = "asc";
  }
  renderGamesTable();
}

export function onSortSelectChanged() {
  const val = document.getElementById("gm-sort-select").value;
  const parts = val.split(":");
  SORT_COLUMN = parts[0] || "name";
  SORT_DIRECTION = parts[1] || "asc";
  renderGamesTable();
}

export function renderGamesTable() {
  const tbody = document.getElementById("games-tbody");
  const filterText = (document.getElementById("gm-filter-text")?.value || "").trim().toLowerCase();
  const filterCat = document.getElementById("gm-filter-category")?.value || "ALL";
  const filterReady = document.getElementById("gm-filter-readiness")?.value || "ALL";

  for (const col of ["id", "name", "category", "unity_version", "stack", "readiness"]) {
    const ind = document.getElementById(`sort-ind-${col}`);
    if (ind) {
      ind.textContent = SORT_COLUMN === col ? (SORT_DIRECTION === "asc" ? "▲" : "▼") : "";
    }
  }

  const filtered = state.managedGames.filter(g => {
    const mi = g.modding_info || {};
    const cat = mi.mcp_category || "Unknown";
    const backend = (mi.scripting_backend || "").toLowerCase();
    const tech = (mi.sheet_engine_tech || "").toLowerCase();
    const rt = (g.runtime || "").toLowerCase();
    const readyStr = mi.modding_readiness || "";

    if (filterCat !== "ALL") {
      if (filterCat === "Unity_IL2CPP") {
        if (!(rt === "unity_il2cpp" || backend.includes("il2cpp") || g.has_game_assembly)) return false;
      } else if (filterCat === "Unity_Mono") {
        if (!(rt === "unity_mono" || backend.includes("mono"))) return false;
      } else if (filterCat === "Unreal Engine 3") {
        if (!(rt === "unreal engine 3" || tech.includes("unreal engine 3"))) return false;
      } else if (filterCat === "RE Engine") {
        if (!(rt === "re engine" || tech.includes("re engine"))) return false;
      } else if (filterCat === "FromSoftware") {
        if (!(rt === "fromsoftware" || tech.includes("fromsoftware"))) return false;
      } else if (cat !== filterCat && g.runtime !== filterCat) {
        return false;
      }
    }
    if (filterReady === "READY" && !(g.interop_ready && g.plugin_installed)) {
      return false;
    }
    if (filterReady === "UNSCRAPED" && g.metadata_scraped) {
      return false;
    }
    if (filterReady === "MISSING" && !g.missing_on_disk) {
      return false;
    }
    if (filterReady === "UNITY" && cat !== "Unity") {
      return false;
    }
    if (filterReady === "BLOCKED" && cat !== "Online / Anti-Cheat") {
      return false;
    }
    if (filterText) {
      const haystack = [
        g.id,
        g.name,
        g.game_dir,
        cat,
        mi.unity_version,
        mi.scripting_backend,
        mi.recommended_mcp_stack,
        mi.sheet_reference_match,
        readyStr
      ].join(" ").toLowerCase();
      if (!haystack.includes(filterText)) {
        return false;
      }
    }
    return true;
  });

  filtered.sort((a, b) => {
    const favA = a.favorite ? 1 : 0;
    const favB = b.favorite ? 1 : 0;
    if (favA !== favB) return favB - favA;
    const va = getSortValue(a, SORT_COLUMN);
    const vb = getSortValue(b, SORT_COLUMN);
    if (va < vb) return SORT_DIRECTION === "asc" ? -1 : 1;
    if (va > vb) return SORT_DIRECTION === "asc" ? 1 : -1;
    return 0;
  });

  document.getElementById("manager-count-badge").textContent = `${filtered.length} / ${state.managedGames.length}`;

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6">No managed games match the current filter.</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.map(g => {
    const isAct = g.id === state.activeGameId;
    const mi = g.modding_info || {};
    const isMissing = Boolean(g.missing_on_disk);
    const isScraped = Boolean(g.metadata_scraped);
    const cat = isScraped ? (mi.mcp_category || g.runtime) : `${g.runtime} (Pending Scrape)`;
    const engVer = !isScraped
      ? "Click Load Game to scrape"
      : ((mi.unity_version && mi.unity_version !== "Unknown")
          ? `Unity ${mi.unity_version} (${mi.scripting_backend || ""})`
          : (mi.scripting_backend || g.runtime));

    let statusPill = `<span class="pill yes">${mi.modding_readiness || "Scraped"}</span>`;
    if (isMissing) {
      statusPill = `<span class="pill no" title="Folder not found on disk. Loading this game will display its previously downloaded metadata.">Missing${isScraped ? " (Cached Info)" : ""}</span>`;
    } else if (!isScraped) {
      statusPill = `<span class="pill na">Pending 1st Load</span>`;
    } else if (mi.mcp_category === "Online / Anti-Cheat") {
      statusPill = `<span class="pill no">Blocked (Anti-Cheat)</span>`;
    } else if (!g.plugin_installed && mi.mcp_category === "Unity") {
      statusPill = `<span class="pill warn">Unprovisioned</span>`;
    }

    const delBtn = g.id === "battleground"
      ? ""
      : `<button class="danger btn-sm" onclick="deleteManagedGame('${g.id}')">Remove</button>`;
    const missingTag = isMissing
      ? `<span class="pill no" style="margin-left:6px;">Missing Folder</span>`
      : "";
    const pinLabel = g.favorite ? "Pinned" : "Pin";
    const pinCls = g.favorite ? "btn-sm" : "secondary btn-sm";
    return `<tr style="${isAct ? 'background: rgba(59, 130, 246, 0.14);' : (isMissing ? 'background: rgba(239, 68, 68, 0.07);' : '')}">
      <td>
        <div style="display:flex; align-items:center; gap:10px; min-width:0;">
          <img src="/api/games/${encodeURIComponent(g.id)}/icon" alt="" width="24" height="24" loading="lazy"
               style="width:24px; height:24px; border-radius:4px; object-fit:contain; flex-shrink:0; background:#0b0f17; border:1px solid var(--border);"
               onerror="this.style.display='none'" />
          <div style="min-width:0;">
            <div style="font-weight:600; color:#fff;">${isAct ? '<span class="badge" style="margin-right:6px;">Loaded</span>' : ''}${g.favorite ? '<span class="badge" style="margin-right:6px; color:#fbbf24;">Pinned</span>' : ''}${g.name}${missingTag}</div>
            <div style="font-size:0.72rem; color:var(--muted);">${g.game_dir}</div>
          </div>
        </div>
      </td>
      <td class="col-cat">${cat}</td>
      <td class="col-eng">${engVer}</td>
      <td class="col-stack" style="color:#34d399;">${isScraped ? (mi.recommended_mcp_stack || "—") : "Scraped on 1st Load"}</td>
      <td>${statusPill}</td>
      <td>
        <div class="btn-group">
          <button class="success btn-sm" onclick="loadAndOpenGame('${g.id}')">Load Game</button>
          <button class="${pinCls}" onclick="toggleFavoriteGame('${g.id}', ${!g.favorite})">${pinLabel}</button>
          <button class="secondary btn-sm" onclick="editGameInManager('${g.id}')">Edit</button>
          ${delBtn}
        </div>
      </td>
    </tr>`;
  }).join("");
}

export function _applyGamesPayload(data, applyActive = false) {
  if (!data) return;
  state.managedGames = data.games || [];
  state.activeGameId = data.active_game_id || "battleground";
  document.getElementById("games-store-path").textContent = data.store_path || "";

  const sel = document.getElementById("global-game-select");
  if (sel) {
    sel.innerHTML = state.managedGames.map(g => {
      const mi = g.modding_info || {};
      const uver = (mi.unity_version && mi.unity_version !== "Unknown") ? `Unity ${mi.unity_version}` : (mi.mcp_category || g.runtime);
      return `<option value="${g.id}" ${g.id === state.activeGameId ? "selected" : ""}>${g.name} [${uver}]</option>`;
    }).join("");
  }

  renderGamesTable();

  const activeGame = state.managedGames.find(g => g.id === state.activeGameId) || state.managedGames[0];
  if (activeGame) {
    if (applyActive) {
      applyGameSettingsToUI(activeGame);
    } else {
      renderTopGameInfoPanel(activeGame);
    }
  }
}

export async function refreshGamesList(applyActive = false) {
  if (!state.managedGames.length) {
    try {
      const cachedRaw = localStorage.getItem("modtool_cache_games");
      if (cachedRaw) _applyGamesPayload(JSON.parse(cachedRaw), applyActive);
    } catch {}
  }
  try {
    const res = await apiFetch("/api/games");
    const data = await res.json();
    try { localStorage.setItem("modtool_cache_games", JSON.stringify(data)); } catch {}
    _applyGamesPayload(data, applyActive);
  } catch (err) {
    console.error("Failed to load managed games:", err);
  }
}

export async function onGlobalGameSelected() {
  const gameId = document.getElementById("global-game-select").value;
  await loadAndOpenGame(gameId);
}

export async function loadAndOpenGame(gameId) {
  await selectManagedGame(gameId);
  switchTab("game");
}

export function editGameInManager(gameId) {
  const g = state.managedGames.find(item => item.id === gameId);
  if (!g) return;
  applyGameSettingsToUI(g);
  const d = document.getElementById("game-editor-details");
  d.open = true;
  d.scrollIntoView({ behavior: "smooth" });
}

export async function selectManagedGame(gameId) {
  const res = await apiPost("/api/games/select", { game_id: gameId });
  if (res.ok && res.data.game) {
    applyGameSettingsToUI(res.data.game);
    document.getElementById("gm-output").textContent =
      `Loaded settings for '${res.data.game.name}' (${gameId}):\n` +
      JSON.stringify(res.data.game, null, 2);
    await refreshGamesList(false);
  }
}

export function fillNewGameTemplate() {
  document.getElementById("gm-id").value = "custom_unity_game";
  document.getElementById("gm-name").value = "Custom Unity IL2CPP Game";
  document.getElementById("gm-game-dir").value = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\YourGame";
  document.getElementById("gm-notes").value = "Custom managed game profile";
  document.getElementById("game-editor-details").open = true;
}

export async function saveManagedGame() {
  const out = document.getElementById("gm-output");
  out.textContent = "Saving managed game settings...";
  const payload = {
    id: document.getElementById("gm-id").value.trim(),
    name: document.getElementById("gm-name").value.trim(),
    tab_label: document.getElementById("gm-name").value.trim(),
    has_dedicated_tab: false,
    runtime: document.getElementById("gm-runtime").value,
    game_dir: document.getElementById("gm-game-dir").value.trim(),
    bepinex_zip_path: document.getElementById("gm-zip-path").value.trim(),
    expected_sha256: document.getElementById("gm-sha256").value.trim(),
    plugin_build_dir: document.getElementById("gm-plugin-dir").value.trim(),
    venv_python_path: document.getElementById("mcp-python").value.trim(),
    mcp_app_path: document.getElementById("mcp-app").value.trim(),
    game_ip: "localhost",
    default_entity: document.getElementById("mcp-target-entity").value,
    default_variable: document.getElementById("mcp-variable-select").value,
    default_go_id: document.getElementById("mcp-go-id").value.trim(),
    default_component: document.getElementById("gm-def-comp").value.trim(),
    default_member: document.getElementById("gm-def-member").value.trim(),
    default_value: document.getElementById("gm-def-val").value.trim(),
    default_profile: document.getElementById("mcp-profile-name").value.trim(),
    default_selector: parseSelector() || {},
    notes: document.getElementById("gm-notes").value.trim()
  };
  const res = await apiPost("/api/games", payload);
  out.textContent = JSON.stringify(res.data, null, 2);
  if (res.ok && res.data.game) {
    applyGameSettingsToUI(res.data.game);
    await refreshGamesList(false);
    switchTab("game");
  }
}

export async function deleteManagedGame(gameId) {
  const res = await apiFetch(`/api/games/${encodeURIComponent(gameId)}`, { method: "DELETE" });
  const data = await res.json();
  document.getElementById("gm-output").textContent = JSON.stringify(data, null, 2);
  await refreshGamesList(true);
  await loadLogs();
}

export async function pruneNonGames() {
  const btn = _resolveLibBtn("");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const resp = await apiPost("/api/games/prune-non-games", {});
        const data = resp?.data ?? resp;
        await refreshGamesList(false);
        loadLogs();
        return data;
      },
      {
        pendingText: "Pruning non-game entries...",
        successText: (d) => d?.summary || "Pruned non-game entries",
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

export async function prewarmAllGameIcons() {
  const btn = _resolveLibBtn("btn-prewarm-icons");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const resp = await apiPost("/api/games/prewarm-icons", {});
        const data = resp?.data ?? resp;
        renderGamesTable();
        return data;
      },
      {
        pendingText: "Pre-warming game icons...",
        successText: (d) => d?.summary || "Icons pre-warmed",
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

export async function toggleFavoriteGame(gameId, favorite) {
  try {
    const resp = await apiPost(`/api/games/${encodeURIComponent(gameId)}/notes`, { favorite });
    if (resp.ok) {
      await refreshGamesList(false);
    }
  } catch {}
}

export async function saveActiveGameNotes() {
  const tagsRaw = document.getElementById("game-user-tags")?.value || "";
  const notes = document.getElementById("game-user-notes")?.value || "";
  const tags = tagsRaw.split(",").map(s => s.trim()).filter(Boolean);
  try {
    const resp = await apiPost(`/api/games/${encodeURIComponent(state.activeGameId)}/notes`, { tags, user_notes: notes });
    const out = document.getElementById("fp-output");
    if (out) {
      out.style.display = "block";
      out.textContent = resp.ok ? "Saved game tags and notes." : JSON.stringify(resp.data);
    }
    await refreshGamesList(false);
  } catch (err) {
    alert(`Failed to save notes: ${err.message || err}`);
  }
}

export async function launchActiveGame() {
  const out = document.getElementById("fp-output");
  try {
    const resp = await apiPost(`/api/games/${encodeURIComponent(state.activeGameId)}/launch`, {});
    if (out) {
      out.style.display = "block";
      out.textContent = resp.ok ? resp.data.summary : (resp.data?.detail || "Launch failed");
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

export async function applyOptimalUnrealIni() {
  const btn = _resolveLibBtn("");
  const game = state.managedGames.find(g => g.id === state.activeGameId) || state.lastRenderedGame || {};
  const ue = game.modding_info?.unreal_settings;
  if (!ue || !ue.ini_path) {
    alert("No Unreal GameUserSettings.ini detected for this game yet. Click Rescan INI first.");
    return;
  }
  const updates = {};
  for (const row of (ue.settings || [])) {
    if (row.recommended_value !== undefined && row.recommended_value !== null && row.key) {
      updates[row.key] = String(row.recommended_value);
    }
  }
  try {
    await runTrackedAction(
      btn,
      async () => {
        const resp = await apiPost("/api/unreal-settings/apply", { game_id: state.activeGameId, ini_path: ue.ini_path, updates });
        const data = resp?.data ?? resp;
        await importUnrealSettings(ue.ini_path);
        return data;
      },
      {
        pendingText: "Applying optimal Unreal INI settings...",
        successText: (d) => d?.summary || "Applied optimal Unreal INI settings",
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

export async function restoreUnrealIniBackup() {
  const btn = _resolveLibBtn("");
  const game = state.managedGames.find(g => g.id === state.activeGameId) || state.lastRenderedGame || {};
  const iniPath = game.modding_info?.unreal_settings?.ini_path || "";
  if (!iniPath) return;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const resp = await apiPost("/api/unreal-settings/restore", { game_id: state.activeGameId, ini_path: iniPath });
        const data = resp?.data ?? resp;
        await importUnrealSettings(iniPath);
        return data;
      },
      {
        pendingText: "Restoring Unreal INI backup...",
        successText: (d) => d?.summary || "Restored Unreal INI backup",
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

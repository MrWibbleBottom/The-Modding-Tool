import { apiFetch, apiGet, apiPost, pollBackgroundJob, runTrackedAction, state, switchTab } from "./api.js";
import { loadLogs } from "./app_mgmt.js";
import { parseSelector } from "./mcp.js";
import { escHtml, libState, resolveBtn, upsertManagedGameInState } from "./state.js";
import { renderActiveGamePanel, renderTopGameInfoPanel } from "./active_game.js";

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
    libState.libraryEntries = Array.isArray(res.data.entries) ? res.data.entries : [];
    const badge = document.getElementById("library-count-badge");
    if (badge) badge.textContent = String(libState.libraryEntries.length);
    renderGameLibraryCards();
  }
}

export async function scanLibraryInstalled(extraRoots = []) {
  const btn = resolveBtn("btn-lib-scan-installed");
  const statusEl = document.getElementById("lib-status-bar");
  if (statusEl) statusEl.textContent = "Scanning Steam, Epic, GOG, and custom folders + fingerprinting engines...";
  try {
    await runTrackedAction(
      btn,
      async (controls) => {
        const res = await apiPost("/api/library/scan-installed", {
          custom_roots: extraRoots,
          save_custom_roots: true,
          async_job: true,
        });
        const data = await pollBackgroundJob(res, controls);
        libState.libraryEntries = Array.isArray(data?.entries) ? data.entries : [];
        const badge = document.getElementById("library-count-badge");
        if (badge) badge.textContent = String(libState.libraryEntries.length);
        if (statusEl) {
          statusEl.innerHTML = `Discovered <strong>${data?.installed_count ?? 0}</strong> installed game(s) (${data?.total_count ?? libState.libraryEntries.length} total in library).`;
        }
        renderGameLibraryCards();
        return data;
      },
      {
        pendingText: "Scanning Steam, Epic, GOG, and custom libraries...",
        successText: (d) => `Discovered ${d?.installed_count ?? 0} installed game(s) (${d?.total_count ?? 0} total)`,
        mode: "determinate",
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
  const btn = resolveBtn("btn-lib-run-owned");
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
        libState.libraryEntries = Array.isArray(data?.entries) ? data.entries : [];
        const badge = document.getElementById("library-count-badge");
        if (badge) badge.textContent = String(libState.libraryEntries.length);
        if (statusEl) {
          statusEl.innerHTML = `Imported <strong>${data?.imported_count ?? 0}</strong> owned/uninstalled title(s) (${data?.total_count ?? libState.libraryEntries.length} total in library).`;
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
  const data = await apiPost("/api/library/import-file", { content: txt, enrich_metadata: true });
  if (data) {
    libState.libraryEntries = Array.isArray(data.entries) ? data.entries : [];
    const badge = document.getElementById("library-count-badge");
    if (badge) badge.textContent = String(libState.libraryEntries.length);
    if (statusEl) {
      statusEl.innerHTML = `Imported <strong>${data.imported_count}</strong> title(s) from JSON/CSV (${data.total_count} total).`;
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

  const filtered = libState.libraryEntries.filter(e => {
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

  grid.innerHTML = filtered.map(e => {
    const isInstalled = e.install_state === "installed";
    const isRestricted = e.anti_cheat_status === "Restricted";
    const cardCls = isRestricted ? "missing" : (isInstalled ? "ok" : "pending");
    const confLabel = e.detection_confidence === "verified_disk" ? "Verified" : "Estimated";
    const confPillCls = e.detection_confidence === "verified_disk" ? "yes" : "warn";
    const adapterCls = isRestricted ? "no" : (e.recommended_adapter === "bepinex-mcp" ? "yes" : "na");
    const clickable = isInstalled && !isRestricted;
    const idxInLib = libState.libraryEntries.indexOf(e);
    return `<div class="step-card ${cardCls}" ${clickable ? `style="cursor:pointer;" onclick="selectLibraryEntry(${idxInLib})"` : ""}>
      <div class="step-card-top">
        <span class="step-card-title">${escHtml(e.title)}</span>
        <span class="pill ${isInstalled ? "yes" : "na"}">${isInstalled ? "Installed" : "Not Installed"}</span>
      </div>
      <div style="display:flex; gap:6px; flex-wrap:wrap; margin:4px 0;">
        <span class="badge">${escHtml((e.source_platform || "").toUpperCase())}</span>
        <span class="badge">${escHtml(e.engine || "Unknown")}</span>
        <span class="pill ${confPillCls}">${confLabel}</span>
        <span class="pill ${adapterCls}">${escHtml(e.recommended_adapter || "unknown")}</span>
      </div>
      <div class="step-card-detail">${e.root_path ? escHtml(e.root_path) : "Not installed on disk (pre-install metadata)"}</div>
    </div>`;
  }).join("");
}

export function selectLibraryEntry(index) {
  const entry = libState.libraryEntries[index];
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
  const data = await apiPost("/api/games/browse", {
    initial_dir: inp ? inp.value : "",
    title: "Select Game or Platform Library Folder"
  });
  if (data && data.status === "ok" && data.path && inp) {
    inp.value = data.path;
  }
  return data;
}

export async function browseAndScan() {
  const picked = await browseIntoInput("scan-custom-path");
  if (picked && picked.status === "ok" && picked.path) {
    await scanPlatforms(picked.path, "btn-game-scan");
  }
}

export async function scanCustomPathInput() {
  const customPath = document.getElementById("scan-custom-path").value.trim();
  await scanPlatforms(customPath, "btn-scan-path");
}

function _syncScanLogToggleButton() {
  const panel = document.getElementById("scan-log-panel");
  const toggleBtn = document.getElementById("btn-toggle-scan-log");
  if (!toggleBtn) return;
  const isOpen = Boolean(panel && !panel.hidden);
  toggleBtn.textContent = isOpen ? "Hide Game Scan Log" : "Show Game Scan Log";
}

export function setScanOutputLog(text) {
  const panel = document.getElementById("scan-log-panel");
  const out = document.getElementById("scan-output");
  if (out) {
    out.textContent = String(text || "");
  }
  if (panel && !String(text || "").trim()) {
    panel.hidden = true;
  }
  _syncScanLogToggleButton();
}

export function toggleScanOutputLog() {
  const panel = document.getElementById("scan-log-panel");
  const out = document.getElementById("scan-output");
  if (!panel) return;
  panel.hidden = !panel.hidden;
  if (!panel.hidden && out && !out.textContent.trim()) {
    out.textContent = "No scan output recorded yet.";
  }
  _syncScanLogToggleButton();
}

export function closeScanOutputLog() {
  const panel = document.getElementById("scan-log-panel");
  if (panel) panel.hidden = true;
  _syncScanLogToggleButton();
}

export async function copyScanOutputLog() {
  const out = document.getElementById("scan-output");
  const copyBtn = document.getElementById("btn-copy-scan-log");
  const text = out ? out.textContent.trim() : "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    if (copyBtn) {
      copyBtn.textContent = "Copied!";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
    }
  } catch {
    // Ignore clipboard restrictions
  }
}

export function startScanProgressPolling(initialPhase = "Starting scan...", onProgress = null) {
  stopScanProgressPolling();
  libState.scanProgressCallback = typeof onProgress === "function" ? onProgress : null;
  if (typeof libState.scanProgressCallback === "function") {
    libState.scanProgressCallback(0, initialPhase);
  }
  libState.scanProgressTimer = setInterval(async () => {
    try {
      const res = await apiFetch("/api/games/scan-progress");
      if (res.ok) {
        const data = await res.json();
        if (typeof libState.scanProgressCallback === "function" && data) {
          const pct = Math.max(0, Math.min(100, Number(data.percent || 0)));
          const itemText = data.current_item ? ` — ${data.current_item}` : "";
          const phaseMsg = `${data.phase || "Scanning..."}${itemText}`;
          libState.scanProgressCallback(pct, `${phaseMsg} (${pct}%)`);
        }
      }
    } catch {}
  }, 200);
}

export function stopScanProgressPolling(finalPhase = "", onProgress = null) {
  if (libState.scanProgressTimer) {
    clearInterval(libState.scanProgressTimer);
    libState.scanProgressTimer = null;
  }
  const cb = typeof onProgress === "function" ? onProgress : libState.scanProgressCallback;
  if (typeof cb === "function" && finalPhase) {
    cb(100, finalPhase);
  }
  libState.scanProgressCallback = null;
}

async function _executeScanAllGamesInfo(prefixText = "", onProgress = null) {
  setScanOutputLog(
    prefixText
      ? `${prefixText}\n\nScraping Game Info metadata...`
      : "Scanning game folders on disk and populating Game Info metadata for all found games..."
  );
  startScanProgressPolling("Scraping Game Info metadata...", onProgress);
  try {
    const res = await apiPost("/api/games/scan-info", {});
    const data = res?.data ?? res;
    const lines = (data?.results || []).map(
      r => `${r.name} (${r.id}): ${r.status}${r.mcp_category ? ` [${r.mcp_category} | Unity ${r.unity_version || "N/A"} | ${r.modding_readiness}]` : ""}`
    ).join("\n  ");
    const infoSummary = `${data?.summary || "Game info scan complete."}\nPopulated Game Info:\n  ${lines || "None"}`;
    setScanOutputLog(prefixText ? `${prefixText}\n\n${infoSummary}` : infoSummary);
    stopScanProgressPolling("Game scan & info complete", onProgress);
    await refreshGamesList(true);
    return data;
  } catch (err) {
    stopScanProgressPolling("", onProgress);
    setScanOutputLog((prefixText ? `${prefixText}\n\n` : "") + `Game info scan failed: ${err?.message || err}`);
    throw err;
  }
}

export async function scanPlatforms(customPath = "", fallbackBtnId = "btn-game-scan") {
  const scanBtn = resolveBtn(fallbackBtnId);
  const scopeEl = document.getElementById("scan-unity-only");
  const discoveryScope = scopeEl ? scopeEl.value : "all";
  const scopeLabel = scopeEl && scopeEl.selectedOptions[0] ? scopeEl.selectedOptions[0].textContent : discoveryScope;
  setScanOutputLog(
    customPath
      ? `Scanning folder '${customPath}' [Scope: ${scopeLabel}] and auto-populating Game Manager...`
      : `Scanning PC game platforms (Steam, Epic Games, GOG Galaxy, XboxGames, Ubisoft, EA) [Scope: ${scopeLabel}]...`
  );
  try {
    await runTrackedAction(
      scanBtn,
      async ({ setProgress }) => {
        const mapPhase1 = (pct, label) => setProgress(Math.round(pct * 0.5), label);
        const mapPhase2 = (pct, label) => setProgress(50 + Math.round(pct * 0.5), label);
        startScanProgressPolling("Discovering platform game folders...", mapPhase1);
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
        const infoData = await _executeScanAllGamesInfo(scanSummaryText, mapPhase2);
        setProgress(100, data?.summary || "Platform & Game Info scan complete");
        return { scan: data, info: infoData };
      },
      {
        pendingText: customPath ? `Scanning '${customPath}'...` : "Scanning game platforms & populating Game Info...",
        successText: (r) => r?.scan?.summary || "Platform & Game Info scan complete",
        mode: "determinate",
      },
    );
  } catch (err) {
    stopScanProgressPolling();
    setScanOutputLog(`Scan failed: ${err?.message || err}`);
  }
}

export async function scanAllGamesInfo(prefixText = "") {
  const btn = resolveBtn("btn-scan-game-info");
  try {
    await runTrackedAction(
      btn,
      async ({ setProgress }) => {
        const data = await _executeScanAllGamesInfo(prefixText, setProgress);
        setProgress(100, data?.summary || "Game info scan complete");
        return data;
      },
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
  if (libState.sortColumn === col) {
    libState.sortDirection = libState.sortDirection === "asc" ? "desc" : "asc";
  } else {
    libState.sortColumn = col;
    libState.sortDirection = "asc";
  }
  renderGamesTable();
}

export function onSortSelectChanged() {
  const val = document.getElementById("gm-sort-select").value;
  const parts = val.split(":");
  libState.sortColumn = parts[0] || "name";
  libState.sortDirection = parts[1] || "asc";
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
      ind.textContent = libState.sortColumn === col ? (libState.sortDirection === "asc" ? "▲" : "▼") : "";
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
    const va = getSortValue(a, libState.sortColumn);
    const vb = getSortValue(b, libState.sortColumn);
    if (va < vb) return libState.sortDirection === "asc" ? -1 : 1;
    if (va > vb) return libState.sortDirection === "asc" ? 1 : -1;
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

export function _applyGamesPayload(data, applyActive = false, { allowAutoFetch = true } = {}) {
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
    if (applyActive || !state.lastRenderedGame || state.lastRenderedGame.id !== activeGame.id) {
      renderActiveGamePanel(activeGame, { allowAutoFetch });
    } else {
      renderTopGameInfoPanel(activeGame);
    }
  }
}

export async function refreshGamesList(applyActive = false) {
  if (!state.managedGames.length) {
    try {
      const cachedRaw = localStorage.getItem("modtool_cache_games");
      if (cachedRaw) _applyGamesPayload(JSON.parse(cachedRaw), applyActive, { allowAutoFetch: false });
    } catch {}
  }
  try {
    const res = await apiFetch("/api/games");
    const data = await res.json();
    try { localStorage.setItem("modtool_cache_games", JSON.stringify(data)); } catch {}
    _applyGamesPayload(data, applyActive, { allowAutoFetch: true });
  } catch (err) {
    console.error("Failed to load managed games:", err);
  }
}

export async function onGlobalGameSelected() {
  const gameId = document.getElementById("global-game-select")?.value;
  if (gameId) {
    await loadAndOpenGame(gameId);
  }
}

export async function loadAndOpenGame(gameId) {
  const localGame = state.managedGames.find(item => item.id === gameId);
  if (localGame) {
    renderActiveGamePanel(localGame, { allowAutoFetch: false });
  }
  await selectManagedGame(gameId);
  switchTab("game");
}

export function editGameInManager(gameId) {
  const g = state.managedGames.find(item => item.id === gameId);
  if (!g) return;
  renderActiveGamePanel(g);
  const d = document.getElementById("game-editor-details");
  d.open = true;
  d.scrollIntoView({ behavior: "smooth" });
}

export async function selectManagedGame(gameId) {
  const data = await apiPost("/api/games/select", { game_id: gameId });
  if (data && data.game) {
    upsertManagedGameInState(data.game);
    renderActiveGamePanel(data.game);
    const gmOut = document.getElementById("gm-output");
    if (gmOut) {
      gmOut.textContent =
        `Loaded settings for '${data.game.name}' (${gameId}):\n` +
        JSON.stringify(data.game, null, 2);
    }
    await refreshGamesList(false);
  }
  return data;
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
  const data = await apiPost("/api/games", payload);
  if (out) out.textContent = JSON.stringify(data, null, 2);
  if (data && data.game) {
    upsertManagedGameInState(data.game);
    renderActiveGamePanel(data.game);
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
  const btn = resolveBtn("btn-prune-non-games");
  try {
    await runTrackedAction(
      btn,
      async ({ setProgress }) => {
        setProgress(25, "Pruning non-game and duplicate entries...");
        const resp = await apiPost("/api/games/prune-non-games", {});
        const data = resp?.data ?? resp;
        setProgress(80, "Refreshing Game Manager list...");
        await refreshGamesList(false);
        loadLogs();
        setScanOutputLog(
          `${data?.summary || "Prune complete."}\nRemoved:\n  ${(data?.removed || []).join("\n  ") || "None"}`
        );
        setProgress(100, data?.summary || "Pruned non-game entries");
        return data;
      },
      {
        pendingText: "Pruning non-game entries...",
        successText: (d) => d?.summary || "Pruned non-game entries",
        mode: "determinate",
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

export async function prewarmAllGameIcons() {
  const btn = resolveBtn("btn-prewarm-icons");
  try {
    await runTrackedAction(
      btn,
      async (controls) => {
        controls.setProgress(10, "Queueing icon pre-warm...");
        const resp = await apiPost("/api/games/prewarm-icons?async_job=true", {});
        const data = await pollBackgroundJob(resp, controls);
        controls.setProgress(95, "Updating game table icons...");
        renderGamesTable();
        setScanOutputLog(data?.summary || "Game icons pre-warmed.");
        controls.setProgress(100, data?.summary || "Icons pre-warmed");
        return data;
      },
      {
        pendingText: "Pre-warming game icons...",
        successText: (d) => d?.summary || "Icons pre-warmed",
        mode: "determinate",
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

export async function toggleFavoriteGame(gameId, favorite) {
  try {
    await apiPost(`/api/games/${encodeURIComponent(gameId)}/notes`, { favorite });
    await refreshGamesList(false);
  } catch {}
}

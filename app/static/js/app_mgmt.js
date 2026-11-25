import { apiFetch, apiGet, apiPost, runTrackedAction, state, switchTab } from "./api.js";
import { STEAM_ASSETS_JS_CACHE, escHtml, resolveBtn } from "./state.js";
import { refreshGamesList } from "./game_table.js";
import { loadPcSpecs } from "./analyzer.js";

export {
  checkGitHubUpdates,
  closeGitPushConfirmModal,
  confirmPushAppToGitHub,
  copyGitPushError,
  loadAppBackups,
  loadGitHubRepoConfig,
  loadGitStatus,
  pullGitHubBranches,
  pullGitHubSync,
  pushAppToGitHub,
  renderAppBackupsTable,
  restoreAppBackupVersion,
  rollbackToSnapshot,
  saveAppSnapshot,
  saveGitHubRepoConfig,
  toggleAppBackupsExpand,
  toggleBackupRowExpand,
} from "./git_backup.js";

export async function switchLogCategory(category) {
  state.activeLogCategory = category || "game";
  for (const k of ["game", "app", "all"]) {
    const b = document.getElementById(`btn-log-cat-${k}`);
    if (b) {
      b.className = (k === state.activeLogCategory) ? "btn-sm" : "secondary btn-sm";
    }
  }
  window._AUDIT_LOG_EXPANDED = {};
  await loadLogs();
}

export async function loadLogs() {
  try {
    const res = await apiFetch(`/api/logs?limit=50&category=${encodeURIComponent(state.activeLogCategory)}`);
    const data = await res.json();
    document.getElementById("audit-path").textContent = data.audit_log_path || "";
    const gameCntEl = document.getElementById("log-game-count-badge");
    const appCntEl = document.getElementById("log-app-count-badge");
    if (gameCntEl && data.game_count !== undefined) gameCntEl.textContent = String(data.game_count);
    if (appCntEl && data.app_count !== undefined) appCntEl.textContent = String(data.app_count);
    const countBadge = document.getElementById("audit-count-badge");
    const tabCountBadge = document.getElementById("audit-tab-count-badge");
    if (Array.isArray(data.entries)) {
      if (countBadge) countBadge.textContent = `${data.entries.length} entries (${state.activeLogCategory})`;
      if (tabCountBadge) tabCountBadge.textContent = `${(data.game_count ?? 0) + (data.app_count ?? 0) || data.entries.length}`;
    }
    const tbody = document.getElementById("logs-tbody");
    if (!data.entries || data.entries.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5">No audit entries yet.</td></tr>';
      return;
    }
    window._AUDIT_LOG_EXPANDED = window._AUDIT_LOG_EXPANDED || {};
    window._AUDIT_LOG_ENTRIES = data.entries;
    renderAuditLogsTable();
  } catch (err) {
    console.error(err);
  }
}

export function toggleAuditLogRow(idx) {
  window._AUDIT_LOG_EXPANDED = window._AUDIT_LOG_EXPANDED || {};
  window._AUDIT_LOG_EXPANDED[idx] = !window._AUDIT_LOG_EXPANDED[idx];
  renderAuditLogsTable();
}

export function renderAuditLogsTable() {
  const tbody = document.getElementById("logs-tbody");
  const entries = window._AUDIT_LOG_ENTRIES || [];
  if (!tbody) return;
  const MAX_PREVIEW = 120;
  tbody.innerHTML = entries.map((e, idx) => {
    const stClass = `status-${(e.status || "ok").toLowerCase()}`;
    const hashShort = e.sha256 ? `${e.sha256.slice(0, 12)}...` : "-";
    const baseSummary = String(e.summary || "");
    const detailsText = (e.details && Object.keys(e.details).length > 0)
      ? JSON.stringify(e.details, null, 2)
      : "";
    const fullText = detailsText ? `${baseSummary}\n${detailsText}` : baseSummary;
    const isLong = fullText.length > MAX_PREVIEW || Boolean(detailsText);
    const isExpanded = Boolean((window._AUDIT_LOG_EXPANDED || {})[idx]);
    const shownText = isExpanded
      ? fullText
      : (baseSummary.length > MAX_PREVIEW ? `${baseSummary.slice(0, MAX_PREVIEW)}...` : baseSummary);

    const expandBtn = isLong
      ? `<button class="secondary btn-sm" style="margin-left:6px; padding:2px 8px; min-height:22px; font-size:0.7rem;" onclick="toggleAuditLogRow(${idx})">${isExpanded ? "Collapse" : "Expand"}</button>`
      : "";

    return `<tr>
      <td>${escHtml(e.timestamp || "")}</td>
      <td>${escHtml(e.action || "")}</td>
      <td class="${stClass}">${escHtml(e.status || "")}</td>
      <td title="${escHtml(e.sha256 || "")}">${escHtml(hashShort)}</td>
      <td>
        <div style="white-space:${isExpanded ? "pre-wrap" : "normal"}; font-size:0.79rem;">${escHtml(shownText)}${expandBtn}</div>
      </td>
    </tr>`;
  }).join("");
}

const DEFAULT_APP_UI_CONFIG = {
  app_name: "The Modding Tool",
  app_subtitle: "Platform- & Engine-Agnostic Modding Console",
  accent_color: "#3b82f6",
  compact_mode: false,
  startup_tab: "manager",
  banner_image_url: "",
  banner_height_px: 110,
  favicon_url: "",
  tab_labels: {
    manager: "Game Manager",
    game: "Active Game",
    modbuilder: "Build a Mod",
    specs: "PC Specs",
    logs: "Audit Log",
  },
  visibility: {
    header_subtitle: true,
    header_quick_load: true,
    header_active_badge: true,
    header_server_badge: true,
    tab_manager: true,
    tab_game: true,
    tab_modbuilder: true,
    tab_specs: true,
    tab_logs: true,
    sec_pipeline_status: true,
    sec_provision_restore: true,
    sec_live_mcp: true,
  },
  in_progress: {
    manager: false,
    game: false,
    modbuilder: false,
    specs: false,
    logs: false,
  },
};
let APP_UI_CONFIG = JSON.parse(JSON.stringify(DEFAULT_APP_UI_CONFIG));
state.appUiConfig = APP_UI_CONFIG;

export function _setElVisible(id, visible, displayStyle = "") {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = visible ? displayStyle : "none";
}

export function applyAppUiConfig(cfg) {
  if (!cfg) return;
  state.appUiConfig = cfg;
  const appName = (cfg.app_name || DEFAULT_APP_UI_CONFIG.app_name).trim() || DEFAULT_APP_UI_CONFIG.app_name;
  const appSubtitle = (cfg.app_subtitle ?? DEFAULT_APP_UI_CONFIG.app_subtitle);
  document.title = appName;
  const titleEl = document.getElementById("app-main-title");
  if (titleEl) titleEl.textContent = appName;
  const subEl = document.getElementById("app-subtitle");
  if (subEl) subEl.textContent = appSubtitle;

  if (cfg.accent_color) {
    document.documentElement.style.setProperty("--accent", cfg.accent_color);
  }
  document.body.classList.toggle("compact", Boolean(cfg.compact_mode));

  const bannerUrl = (cfg.banner_image_url || "").trim();
  const bannerH = Number(cfg.banner_height_px || 110);
  document.documentElement.style.setProperty("--banner-h", `${bannerH}px`);
  for (const wrap of document.querySelectorAll(".app-tab-banner")) {
    const img = wrap.querySelector(".app-tab-banner-img");
    if (!img) continue;
    if (bannerUrl) {
      img.src = bannerUrl;
      wrap.style.display = "block";
    } else {
      wrap.style.display = "none";
      img.removeAttribute("src");
    }
  }

  const bannerThumb = document.getElementById("ae-banner-thumb");
  const bannerThumbLbl = document.getElementById("ae-banner-thumb-label");
  if (bannerThumb && bannerThumbLbl) {
    if (bannerUrl) {
      bannerThumb.src = bannerUrl;
      bannerThumb.style.display = "block";
      bannerThumbLbl.textContent = `Active Banner (${bannerH}px max height)`;
    } else {
      bannerThumb.style.display = "none";
      bannerThumb.removeAttribute("src");
      bannerThumbLbl.textContent = "No banner currently in use";
    }
  }

  const favUrl = (cfg.favicon_url || "").trim();
  const oldFav = document.getElementById("app-favicon");
  const nextHref = favUrl || "data:,";
  if (!oldFav || oldFav.getAttribute("href") !== nextHref) {
    if (oldFav) oldFav.remove();
    const newFav = document.createElement("link");
    newFav.id = "app-favicon";
    newFav.rel = "icon";
    newFav.href = nextHref;
    document.head.appendChild(newFav);
  }
  const favThumb = document.getElementById("ae-favicon-thumb");
  const favThumbLbl = document.getElementById("ae-favicon-thumb-label");
  if (favThumb && favThumbLbl) {
    if (favUrl) {
      favThumb.src = favUrl;
      favThumb.style.display = "block";
      favThumbLbl.textContent = "Active Favicon";
    } else {
      favThumb.style.display = "none";
      favThumb.removeAttribute("src");
      favThumbLbl.textContent = "No custom favicon currently in use";
    }
  }

  const labels = cfg.tab_labels || {};
  for (const tabKey of ["manager", "game", "modbuilder", "specs", "logs"]) {
    const lblEl = document.getElementById(`tab-label-${tabKey}`);
    if (lblEl && labels[tabKey]) {
      lblEl.textContent = labels[tabKey];
    }
  }

  const vis = cfg.visibility || {};
  _setElVisible("app-subtitle", vis.header_subtitle !== false);
  _setElVisible("header-quick-load", vis.header_quick_load !== false, "flex");
  _setElVisible("active-game-badge", vis.header_active_badge !== false);
  _setElVisible("server-badge", vis.header_server_badge !== false);

  _setElVisible("tab-btn-manager", vis.tab_manager !== false, "inline-flex");
  _setElVisible("tab-btn-game", vis.tab_game !== false, "inline-flex");
  _setElVisible("tab-btn-modbuilder", vis.tab_modbuilder !== false, "inline-flex");
  _setElVisible("tab-btn-specs", vis.tab_specs !== false, "inline-flex");
  _setElVisible("tab-btn-logs", vis.tab_logs !== false, "inline-flex");

  _setElVisible("info-quick-indicators", vis.sec_pipeline_status !== false, "flex");
  _setElVisible("card-provision-restore", vis.sec_provision_restore !== false);
  _setElVisible("card-live-mcp", vis.sec_live_mcp !== false);

  const wip = cfg.in_progress || {};
  for (const tabKey of ["manager", "game", "modbuilder", "specs", "logs"]) {
    const isWip = Boolean(wip[tabKey]);
    const btn = document.getElementById(`tab-btn-${tabKey}`);
    const banner = document.getElementById(`wip-banner-${tabKey}`);
    if (btn) btn.classList.toggle("wip-active", isWip);
    if (banner) banner.style.display = isWip ? "flex" : "none";
  }
}

export function populateAppEditorForm(cfg) {
  const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  setVal("ae-app-name", cfg.app_name || "");
  setVal("ae-app-subtitle", cfg.app_subtitle || "");
  setVal("ae-accent-color", cfg.accent_color || "#3b82f6");
  setVal("ae-startup-tab", cfg.startup_tab || "manager");
  setVal("ae-banner-url", cfg.banner_image_url || "");
  setVal("ae-banner-height", String(cfg.banner_height_px || 110));
  setVal("ae-favicon-url", cfg.favicon_url || "");

  const compactEl = document.getElementById("ae-compact-mode");
  if (compactEl) compactEl.checked = Boolean(cfg.compact_mode);

  const labels = cfg.tab_labels || {};
  for (const k of ["manager", "game", "modbuilder", "specs", "logs"]) {
    setVal(`ae-tablabel-${k}`, labels[k] || DEFAULT_APP_UI_CONFIG.tab_labels[k]);
  }

  const vis = cfg.visibility || {};
  for (const key of Object.keys(DEFAULT_APP_UI_CONFIG.visibility)) {
    const cb = document.getElementById(`ae-vis-${key}`);
    if (cb) cb.checked = vis[key] !== false;
  }
  const wip = cfg.in_progress || {};
  for (const tabKey of ["manager", "game", "modbuilder", "specs", "logs"]) {
    const cb = document.getElementById(`ae-wip-${tabKey}`);
    if (cb) cb.checked = Boolean(wip[tabKey]);
  }
}

export function readAppEditorForm() {
  const getVal = (id, fallback = "") => document.getElementById(id)?.value ?? fallback;
  const vis = {};
  for (const key of Object.keys(DEFAULT_APP_UI_CONFIG.visibility)) {
    const cb = document.getElementById(`ae-vis-${key}`);
    vis[key] = cb ? cb.checked : true;
  }
  return {
    app_name: getVal("ae-app-name", DEFAULT_APP_UI_CONFIG.app_name),
    app_subtitle: getVal("ae-app-subtitle", DEFAULT_APP_UI_CONFIG.app_subtitle),
    accent_color: getVal("ae-accent-color", "#3b82f6"),
    compact_mode: Boolean(document.getElementById("ae-compact-mode")?.checked),
    startup_tab: getVal("ae-startup-tab", "manager"),
    banner_image_url: getVal("ae-banner-url", ""),
    banner_height_px: Number(getVal("ae-banner-height", "110")) || 110,
    favicon_url: getVal("ae-favicon-url", ""),
    tab_labels: {
      manager: getVal("ae-tablabel-manager", "Game Manager"),
      game: getVal("ae-tablabel-game", "Active Game"),
      modbuilder: getVal("ae-tablabel-modbuilder", "Build a Mod"),
      specs: getVal("ae-tablabel-specs", "PC Specs"),
      logs: getVal("ae-tablabel-logs", "Audit Log"),
    },
    visibility: vis,
    in_progress: {
      manager: Boolean(document.getElementById("ae-wip-manager")?.checked),
      game: Boolean(document.getElementById("ae-wip-game")?.checked),
      modbuilder: Boolean(document.getElementById("ae-wip-modbuilder")?.checked),
      specs: Boolean(document.getElementById("ae-wip-specs")?.checked),
      logs: Boolean(document.getElementById("ae-wip-logs")?.checked),
    },
  };
}

export async function onWipToggle() {
  onAppEditorInput();
  await saveAppUiConfig();
}

export function onAppEditorInput() {
  APP_UI_CONFIG = readAppEditorForm();
  applyAppUiConfig(APP_UI_CONFIG);
  try { localStorage.setItem("modtool_app_ui_config", JSON.stringify(APP_UI_CONFIG)); } catch {}
}

export function setAccentPreset(hex) {
  const el = document.getElementById("ae-accent-color");
  if (el) el.value = hex;
  onAppEditorInput();
}

export function _resizeImageForUpload(file, kind) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const rawUrl = String(reader.result || "");
      if (file.name.toLowerCase().endsWith(".ico") || file.name.toLowerCase().endsWith(".svg")) {
        resolve(rawUrl);
        return;
      }
      const img = new Image();
      img.onload = () => {
        const maxW = kind === "favicon" ? 128 : 1600;
        const maxH = kind === "favicon" ? 128 : 360;
        let w = img.width;
        let h = img.height;
        if (w <= maxW && h <= maxH) {
          resolve(rawUrl);
          return;
        }
        const scale = Math.min(maxW / w, maxH / h);
        w = Math.max(1, Math.round(w * scale));
        h = Math.max(1, Math.round(h * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL("image/png", 0.92));
      };
      img.onerror = () => resolve(rawUrl);
      img.src = rawUrl;
    };
    reader.readAsDataURL(file);
  });
}

export async function uploadAppAsset(kind, event) {
  const file = event.target?.files?.[0];
  if (!file) return;
  const uploadBtn = event.target?.parentElement?.querySelector("button") || resolveBtn("");
  const logEl = document.getElementById("ae-status-log");
  try {
    await runTrackedAction(
      uploadBtn,
      async () => {
        const resizedDataUrl = await _resizeImageForUpload(file, kind);
        const resp = await apiPost("/api/app-ui-config/upload-asset", {
          kind,
          filename: file.name,
          data_url: resizedDataUrl,
        });
        const payload = resp?.data ?? resp ?? {};
        const inputId = kind === "banner" ? "ae-banner-url" : "ae-favicon-url";
        const inputEl = document.getElementById(inputId);
        if (inputEl && payload.url) {
          inputEl.value = payload.url;
        }
        onAppEditorInput();
        await _persistAppUiConfig();
        if (logEl) logEl.textContent = `Uploaded, resized, and saved ${kind} (${file.name}).`;
        return payload;
      },
      {
        pendingText: `Uploading ${kind} (${file.name})...`,
        successText: `Uploaded and saved ${kind} (${file.name})`,
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Upload failed: ${err?.message || err}`;
  } finally {
    event.target.value = "";
  }
}

export async function removeAppAsset(kind) {
  const btn = resolveBtn("");
  const logEl = document.getElementById("ae-status-log");
  const inputId = kind === "banner" ? "ae-banner-url" : "ae-favicon-url";
  const el = document.getElementById(inputId);
  if (el) el.value = "";
  onAppEditorInput();
  try {
    await runTrackedAction(
      btn,
      async () => {
        const resp = await apiPost("/api/app-ui-config/remove-asset", { kind });
        const data = resp?.data ?? resp ?? {};
        if (data.config) {
          APP_UI_CONFIG = data.config;
          applyAppUiConfig(APP_UI_CONFIG);
          populateAppEditorForm(APP_UI_CONFIG);
          try { localStorage.setItem("modtool_app_ui_config", JSON.stringify(APP_UI_CONFIG)); } catch {}
        }
        if (logEl) logEl.textContent = `Removed ${kind} and saved settings.`;
        loadLogs();
        return data;
      },
      {
        pendingText: `Removing custom ${kind}...`,
        successText: `Removed custom ${kind}`,
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Remove failed: ${err?.message || err}`;
  }
}

export function setAllElementsVisible(showAll) {
  for (const key of Object.keys(DEFAULT_APP_UI_CONFIG.visibility)) {
    const cb = document.getElementById(`ae-vis-${key}`);
    if (!cb) continue;
    if (showAll) {
      cb.checked = true;
    } else {
      cb.checked = ["tab_manager", "tab_game", "tab_modbuilder"].includes(key);
    }
  }
  onAppEditorInput();
}

export async function loadAppUiConfig() {
  try {
    const cached = localStorage.getItem("modtool_app_ui_config");
    if (cached) {
      APP_UI_CONFIG = JSON.parse(cached);
      applyAppUiConfig(APP_UI_CONFIG);
    }
  } catch {}

  try {
    const res = await apiFetch("/api/app-ui-config");
    const data = await res.json();
    if (data && data.config) {
      APP_UI_CONFIG = data.config;
      applyAppUiConfig(APP_UI_CONFIG);
      populateAppEditorForm(APP_UI_CONFIG);
      try { localStorage.setItem("modtool_app_ui_config", JSON.stringify(APP_UI_CONFIG)); } catch {}
      if (APP_UI_CONFIG.startup_tab && APP_UI_CONFIG.startup_tab !== "manager") {
        switchTab(APP_UI_CONFIG.startup_tab);
      }
    }
  } catch {
    populateAppEditorForm(APP_UI_CONFIG);
  }
}

async function _persistAppUiConfig() {
  APP_UI_CONFIG = readAppEditorForm();
  applyAppUiConfig(APP_UI_CONFIG);
  try { localStorage.setItem("modtool_app_ui_config", JSON.stringify(APP_UI_CONFIG)); } catch {}
  const logEl = document.getElementById("ae-status-log");
  const data = await apiPost("/api/app-ui-config", { config: APP_UI_CONFIG });
  if (logEl) logEl.textContent = `Saved App Editor settings to ${data.config_path} (SHA-256: ${data.sha256})`;
  loadLogs();
  return data;
}

export async function saveAppUiConfig() {
  const btn = resolveBtn("");
  const logEl = document.getElementById("ae-status-log");
  try {
    await runTrackedAction(btn, _persistAppUiConfig, {
      pendingText: "Saving App UI configuration...",
      successText: "Saved App UI configuration",
    });
  } catch (err) {
    if (logEl) logEl.textContent = `Save failed: ${err?.message || err}`;
  }
}

export async function resetAppUiConfig() {
  const btn = resolveBtn("");
  const logEl = document.getElementById("ae-status-log");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const data = await apiPost("/api/app-ui-config/reset", {});
        APP_UI_CONFIG = data.config || JSON.parse(JSON.stringify(DEFAULT_APP_UI_CONFIG));
        applyAppUiConfig(APP_UI_CONFIG);
        populateAppEditorForm(APP_UI_CONFIG);
        try { localStorage.removeItem("modtool_app_ui_config"); } catch {}
        if (logEl) logEl.textContent = "Reset App Editor settings to defaults.";
        loadLogs();
        return data;
      },
      {
        pendingText: "Resetting App UI settings to defaults...",
        successText: "Reset App UI settings to defaults",
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Reset failed: ${err?.message || err}`;
  }
}

export function exportAppUiConfig() {
  const cfg = readAppEditorForm();
  const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "app_ui_config.json";
  a.click();
  URL.revokeObjectURL(url);
}

export function triggerImportAppUiConfig() {
  document.getElementById("ae-import-file")?.click();
}

export function importAppUiConfigFile(event) {
  const file = event.target?.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(String(reader.result || "{}"));
      APP_UI_CONFIG = {
        ...DEFAULT_APP_UI_CONFIG,
        ...parsed,
        tab_labels: { ...DEFAULT_APP_UI_CONFIG.tab_labels, ...(parsed.tab_labels || {}) },
        visibility: { ...DEFAULT_APP_UI_CONFIG.visibility, ...(parsed.visibility || {}) },
      };
      applyAppUiConfig(APP_UI_CONFIG);
      populateAppEditorForm(APP_UI_CONFIG);
      const logEl = document.getElementById("ae-status-log");
      if (logEl) logEl.textContent = `Imported config from ${file.name}. Click 'Save Changes' to persist.`;
    } catch (err) {
      alert(`Invalid JSON config: ${err.message || err}`);
    }
  };
  reader.readAsText(file);
  event.target.value = "";
}

// =====================================================================
// PORTABLE EXPORT, TOOL UPGRADES, DIAGNOSTICS, LIFECYCLE & BANNER
// =====================================================================
let LAST_APP_DIAGNOSTICS = null;

export async function exportPortableAppZip() {
  const btn = resolveBtn("btn-export-app");
  const logEl = document.getElementById("am-export-log");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const data = await apiPost("/api/app/export", {});
        if (logEl) {
          logEl.textContent = `${data.summary}\nSHA-256: ${data.zip_sha256}\nPath: ${data.zip_path}\nStarting browser download...`;
        }
        const a = document.createElement("a");
        a.href = data.download_url || "/api/app/export/download";
        a.download = "TheModdingTool.zip";
        document.body.appendChild(a);
        a.click();
        a.remove();
        loadLogs();
        return data;
      },
      {
        pendingText: "Packaging portable TheModdingTool.zip...",
        successText: (d) => d?.summary || "Exported TheModdingTool.zip",
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Export failed: ${err?.message || err}`;
  }
}

export async function loadModdingToolsStatus() {
  const tbody = document.getElementById("am-tools-tbody");
  try {
    const res = await apiFetch("/api/app/tools/status");
    const data = await res.json();
    const tools = data.tools || [];
    if (!tbody) return;
    tbody.innerHTML = tools.map(t => {
      const shaShort = t.sha256 ? `${t.sha256.slice(0, 16)}...` : "None";
      const secPill = t.checksum_match
        ? `<span class="pill yes">SHA-256 Verified</span>`
        : (t.present ? `<span class="pill warn">Unverified / Custom</span>` : `<span class="pill no">Not Installed</span>`);
      const btnLabel = t.id === "bepinex_mcp_plugin" ? "Rebuild Release" : "Upgrade / Verify";
      return `
        <tr>
          <td><strong>${escHtml(t.name)}</strong></td>
          <td><code>${escHtml(t.version)}</code></td>
          <td title="${escHtml(t.sha256 || "")}"><code>${escHtml(shaShort)}</code></td>
          <td>${secPill}</td>
          <td><button class="btn-sm" onclick="upgradeModdingTool('${escHtml(t.id)}')">${btnLabel}</button></td>
        </tr>
      `;
    }).join("");
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="5">Failed to load tools: ${escHtml(err.message || err)}</td></tr>`;
  }
}

export async function upgradeModdingTool(toolId) {
  const btn = resolveBtn("");
  const logEl = document.getElementById("am-tools-log");
  if (logEl) logEl.textContent = `Upgrading ${toolId} with security verification...`;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const data = await apiPost("/api/app/tools/upgrade", {
          tool_id: toolId,
          require_https: Boolean(document.getElementById("am-sec-https")?.checked),
          require_checksum: Boolean(document.getElementById("am-sec-checksum")?.checked),
          keep_backup: Boolean(document.getElementById("am-sec-backup")?.checked),
          expected_sha256: document.getElementById("am-tool-expected-sha")?.value || "",
          custom_url: document.getElementById("am-tool-custom-url")?.value || "",
        });
        if (logEl) {
          logEl.textContent = `${data.summary}\nSHA-256: ${data.sha256 || "—"}`;
        }
        await loadModdingToolsStatus();
        loadLogs();
        return data;
      },
      {
        pendingText: `Upgrading / verifying ${toolId}...`,
        successText: (d) => d?.summary || `Upgraded ${toolId}`,
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Upgrade blocked/failed: ${err?.message || err}`;
  }
}

export async function loadAppVersion() {
  try {
    const res = await apiFetch("/api/app/version");
    const data = await res.json();
    const ver = data.version || "0.1.0";
    const ch = data.channel || "pre-launch";
    const badge = document.getElementById("app-version-badge");
    const disp = document.getElementById("am-version-display");
    const launchBtn = document.getElementById("btn-launch-1-0");
    if (badge) {
      badge.textContent = state.appInstalledShortSha ? `v${ver} (${state.appInstalledShortSha})` : `v${ver}`;
    }
    if (disp) disp.textContent = `v${ver} (${ch})`;
    if (launchBtn) launchBtn.style.display = data.launched ? "none" : "inline-flex";
  } catch {}
}

export async function bumpAppVersion(bumpType) {
  const note = prompt(`Optional note for '${bumpType}' version bump:`, `Manual ${bumpType} release`);
  if (note === null) return;
  try {
    const resp = await apiPost("/api/app/version/bump", { bump_type: bumpType, note });
    if (resp.ok) {
      await loadAppVersion();
      loadLogs();
    } else {
      alert(resp.data?.detail || "Version bump failed");
    }
  } catch (err) {
    alert(`Version bump failed: ${err.message || err}`);
  }
}

export async function loadAppDiagnostics() {
  try {
    const res = await apiFetch("/api/app/diagnostics");
    const data = await res.json();
    LAST_APP_DIAGNOSTICS = data;
    const rt = document.getElementById("am-diag-runtimes");
    const pid = document.getElementById("am-diag-pid");
    const paths = document.getElementById("am-diag-paths");
    if (rt) rt.textContent = `Python ${data.python_version} | .NET SDK ${data.dotnet_version}`;
    if (pid) pid.textContent = `PID ${data.pid} | ${data.built_mods_count} Built Mod Folder(s)`;
    if (paths) paths.textContent = `AppData: ${data.app_data_dir} | Workspace: ${data.workspace_dir}`;
    if (data.app_version) {
      const badge = document.getElementById("app-version-badge");
      const disp = document.getElementById("am-version-display");
      if (badge) badge.textContent = `v${data.app_version}`;
      if (disp) disp.textContent = `v${data.app_version} (${data.app_channel || "pre-launch"})`;
    }
  } catch (err) {
    console.error("Failed to load diagnostics:", err);
  }
}

export async function openAppMgmtFolder(kind) {
  const path = kind === "backups"
    ? (state.lastBackupsDir || "")
    : (LAST_APP_DIAGNOSTICS?.app_data_dir || "");
  if (!path) return;
  try {
    await apiPost("/api/tools/open-folder", { path });
  } catch (err) {
    alert(`Failed to open folder: ${err.message || err}`);
  }
}

export async function clearBuiltModsCache() {
  if (!confirm("Clear all cached built mod folders in %LOCALAPPDATA%\\TheModdingTool\\built_mods?")) return;
  try {
    const resp = await apiPost("/api/app/maintenance/clear-built-mods", {});
    alert((resp.data && resp.data.summary) || "Built mods cache cleared.");
    await loadAppDiagnostics();
    loadLogs();
  } catch (err) {
    alert(`Failed to clear cache: ${err.message || err}`);
  }
}

export async function flushAllAppCaches() {
  if (!confirm("Flush ALL cached game data, PC specs, Steam assets, icons, built mods, and browser cache?")) return;
  try {
    const resp = await apiPost("/api/app/maintenance/flush-all", {});
    try {
      localStorage.removeItem("modtool_cache_games");
      localStorage.removeItem("modtool_cache_pc_specs");
      localStorage.removeItem("modtool_app_ui_config");
    } catch {}
    STEAM_ASSETS_JS_CACHE.clear();
    state.managedGames = [];
    state.pcSpecsData = null;
    alert((resp.data && resp.data.summary) || "All caches flushed.");
    await Promise.all([loadAppDiagnostics(), refreshGamesList(true), loadPcSpecs(false), loadLogs()]);
  } catch (err) {
    alert(`Failed to flush all caches: ${err.message || err}`);
  }
}

export async function restartAppServer() {
  if (!confirm("Restart The Modding Tool server now?")) return;
  const overlay = document.getElementById("app-restarting-overlay");
  const statusEl = document.getElementById("app-restarting-status");
  if (overlay) overlay.style.display = "flex";
  try {
    await apiFetch("/api/app/restart", { method: "POST" });
  } catch {}

  let attempts = 0;
  const pollTimer = setInterval(async () => {
    attempts += 1;
    if (statusEl) {
      statusEl.textContent = `Waiting for server to come back online... (attempt ${attempts})`;
    }
    if (attempts < 2) return;
    try {
      const r = await apiFetch("/api/app/diagnostics", { cache: "no-store" });
      if (r.ok) {
        clearInterval(pollTimer);
        if (statusEl) statusEl.textContent = "Server is back online! Reloading page...";
        window.location.reload();
      }
    } catch {}
  }, 600);
}

export async function stopAppServer() {
  const btn = resolveBtn("btn-am-stop-server");
  if (!confirm("Stop The Modding Tool server and open the Offline Control Page?")) return;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/system/shutdown", {});
        window.location.href = "/offline";
        return res;
      },
      {
        pendingText: "Stopping local server...",
        successText: "Server stopped — opening Offline Control Page",
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

export async function registerProtocolLauncher() {
  const btn = resolveBtn("btn-register-protocol");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/system/register-protocol", {});
        loadLogs();
        return res;
      },
      {
        pendingText: "Registering moddingtool:// Windows protocol...",
        successText: (d) => `Registered ${d?.registry_path || "moddingtool://"} launcher`,
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

export async function loadWatcherStatus() {
  const btn = document.getElementById("btn-toggle-watcher");
  if (!btn) return;
  try {
    const res = await apiFetch("/api/app/watcher/status");
    const data = await res.json();
    btn.textContent = data.running ? `Auto-Watcher: Running (PID ${data.pid})` : "Auto-Watcher: Stopped (Start)";
  } catch {}
}

export async function toggleBackupWatcher() {
  try {
    const resp = await apiPost("/api/app/watcher/toggle", {});
    await loadWatcherStatus();
    if (resp.data?.summary) loadLogs();
  } catch (err) {
    alert(`Watcher toggle failed: ${err.message || err}`);
  }
}

let APP_BANNER_STATE = {
  banner_enabled: true,
  banner_file: "",
  banner_fit: "contain",
  has_banner: false
};

export function triggerBannerUpload() {
  const inp = document.getElementById("app-banner-file-input");
  if (inp) {
    inp.value = "";
    inp.click();
  }
}

export function applyBannerStateToDOM(cacheBust = false) {
  const slot = document.getElementById("app-banner-slot");
  const img = document.getElementById("app-banner-img");
  const fit = APP_BANNER_STATE.banner_fit === "cover" ? "cover" : "contain";

  document.documentElement.style.setProperty("--banner-fit", fit);

  if (!slot || !img) return;
  if (APP_BANNER_STATE.has_banner && APP_BANNER_STATE.banner_enabled) {
    if (cacheBust || !img.getAttribute("src")) {
      img.src = `/api/ui/banner?t=${Date.now()}`;
    }
    slot.style.display = "flex";
  } else {
    slot.style.display = "none";
  }
}

export async function loadPersistentBanner() {
  try {
    const res = await apiGet("/api/ui/banner/settings");
    if (res.ok && res.data) {
      APP_BANNER_STATE = {
        banner_enabled: res.data.banner_enabled !== false,
        banner_file: res.data.banner_file || "",
        banner_fit: res.data.banner_fit === "cover" ? "cover" : "contain",
        has_banner: Boolean(res.data.has_banner)
      };
      if (APP_BANNER_STATE.has_banner) {
        const imgRes = await apiFetch("/api/ui/banner", { cache: "no-store" });
        if (imgRes.status === 204 || !imgRes.ok) {
          APP_BANNER_STATE.has_banner = false;
        }
      }
      applyBannerStateToDOM(true);
    }
  } catch (e) {
    console.warn("Failed to load banner settings:", e);
  }
}

export async function uploadCustomBannerFile(inputEl) {
  const file = inputEl?.files?.[0];
  if (!file) return;
  try {
    await runTrackedAction(
      null,
      async () => {
        const fd = new FormData();
        fd.append("file", file);
        const resp = await apiFetch("/api/ui/banner", { method: "POST", body: fd });
        const text = await resp.text();
        let data = {};
        try { data = JSON.parse(text); } catch { data = { raw: text }; }
        if (!resp.ok) {
          throw new Error(data.detail || text || `HTTP ${resp.status}`);
        }
        APP_BANNER_STATE.banner_enabled = data.banner_enabled !== false;
        APP_BANNER_STATE.banner_file = data.banner_file || "";
        APP_BANNER_STATE.banner_fit = data.banner_fit === "cover" ? "cover" : "contain";
        APP_BANNER_STATE.has_banner = true;
        applyBannerStateToDOM(true);
        return data;
      },
      {
        pendingText: `Uploading banner (${file.name})...`,
        successText: `Uploaded banner (${file.name})`,
      },
    );
  } catch {
    // Inline error handled by runTrackedAction
  }
}

export async function toggleBannerFit() {
  const nextFit = APP_BANNER_STATE.banner_fit === "cover" ? "contain" : "cover";
  const res = await apiPost("/api/ui/banner/settings", { banner_fit: nextFit });
  if (res.ok && res.data) {
    APP_BANNER_STATE.banner_fit = res.data.banner_fit || nextFit;
    applyBannerStateToDOM(false);
  }
}

export async function toggleBannerVisibility(explicitState = null) {
  const nextEnabled = explicitState !== null ? Boolean(explicitState) : !APP_BANNER_STATE.banner_enabled;
  const res = await apiPost("/api/ui/banner/settings", { banner_enabled: nextEnabled });
  if (res.ok && res.data) {
    APP_BANNER_STATE.banner_enabled = res.data.banner_enabled !== false;
    applyBannerStateToDOM(false);
  }
}

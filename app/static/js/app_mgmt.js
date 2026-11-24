import { apiFetch, apiGet, apiPost, state } from "./api.js";


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
      <td>${_mbEsc(e.timestamp || "")}</td>
      <td>${_mbEsc(e.action || "")}</td>
      <td class="${stClass}">${_mbEsc(e.status || "")}</td>
      <td title="${_mbEsc(e.sha256 || "")}">${_mbEsc(hashShort)}</td>
      <td>
        <div style="white-space:${isExpanded ? "pre-wrap" : "normal"}; font-size:0.79rem;">${_mbEsc(shownText)}${expandBtn}</div>
      </td>
    </tr>`;
  }).join("");
}


const DEFAULT_APP_UI_CONFIG = {
  app_name: "Unity IL2CPP Modding Manager & Live MCP Console",
  app_subtitle: "FastAPI + Persistent MCP Stdio Bridge (bepinex-mcp)",
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

export function _setElVisible(id, visible, displayStyle = "") {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = visible ? displayStyle : "none";
}

export function applyAppUiConfig(cfg) {
  if (!cfg) return;
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
  const logEl = document.getElementById("ae-status-log");
  try {
    const resizedDataUrl = await _resizeImageForUpload(file, kind);
    const resp = await apiPost("/api/app-ui-config/upload-asset", {
      kind,
      filename: file.name,
      data_url: resizedDataUrl,
    });
    if (!resp.ok) {
      throw new Error(resp.data?.detail || "Upload failed");
    }
    const payload = resp.data || {};
    const inputId = kind === "banner" ? "ae-banner-url" : "ae-favicon-url";
    const inputEl = document.getElementById(inputId);
    if (inputEl && payload.url) {
      inputEl.value = payload.url;
    }
    onAppEditorInput();
    await saveAppUiConfig();
    if (logEl) logEl.textContent = `Uploaded, resized, and saved ${kind} (${file.name}).`;
  } catch (err) {
    if (logEl) logEl.textContent = `Upload failed: ${err.message || err}`;
  }
  event.target.value = "";
}

export async function removeAppAsset(kind) {
  const logEl = document.getElementById("ae-status-log");
  const inputId = kind === "banner" ? "ae-banner-url" : "ae-favicon-url";
  const el = document.getElementById(inputId);
  if (el) el.value = "";
  onAppEditorInput();
  try {
    const resp = await apiPost("/api/app-ui-config/remove-asset", { kind });
    if (!resp.ok) {
      throw new Error(resp.data?.detail || "Remove failed");
    }
    const data = resp.data || {};
    if (data.config) {
      APP_UI_CONFIG = data.config;
      applyAppUiConfig(APP_UI_CONFIG);
      populateAppEditorForm(APP_UI_CONFIG);
      try { localStorage.setItem("modtool_app_ui_config", JSON.stringify(APP_UI_CONFIG)); } catch {}
    }
    if (logEl) logEl.textContent = `Removed ${kind} and saved settings.`;
    loadLogs();
  } catch (err) {
    if (logEl) logEl.textContent = `Remove failed: ${err.message || err}`;
  }
}

export function setAllElementsVisible(showAll) {
  for (const key of Object.keys(DEFAULT_APP_UI_CONFIG.visibility)) {
    const cb = document.getElementById(`ae-vis-${key}`);
    if (!cb) continue;
    if (showAll) {
      cb.checked = true;
    } else {
      // Minimal mode keeps Game Manager + Active Game + Build a Mod tabs visible, hides secondary chrome
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
  } catch (err) {
    populateAppEditorForm(APP_UI_CONFIG);
  }
}

export async function saveAppUiConfig() {
  APP_UI_CONFIG = readAppEditorForm();
  applyAppUiConfig(APP_UI_CONFIG);
  try { localStorage.setItem("modtool_app_ui_config", JSON.stringify(APP_UI_CONFIG)); } catch {}
  const logEl = document.getElementById("ae-status-log");
  try {
    const data = await apiPost("/api/app-ui-config", { config: APP_UI_CONFIG });
    if (logEl) logEl.textContent = `Saved App Editor settings to ${data.config_path} (SHA-256: ${data.sha256})`;
    loadLogs();
  } catch (err) {
    if (logEl) logEl.textContent = `Save failed: ${err.message || err}`;
  }
}

export async function resetAppUiConfig() {
  const logEl = document.getElementById("ae-status-log");
  try {
    const data = await apiPost("/api/app-ui-config/reset", {});
    APP_UI_CONFIG = data.config || JSON.parse(JSON.stringify(DEFAULT_APP_UI_CONFIG));
    applyAppUiConfig(APP_UI_CONFIG);
    populateAppEditorForm(APP_UI_CONFIG);
    try { localStorage.removeItem("modtool_app_ui_config"); } catch {}
    if (logEl) logEl.textContent = "Reset App Editor settings to defaults.";
    loadLogs();
  } catch (err) {
    if (logEl) logEl.textContent = `Reset failed: ${err.message || err}`;
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
// APP MANAGEMENT (EXPORT, SAVE/RESTORE, TOOL UPGRADES, DIAGNOSTICS)
// =====================================================================
let LAST_APP_DIAGNOSTICS = null;
let LAST_BACKUPS_DIR = "";

export async function exportPortableAppZip() {
  const btn = document.getElementById("btn-export-app");
  const logEl = document.getElementById("am-export-log");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Packaging...";
  }
  try {
    const data = await apiPost("/api/app/export", {});
    if (logEl) {
      logEl.textContent = `${data.summary}\nSHA-256: ${data.zip_sha256}\nPath: ${data.zip_path}\nStarting browser download...`;
    }
    const a = document.createElement("a");
    a.href = data.download_url || "/api/app/export/download";
    a.download = "ModToolPoC-Win64.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
    loadLogs();
  } catch (err) {
    if (logEl) logEl.textContent = `Export failed: ${err.message || err}`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Export App (.zip)";
    }
  }
}

let APP_BACKUPS_LIST = [];
let APP_BACKUPS_EXPANDED = false;
const APP_BACKUP_ROW_EXPANDED = {};

export function toggleAppBackupsExpand() {
  APP_BACKUPS_EXPANDED = !APP_BACKUPS_EXPANDED;
  renderAppBackupsTable();
}

export function toggleBackupRowExpand(idx) {
  APP_BACKUP_ROW_EXPANDED[idx] = !APP_BACKUP_ROW_EXPANDED[idx];
  renderAppBackupsTable();
}

export function renderAppBackupsTable() {
  const tbody = document.getElementById("am-backups-tbody");
  const expandAllBtn = document.getElementById("btn-toggle-backups-expand");
  if (!tbody) return;
  if (!APP_BACKUPS_LIST.length) {
    if (expandAllBtn) expandAllBtn.style.display = "none";
    tbody.innerHTML = `<tr><td colspan="6" style="color:var(--muted);">No saved backups yet. Click Save Snapshot to create one.</td></tr>`;
    return;
  }

  if (expandAllBtn) {
    if (APP_BACKUPS_LIST.length > 5) {
      expandAllBtn.style.display = "inline-flex";
      expandAllBtn.textContent = APP_BACKUPS_EXPANDED
        ? "Show Last 5"
        : `Show All (${APP_BACKUPS_LIST.length})`;
    } else {
      expandAllBtn.style.display = "none";
    }
  }

  const visible = APP_BACKUPS_EXPANDED ? APP_BACKUPS_LIST : APP_BACKUPS_LIST.slice(0, 5);
  const MAX_SUM = 110;
  tbody.innerHTML = visible.map((v, idx) => {
    const st = v.stats || {};
    const chg = `+${st.added ?? 0} ~${st.modified ?? 0} -${st.deleted ?? 0} (${st.total_files ?? 0} total)`;
    const fullSummary = (v.summary_preview || v.reason || "").trim();
    const isExpanded = Boolean(APP_BACKUP_ROW_EXPANDED[idx]);
    const isLong = fullSummary.length > MAX_SUM;
    const previewText = isExpanded
      ? fullSummary
      : (fullSummary.length > MAX_SUM ? `${fullSummary.slice(0, MAX_SUM)}...` : fullSummary);
    const expandBtn = isLong
      ? `<button class="secondary btn-sm" style="margin-left:6px; padding:2px 8px; min-height:22px; font-size:0.7rem;" onclick="toggleBackupRowExpand(${idx})">${isExpanded ? "Collapse" : "Expand"}</button>`
      : "";
    return `
      <tr>
        <td><code>${_mbEsc(v.version_id)}</code></td>
        <td><span class="pill ${v.kind === "baseline" ? "yes" : "warn"}">${_mbEsc(v.kind)}</span></td>
        <td>${_mbEsc(v.created_at)}</td>
        <td>${_mbEsc(chg)}</td>
        <td><div style="white-space:${isExpanded ? "pre-wrap" : "normal"}; font-size:0.78rem;">${_mbEsc(previewText)}${expandBtn}</div></td>
        <td><button class="secondary btn-sm" onclick="restoreAppBackupVersion('${_mbEsc(v.version_id)}')">Restore</button></td>
      </tr>
    `;
  }).join("");
}

export async function loadAppBackups() {
  const tbody = document.getElementById("am-backups-tbody");
  try {
    const res = await apiFetch("/api/app/backups");
    const data = await res.json();
    LAST_BACKUPS_DIR = data.backups_dir || "";
    APP_BACKUPS_LIST = data.versions || [];
    renderAppBackupsTable();
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="6">Failed to load backups: ${_mbEsc(err.message || err)}</td></tr>`;
  }
}

export async function saveAppSnapshot(forceBaseline) {
  const reason = document.getElementById("am-backup-reason")?.value || "Manual save from App Management";
  const logEl = document.getElementById("am-backup-log");
  try {
    const data = await apiPost("/api/app/backup/save", { reason, force_baseline: Boolean(forceBaseline) });
    if (logEl) logEl.textContent = data.summary || "Snapshot saved.";
    await loadAppBackups();
    loadLogs();
  } catch (err) {
    if (logEl) logEl.textContent = `Save failed: ${err.message || err}`;
  }
}

export async function restoreAppBackupVersion(versionId) {
  if (!confirm(`Restore the entire project to ${versionId}? Any unsaved changes since ${versionId} will be overwritten.`)) {
    return;
  }
  const logEl = document.getElementById("am-backup-log");
  try {
    const data = await apiPost("/api/app/backup/restore", { version_id: versionId });
    if (logEl) logEl.textContent = `${data.summary}. Reload the page if UI files changed.`;
    await loadAppBackups();
    loadLogs();
  } catch (err) {
    if (logEl) logEl.textContent = `Restore failed: ${err.message || err}`;
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
          <td><strong>${_mbEsc(t.name)}</strong></td>
          <td><code>${_mbEsc(t.version)}</code></td>
          <td title="${_mbEsc(t.sha256 || '')}"><code>${_mbEsc(shaShort)}</code></td>
          <td>${secPill}</td>
          <td><button class="btn-sm" onclick="upgradeModdingTool('${_mbEsc(t.id)}')">${btnLabel}</button></td>
        </tr>
      `;
    }).join("");
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="5">Failed to load tools: ${_mbEsc(err.message || err)}</td></tr>`;
  }
}

export async function upgradeModdingTool(toolId) {
  const logEl = document.getElementById("am-tools-log");
  if (logEl) logEl.textContent = `Upgrading ${toolId} with security verification...`;
  try {
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
  } catch (err) {
    if (logEl) logEl.textContent = `Upgrade blocked/failed: ${err.message || err}`;
  }
}

let APP_INSTALLED_SHORT_SHA = "";

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
      badge.textContent = APP_INSTALLED_SHORT_SHA ? `v${ver} (${APP_INSTALLED_SHORT_SHA})` : `v${ver}`;
    }
    if (disp) disp.textContent = `v${ver} (${ch})`;
    if (launchBtn) launchBtn.style.display = data.launched ? "none" : "inline-flex";
  } catch {}
}

export async function loadGitHubRepoConfig() {
  try {
    const res = await apiGet("/api/app/repo-config");
    if (res.ok && res.data) {
      const d = res.data;
      const ownerEl = document.getElementById("gh-owner");
      const repoEl = document.getElementById("gh-repo");
      const branchEl = document.getElementById("gh-branch");
      const tokEl = document.getElementById("gh-token");
      const shaEl = document.getElementById("gh-installed-sha");
      const badgeEl = document.getElementById("gh-sync-badge");

      if (ownerEl) ownerEl.value = d.owner || "";
      if (repoEl) repoEl.value = d.repo || "";
      if (branchEl) branchEl.value = d.branch || "main";
      if (tokEl) tokEl.value = d.token_set ? (d.token_masked || "******") : "";
      APP_INSTALLED_SHORT_SHA = d.installed_short_sha || "";
      if (shaEl) shaEl.textContent = d.installed_short_sha ? `${d.installed_short_sha} (${d.installed_sha})` : "Unset";
      if (badgeEl) {
        if (!d.owner || !d.repo) {
          badgeEl.className = "pill na";
          badgeEl.textContent = "Unconfigured";
        } else {
          badgeEl.className = "pill warn";
          badgeEl.textContent = "Configured";
        }
      }
      await loadAppVersion();
    }
    const bRes = await apiGet("/api/app/backups");
    if (bRes.ok && bRes.data) {
      const sel = document.getElementById("gh-snapshots-select");
      const snaps = Array.isArray(bRes.data.snapshots) ? bRes.data.snapshots : [];
      if (sel) {
        sel.innerHTML = snaps.length
          ? snaps.map(s => `<option value="${s.name}">${s.name} (${s.file_count || 0} files)</option>`).join("")
          : '<option value="">No local snapshots yet</option>';
      }
    }
  } catch (e) {
    console.warn("Failed to load repo config:", e);
  }
}

export async function saveGitHubRepoConfig() {
  const logEl = document.getElementById("gh-sync-log");
  let ownerVal = (document.getElementById("gh-owner")?.value || "").trim();
  let repoVal = (document.getElementById("gh-repo")?.value || "").trim();
  for (const cand of [ownerVal, repoVal]) {
    const m = cand.match(/github\.com[:/]([^/\s]+)\/([^/\s?#]+)/i);
    if (m) {
      ownerVal = m[1];
      repoVal = m[2].replace(/\.git$/i, "");
      break;
    }
  }
  const res = await apiPost("/api/app/repo-config", {
    owner: ownerVal,
    repo: repoVal,
    branch: (document.getElementById("gh-branch")?.value || "main").trim(),
    token: (document.getElementById("gh-token")?.value || "").trim(),
  });
  if (res.ok) {
    if (logEl) logEl.textContent = "Repository settings saved.";
    await loadGitHubRepoConfig();
    loadLogs();
  } else if (logEl) {
    logEl.textContent = `Save failed: ${res.data?.detail || "Unknown error"}`;
  }
}

export async function checkGitHubUpdates() {
  const btn = document.getElementById("btn-gh-check");
  const logEl = document.getElementById("gh-sync-log");
  const remEl = document.getElementById("gh-remote-latest");
  const badgeEl = document.getElementById("gh-sync-badge");
  if (btn) { btn.disabled = true; btn.textContent = "Checking..."; }
  try {
    const res = await apiPost("/api/app/check-update", {});
    const d = res.data || {};
    if (d.status === "ok") {
      if (remEl) remEl.textContent = `${d.short_sha} — ${d.commit_message || ""}`;
      if (badgeEl) {
        badgeEl.className = d.update_available ? "pill warn" : "pill yes";
        badgeEl.textContent = d.update_available ? "Update Available" : "Up to Date";
      }
      if (logEl) logEl.textContent = JSON.stringify(d, null, 2);
    } else {
      if (badgeEl) { badgeEl.className = "pill no"; badgeEl.textContent = d.status || "Error"; }
      if (logEl) logEl.textContent = d.message || "Update check failed.";
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Check GitHub"; }
  }
}

export async function pullGitHubSync() {
  if (!confirm("Pull latest files from GitHub? A pre-sync snapshot will be saved first.")) return;
  const btn = document.getElementById("btn-gh-sync");
  const logEl = document.getElementById("gh-sync-log");
  if (btn) { btn.disabled = true; btn.textContent = "Syncing..."; }
  try {
    const res = await apiPost("/api/app/sync", {});
    if (res.ok) {
      if (logEl) logEl.textContent = `Synced ${res.data.extracted_count} file(s). Snapshot: ${res.data.snapshot}. Archive SHA-256: ${res.data.archive_sha256}`;
      await loadGitHubRepoConfig();
      loadLogs();
    } else if (logEl) {
      logEl.textContent = `Sync failed: ${res.data?.detail || "Unknown error"}`;
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Pull / Restore Latest from GitHub"; }
  }
}

export async function rollbackToSnapshot() {
  const sel = document.getElementById("gh-snapshots-select");
  const snap = sel?.value || "";
  const logEl = document.getElementById("gh-sync-log");
  if (!snap) return;
  if (!confirm(`Roll back application files to snapshot '${snap}'?`)) return;
  const res = await apiPost("/api/app/rollback", { snapshot: snap });
  if (res.ok) {
    if (logEl) logEl.textContent = `Rolled back ${res.data.restored_count} file(s) from ${snap}. Safety snapshot: ${res.data.safety_snapshot}. Click Reload Server to apply backend changes.`;
    await loadGitHubRepoConfig();
    loadLogs();
  } else if (logEl) {
    logEl.textContent = `Rollback failed: ${res.data?.detail || "Unknown error"}`;
  }
}

export async function loadGitStatus() {
  const initBadge = document.getElementById("git-init-badge");
  const brBadge = document.getElementById("git-branch-badge");
  const remBadge = document.getElementById("git-remote-badge");
  const chgBadge = document.getElementById("git-changes-badge");
  const listEl = document.getElementById("git-changed-files-list");
  try {
    const res = await apiGet("/api/app/git-status");
    if (!res.ok || !res.data) return;
    const d = res.data;
    if (!d.git_available) {
      if (initBadge) { initBadge.className = "pill no"; initBadge.textContent = "Git CLI Not Found"; }
      if (listEl) listEl.textContent = "Install Git for Windows to enable local commits and Push to GitHub.";
      return;
    }
    if (initBadge) {
      initBadge.className = d.is_repo ? "pill yes" : "pill warn";
      initBadge.textContent = d.is_repo ? "Initialized" : "Not Initialized";
    }
    if (brBadge) brBadge.textContent = `Branch: ${d.branch || "—"}`;
    if (remBadge) remBadge.textContent = `Origin: ${d.remote_url || "—"}`;
    const files = Array.isArray(d.changed_files) ? d.changed_files : [];
    if (chgBadge) chgBadge.textContent = `${files.length} Uncommitted`;
    if (listEl) {
      if (!d.is_repo) {
        listEl.textContent = "Repository not initialized yet — Push to GitHub will run git init and create .gitignore automatically.";
      } else if (files.length === 0) {
        const latest = d.latest_local_commit ? `Latest commit: ${d.latest_local_commit.short_sha} (${d.latest_local_commit.message})` : "Working tree clean.";
        listEl.textContent = latest;
      } else {
        const preview = files.slice(0, 8).join(", ") + (files.length > 8 ? ` (+${files.length - 8} more)` : "");
        listEl.textContent = `Uncommitted: ${preview}`;
      }
    }
  } catch (e) {
    console.warn("Failed to load git status:", e);
  }
}

export async function copyGitPushError() {
  const summary = document.getElementById("gh-push-error-summary")?.textContent || "";
  const pre = document.getElementById("gh-push-error-pre")?.textContent || "";
  const txt = [summary, pre].filter(Boolean).join("\n");
  if (!txt) return;
  try {
    await navigator.clipboard.writeText(txt);
    const btn = document.getElementById("btn-copy-git-error");
    if (btn) {
      btn.textContent = "Copied!";
      setTimeout(() => { btn.textContent = "Copy Error"; }, 1500);
    }
  } catch {
    // ignore clipboard errors in restricted contexts
  }
}

export async function pushAppToGitHub() {
  const btn = document.getElementById("btn-gh-push");
  const logEl = document.getElementById("gh-sync-log");
  const errBox = document.getElementById("gh-push-error-box");
  const errSum = document.getElementById("gh-push-error-summary");
  const errPre = document.getElementById("gh-push-error-pre");
  const msg = (document.getElementById("gh-commit-msg")?.value || "").trim();
  const branch = (document.getElementById("gh-branch")?.value || "main").trim() || "main";
  if (errBox) errBox.style.display = "none";
  if (btn) { btn.disabled = true; btn.textContent = "Pushing..."; }
  if (logEl) logEl.textContent = "Staging changes, committing, and pushing to GitHub...";
  try {
    const res = await apiFetch("/api/app/git-push", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ commit_message: msg, branch }),
    });
    const text = await res.text();
    let data = {};
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
    if (res.ok) {
      if (logEl) {
        if (data.status === "up_to_date") {
          logEl.textContent = data.message || "Everything is already up to date with origin.";
        } else {
          logEl.textContent = `Pushed ${data.short_sha || "HEAD"} to ${data.branch} (${data.changed_file_count} changed file(s)).\n${data.output || ""}`;
        }
      }
      const msgEl = document.getElementById("gh-commit-msg");
      if (msgEl) msgEl.value = "";
      await Promise.all([loadGitStatus(), loadGitHubRepoConfig(), loadLogs()]);
    } else {
      const step = data.step || "push";
      const code = data.exit_code !== undefined ? data.exit_code : res.status;
      const hint = data.hint || data.detail || "Git push failed";
      const details = [data.stderr, data.stdout].filter(Boolean).join("\n\n") || text || "Unknown error";
      if (logEl) logEl.textContent = `Push failed at '${step}' (exit ${code}): ${hint}`;
      if (errSum) errSum.textContent = `Step: ${step} · Exit: ${code} — ${hint}`;
      if (errPre) errPre.textContent = details;
      if (errBox) errBox.style.display = "block";
      await loadLogs();
    }
  } catch (err) {
    if (logEl) logEl.textContent = `Push failed: ${err?.message || err}`;
    if (errSum) errSum.textContent = "Git Push Request Error";
    if (errPre) errPre.textContent = String(err?.message || err);
    if (errBox) errBox.style.display = "block";
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Push to GitHub"; }
  }
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
    ? LAST_BACKUPS_DIR
    : (LAST_APP_DIAGNOSTICS?.app_data_dir || "");
  if (!path) return;
  try {
    await apiPost("/api/tools/open-folder", { path });
  } catch (err) {
    alert(`Failed to open folder: ${err.message || err}`);
  }
}

export async function clearBuiltModsCache() {
  if (!confirm("Clear all cached built mod folders in %LOCALAPPDATA%\\ModToolPoC\\built_mods?")) return;
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
  if (!confirm("Restart the ModToolPoC server now?")) return;
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

export function toggleBannerMenu(ev) {
  if (ev) ev.stopPropagation();
  const dd = document.getElementById("banner-dropdown");
  if (dd) dd.classList.toggle("open");
}

export function registerBannerMenuListener() {
  document.addEventListener("click", (ev) => {
    const wrap = document.getElementById("banner-menu-wrap");
    const dd = document.getElementById("banner-dropdown");
    if (dd && wrap && !wrap.contains(ev.target)) {
      dd.classList.remove("open");
    }
  });
}

export function triggerBannerUpload() {
  const dd = document.getElementById("banner-dropdown");
  if (dd) dd.classList.remove("open");
  const inp = document.getElementById("app-banner-file-input");
  if (inp) {
    inp.value = "";
    inp.click();
  }
}

export function applyBannerStateToDOM(cacheBust = false) {
  const slot = document.getElementById("app-banner-slot");
  const img = document.getElementById("app-banner-img");
  const fitBtn = document.getElementById("btn-banner-fit-toggle");
  const visBtn = document.getElementById("btn-banner-vis-toggle");
  const fit = APP_BANNER_STATE.banner_fit === "cover" ? "cover" : "contain";

  document.documentElement.style.setProperty("--banner-fit", fit);
  if (fitBtn) fitBtn.textContent = fit === "cover" ? "Fit: Fill (Cover)" : "Fit: Full (Contain)";
  if (visBtn) visBtn.textContent = APP_BANNER_STATE.banner_enabled ? "Hide Banner" : "Show Banner";

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
  const fd = new FormData();
  fd.append("file", file);
  const resp = await apiFetch("/api/ui/banner", { method: "POST", body: fd });
  if (resp.ok) {
    const data = await resp.json();
    APP_BANNER_STATE.banner_enabled = data.banner_enabled !== false;
    APP_BANNER_STATE.banner_file = data.banner_file || "";
    APP_BANNER_STATE.banner_fit = data.banner_fit === "cover" ? "cover" : "contain";
    APP_BANNER_STATE.has_banner = true;
    applyBannerStateToDOM(true);
  }
}

export async function toggleBannerFit() {
  const dd = document.getElementById("banner-dropdown");
  if (dd) dd.classList.remove("open");
  const nextFit = APP_BANNER_STATE.banner_fit === "cover" ? "contain" : "cover";
  const res = await apiPost("/api/ui/banner/settings", { banner_fit: nextFit });
  if (res.ok && res.data) {
    APP_BANNER_STATE.banner_fit = res.data.banner_fit || nextFit;
    applyBannerStateToDOM(false);
  }
}

export async function toggleBannerVisibility(explicitState = null) {
  const dd = document.getElementById("banner-dropdown");
  if (dd) dd.classList.remove("open");
  const nextEnabled = explicitState !== null ? Boolean(explicitState) : !APP_BANNER_STATE.banner_enabled;
  const res = await apiPost("/api/ui/banner/settings", { banner_enabled: nextEnabled });
  if (res.ok && res.data) {
    APP_BANNER_STATE.banner_enabled = res.data.banner_enabled !== false;
    applyBannerStateToDOM(false);
  }
}


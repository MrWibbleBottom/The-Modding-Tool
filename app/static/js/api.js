// Shared API client with session-token header, health reconnect, shared state, and navigation.


export const appHooks = {
  onTabSwitch: null,
  onReconnect: null,
  getCommands: null,
};

export const state = {
  managedGames: [],
  activeGameId: "battleground",
  pcSpecsData: null,
  lastRenderedGame: null,
  lastBuiltMod: null,
  lastMbGameId: null,
  activeLogCategory: "game",
  serverWasDisconnected: false,
  initialLoadFailed: false,
  lastAssetAudit: null,
};

export function getSessionToken() {
  const meta = document.querySelector('meta[name="modtool-session-token"]');
  return meta ? meta.getAttribute("content") || "" : "";
}

export async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getSessionToken();
  if (token) {
    headers.set("X-ModTool-Session", token);
  }
  return fetch(url, { ...options, headers });
}

export async function apiGet(url) {
  const res = await apiFetch(url, { cache: "no-store" });
  const text = await res.text();
  let data = {};
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text };
  }
  return { ok: res.ok, status: res.status, data };
}

export async function apiPost(url, body) {
  const res = await apiFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const text = await res.text();
  let parsed = {};
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = { raw: text };
  }
  if (!res.ok) {
    throw new Error(parsed.detail || text || `HTTP ${res.status}`);
  }
  return parsed;
}

export function setOutput(data) {
  const el = document.getElementById("output-pre");
  if (!el) return;
  el.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
}

export function switchTab(tabName) {
  for (const name of ["manager", "library", "game", "modbuilder", "specs", "logs", "appmgmt", "help"]) {
    const btn = document.getElementById(`tab-btn-${name}`);
    const pane = document.getElementById(`tab-pane-${name}`);
    if (btn) btn.classList.toggle("active", name === tabName);
    if (pane) pane.classList.toggle("active", name === tabName);
  }
  if (tabName === "library") {
    loadGameLibrary();
  } else if (tabName === "modbuilder") {
    renderModBuilderRules();
  } else if (tabName === "specs") {
    loadPcSpecs(false);
  } else if (tabName === "logs") {
    loadLogs();
  } else if (tabName === "appmgmt") {
    loadAppBackups();
    loadGitHubRepoConfig();
    loadGitStatus();
    loadModdingToolsStatus();
    loadAppDiagnostics();
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

export function openCommandPalette() {
  const ov = document.getElementById("cmd-palette-overlay");
  const inp = document.getElementById("cmd-palette-input");
  if (!ov) return;
  ov.style.display = "flex";
  if (inp) { inp.value = ""; inp.focus(); }
  renderCommandPalette();
}

export function closeCommandPalette() {
  const ov = document.getElementById("cmd-palette-overlay");
  if (ov) ov.style.display = "none";
}

export function renderCommandPalette() {
  const list = document.getElementById("cmd-palette-list");
  const q = (document.getElementById("cmd-palette-input")?.value || "").trim().toLowerCase();
  if (!list) return;
  const items = [
    { label: "Tab: Game Manager", fn: () => switchTab("manager") },
    { label: "Tab: Active Game", fn: () => switchTab("game") },
    { label: "Tab: Build a Mod", fn: () => switchTab("modbuilder") },
    { label: "Tab: PC Specs", fn: () => switchTab("specs") },
    { label: "Tab: Audit Log", fn: () => switchTab("logs") },
    { label: "Tab: App Management", fn: () => switchTab("appmgmt") },
    { label: "Tab: Help", fn: () => switchTab("help") },
    { label: "Action: Run Game Scan", fn: () => { switchTab("manager"); scanPlatforms(""); } },
    { label: "Action: Fingerprint Active Game", fn: () => { switchTab("game"); runFingerprint(); } },
    { label: "Action: Pre-warm All Game Icons", fn: () => prewarmAllGameIcons() },
    { label: "Action: Save App Snapshot", fn: () => { switchTab("appmgmt"); saveAppSnapshot(false); } },
    { label: "Action: Export Portable App (.zip)", fn: () => { switchTab("appmgmt"); exportPortableAppZip(); } },
    ...state.managedGames.map(g => ({ label: `Load Game: ${g.name}`, fn: () => loadAndOpenGame(g.id) })),
  ];
  window._CMD_PALETTE_ITEMS = items.filter(it => !q || it.label.toLowerCase().includes(q)).slice(0, 15);
  list.innerHTML = window._CMD_PALETTE_ITEMS.map((it, i) => `
    <button class="secondary" style="justify-content:flex-start; width:100%;" onclick="closeCommandPalette(); window._CMD_PALETTE_ITEMS[${i}].fn();">${_mbEsc(it.label)}</button>
  `).join("");
}

// keydown & resize listeners registered in main.js
export function registerGlobalListeners() {
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openCommandPalette();
    } else if (e.key === "Escape") {
      closeCommandPalette();
    }
  });
  window.addEventListener("resize", applyResponsiveResolutionLayout);
}

export function applyResponsiveResolutionLayout() {
  const scrW = (state.pcSpecsData?.display?.width) || Math.round((window.screen?.width || window.innerWidth) * (window.devicePixelRatio || 1));
  const scrH = (state.pcSpecsData?.display?.height) || Math.round((window.screen?.height || window.innerHeight) * (window.devicePixelRatio || 1));
  const vpW = window.innerWidth || scrW;

  let maxW = "100%";
  if (vpW >= 2400) maxW = "1760px";
  else if (vpW >= 1800) maxW = "1480px";
  else if (vpW >= 1280) maxW = "1240px";

  document.documentElement.style.setProperty("--page-max-width", maxW);
  const resBadge = document.getElementById("resolution-badge");
  if (resBadge) {
    resBadge.textContent = `${scrW}x${scrH} (Viewport ${vpW}px)`;
  }
}



export function _setServerPillState(connected) {
  const badge = document.getElementById("server-badge");
  if (!badge) return;
  if (connected) {
    badge.textContent = "127.0.0.1:8765";
    badge.style.background = "rgba(22, 163, 74, 0.2)";
    badge.style.borderColor = "#16a34a";
    badge.style.color = "#86efac";
  } else {
    badge.textContent = "Reconnecting...";
    badge.style.background = "rgba(217, 119, 6, 0.22)";
    badge.style.borderColor = "#d97706";
    badge.style.color = "#fcd34d";
  }
}

export async function pollServerHealth() {
  try {
    const resp = await apiFetch("/api/health", { cache: "no-store" });
    if (!resp.ok) throw new Error("Health check failed");
    _setServerPillState(true);
    if (state.serverWasDisconnected || state.initialLoadFailed) {
      state.serverWasDisconnected = false;
      state.initialLoadFailed = false;
      Promise.all([
        loadAppVersion(),
        loadGitHubRepoConfig(),
        refreshGamesList(false),
        loadLogs(),
      ]).catch(() => {});
    }
  } catch {
    state.serverWasDisconnected = true;
    _setServerPillState(false);
  }
}


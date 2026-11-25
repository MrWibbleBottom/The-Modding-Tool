import { state } from "./api.js";

export { state };

export const libState = {
  sortColumn: "name",
  sortDirection: "asc",
  libraryEntries: [],
  scanProgressTimer: null,
  scanProgressCallback: null,
  autoRescanInFlight: new Set(),
};

export const STEAM_ASSETS_JS_CACHE = new Map();

export function resolveBtn(fallbackId = "") {
  const evTarget = typeof window !== "undefined" ? window.event?.target : null;
  const fromEvent = evTarget && typeof evTarget.closest === "function" ? evTarget.closest("button") : null;
  return fromEvent || (fallbackId ? document.getElementById(fallbackId) : null);
}

export function escHtml(v) {
  return String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function classifyGameEngine(game) {
  const info = game?.modding_info || {};
  const detected = String(game?.detected_engine || "").toLowerCase();
  const cat = String(info.mcp_category || "").toLowerCase();
  const tech = String(info.sheet_engine_tech || "").toLowerCase();
  const backend = String(info.scripting_backend || "").toLowerCase();
  const runtime = String(game?.runtime || "").toLowerCase();
  const unityVer = String(info.unity_version || "").trim();
  const hasRealUnityVer = Boolean(unityVer && unityVer !== "Unknown" && unityVer !== "Not scraped yet");

  const isUnreal = Boolean(
    info.is_unreal_engine ||
    info.unreal_settings ||
    detected === "unreal" ||
    cat.includes("unreal") ||
    tech.includes("unreal") ||
    backend.includes("unreal") ||
    runtime.includes("unreal")
  );

  const isUnity = !isUnreal && Boolean(
    detected.startsWith("unity") ||
    cat === "unity" ||
    hasRealUnityVer ||
    backend.includes("il2cpp") ||
    backend.includes("mono") ||
    runtime.startsWith("unity")
  );

  return { isUnity, isUnreal };
}

export function upsertManagedGameInState(game) {
  if (!game || !game.id || !Array.isArray(state.managedGames)) return;
  const idx = state.managedGames.findIndex((item) => item.id === game.id);
  if (idx >= 0) {
    state.managedGames[idx] = game;
  } else {
    state.managedGames.push(game);
  }
}

export function getUiVisibility() {
  if (state.appUiConfig?.visibility) return state.appUiConfig.visibility;
  if (typeof window !== "undefined" && window.APP_UI_CONFIG?.visibility) {
    return window.APP_UI_CONFIG.visibility;
  }
  return {};
}

export function sanitizeLegacyPaths(value) {
  if (typeof value === "string") {
    return value
      .replaceAll("Battleground", "The-Modding-Tool")
      .replaceAll("ModToolPoC", "TheModdingTool")
      .replace(/The-Modding-Tool([\\/])bepinex-mcp(?![\\/]?bridges)/g, "The-Modding-Tool$1bridges$1bepinex-mcp")
      .replace(/bridges[\\/]bepinex-mcp[\\/]\.venv/g, ".venv");
  }
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeLegacyPaths(item));
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = sanitizeLegacyPaths(v);
    }
    return out;
  }
  return value;
}

export function sanitizeBrowserLocalStorage() {
  if (typeof localStorage === "undefined") return 0;
  let updated = 0;
  try {
    const keys = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (k) keys.push(k);
    }
    for (const key of keys) {
      const raw = localStorage.getItem(key);
      if (typeof raw !== "string" || !raw) continue;
      let nextVal = raw;
      const trimmed = raw.trim();
      if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
        try {
          const parsed = JSON.parse(raw);
          const cleaned = sanitizeLegacyPaths(parsed);
          const serialized = JSON.stringify(cleaned);
          if (serialized !== JSON.stringify(parsed)) {
            nextVal = serialized;
          }
        } catch {
          nextVal = sanitizeLegacyPaths(raw);
        }
      } else {
        nextVal = sanitizeLegacyPaths(raw);
      }
      if (nextVal !== raw) {
        localStorage.setItem(key, nextVal);
        updated += 1;
      }
    }
  } catch {
    // Ignore storage access errors in restricted browser modes
  }
  return updated;
}

// Entry point for The Modding Tool native ES module frontend.

import * as api from "./api.js";
import * as library from "./library.js";
import * as analyzer from "./analyzer.js";
import * as mcp from "./mcp.js";
import * as appMgmt from "./app_mgmt.js";

// Expose handlers globally for inline HTML event attributes (onclick/onchange/oninput)
Object.assign(
  window,
  api,
  library,
  analyzer,
  mcp,
  appMgmt,
);

api.appHooks.onTabSwitch = (tabName) => {
  if (tabName === "logs") appMgmt.loadLogs();
  if (tabName === "specs" && !api.state.pcSpecsData) analyzer.loadPcSpecs(false);
  if (tabName === "modbuilder") {
    const g = api.state.managedGames.find((x) => x.id === api.state.activeGameId);
    if (g) mcp.syncModBuilderWithGame(g);
  }
  if (tabName === "appmgmt") {
    appMgmt.loadAppBackups();
    appMgmt.loadModdingToolsStatus();
    appMgmt.loadAppVersion();
    appMgmt.loadGitHubRepoConfig();
    appMgmt.loadGitStatus();
    appMgmt.loadAppDiagnostics();
    appMgmt.loadWatcherStatus();
  }
};

api.appHooks.onReconnect = async () => {
  await library.refreshGamesList(true);
  await appMgmt.loadLogs();
};

api.registerGlobalListeners();

window.addEventListener("DOMContentLoaded", async () => {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch((err) => {
      console.warn("Service worker registration failed:", err);
    });
  }
  api.applyResponsiveResolutionLayout();
  const loadingOverlay = document.getElementById("app-loading-overlay");
  const loadingStatus = document.getElementById("app-loading-status");
  try {
    if (loadingStatus) loadingStatus.textContent = "Loading UI config, PC specs, and game library...";
    await Promise.all([
      appMgmt.loadAppVersion(),
      appMgmt.loadGitHubRepoConfig(),
      appMgmt.loadGitStatus(),
      appMgmt.loadAppUiConfig(),
      appMgmt.loadPersistentBanner(),
      analyzer.loadPcSpecs(false),
      library.refreshGamesList(true),
      appMgmt.loadLogs(),
      appMgmt.loadWatcherStatus(),
    ]);
    api.applyResponsiveResolutionLayout();
    api._setServerPillState(true);
  } catch {
    api.state.initialLoadFailed = true;
    api._setServerPillState(false);
  } finally {
    if (loadingOverlay) loadingOverlay.style.display = "none";
    setInterval(api.pollServerHealth, 5000);
  }
});

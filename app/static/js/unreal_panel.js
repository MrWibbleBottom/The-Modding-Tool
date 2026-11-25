import { apiGet, apiPost, runTrackedAction, state } from "./api.js";
import { classifyGameEngine, escHtml, resolveBtn, upsertManagedGameInState } from "./state.js";
import { loadGameSnapshots, renderActiveGamePanel, rescanActiveGameMetadata } from "./active_game.js";
import { refreshGamesList } from "./game_table.js";

let _pendingIniPreviewPayload = null;

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
  const { isUnreal } = classifyGameEngine(game);

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

  dismissUnrealIniDiff();
  loadUnrealIniTweaks(game);
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
  const data = await apiPost("/api/games/unreal-settings", {
    game_id: state.activeGameId,
    custom_ini_path: customPath,
    reference_tier_id: refTierId
  });
  if (data && data.game) {
    upsertManagedGameInState(data.game);
    renderActiveGamePanel(data.game, { allowAutoFetch: false });
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

function _collectIniTweakStates() {
  const states = {};
  document.querySelectorAll("#ue-ini-tweaks-list input.ue-tweak-checkbox").forEach((cb) => {
    const tid = cb.dataset.tweakId;
    if (tid) states[tid] = Boolean(cb.checked);
  });
  return states;
}

export async function loadUnrealIniTweaks(game = null) {
  const active = game || state.managedGames.find(g => g.id === state.activeGameId) || state.lastRenderedGame;
  const bar = document.getElementById("ue-ini-tweaks-bar");
  const list = document.getElementById("ue-ini-tweaks-list");
  if (!active || !bar || !list) return;
  const gusPath = active.modding_info?.unreal_settings?.ini_path || "";
  const url = `/api/games/${encodeURIComponent(active.id)}/unreal-ini/tweaks` +
    (gusPath ? `?gus_ini_path=${encodeURIComponent(gusPath)}` : "");
  try {
    const res = await apiGet(url);
    if (!res.ok || !Array.isArray(res.data?.presets)) {
      bar.style.display = "none";
      return;
    }
    const presets = res.data.presets;
    bar.style.display = presets.length ? "block" : "none";
    list.innerHTML = presets.map((p) => (
      `<label class="ini-tweak-chip" title="${escHtml(p.description || "")} (${escHtml(p.target_file || "")})">` +
      `<input type="checkbox" class="ue-tweak-checkbox" data-tweak-id="${escHtml(p.id)}" ${p.active ? "checked" : ""} />` +
      `<span>${escHtml(p.label)} <small style="color:var(--muted);">[${escHtml(p.target_file)}]</small></span>` +
      `</label>`
    )).join("");
    list.querySelectorAll("input.ue-tweak-checkbox").forEach((cb) => {
      cb.addEventListener("change", () => {
        previewUnrealIniTweaks({}, "Pre-INI fenced tweak edit");
      });
    });
  } catch {
    bar.style.display = "none";
  }
}

export function dismissUnrealIniDiff() {
  const panel = document.getElementById("ue-ini-diff-panel");
  if (panel) panel.style.display = "none";
  _pendingIniPreviewPayload = null;
}

export async function previewUnrealIniTweaks(extraUpdates = {}, label = "Pre-INI tweak edit") {
  const game = state.managedGames.find(g => g.id === state.activeGameId) || state.lastRenderedGame || {};
  const slug = game.id || state.activeGameId;
  const gusPath = game.modding_info?.unreal_settings?.ini_path || "";
  const payload = {
    tweak_states: _collectIniTweakStates(),
    updates: extraUpdates && typeof extraUpdates === "object" ? extraUpdates : {},
    gus_ini_path: gusPath,
    label,
  };

  const panel = document.getElementById("ue-ini-diff-panel");
  const summaryEl = document.getElementById("ue-ini-diff-summary");
  const diffPre = document.getElementById("ue-ini-diff-pre");
  const applyBtn = document.getElementById("btn-ue-apply-diff");
  const btn = resolveBtn("btn-ue-preview-diff");

  try {
    const res = await runTrackedAction(
      btn,
      async () => apiPost(`/api/games/${encodeURIComponent(slug)}/unreal-ini/preview`, payload),
      {
        pendingText: "Generating Unreal INI diff preview...",
        successText: (d) => d?.has_changes ? "Unified diff ready — review before applying" : "No INI changes detected",
      },
    );
    _pendingIniPreviewPayload = res?.has_changes ? payload : null;
    if (panel) panel.style.display = "block";
    if (applyBtn) applyBtn.disabled = !res?.has_changes;
    if (summaryEl) {
      const changedFiles = (res?.files || []).filter(f => f.changed);
      summaryEl.textContent = res?.has_changes
        ? `Unified Diff Preview — ${changedFiles.length} file(s) will change (auto-snapshot created before write)`
        : "Unified Diff Preview — Selected INI settings already match target values";
    }
    if (diffPre) {
      diffPre.textContent = (res?.unified_diff || "").trim() || "(No line differences — INI files are already in the requested state.)";
    }
    return res;
  } catch {
    return null;
  }
}

export async function applyPreviewedUnrealIniTweaks() {
  if (!_pendingIniPreviewPayload) {
    alert("Preview Unreal INI changes first before applying.");
    return;
  }
  const btn = resolveBtn("btn-ue-apply-diff");
  const game = state.managedGames.find(g => g.id === state.activeGameId) || state.lastRenderedGame || {};
  const slug = game.id || state.activeGameId;
  const payload = _pendingIniPreviewPayload;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost(`/api/games/${encodeURIComponent(slug)}/unreal-ini/apply`, payload);
        dismissUnrealIniDiff();
        await Promise.all([
          importUnrealSettings(payload.gus_ini_path || ""),
          loadGameSnapshots(slug),
        ]);
        return res;
      },
      {
        pendingText: "Creating snapshot and applying Unreal INI changes...",
        successText: (d) => d?.summary || "Applied Unreal INI changes",
      },
    );
  } catch {
    // Error handled by runTrackedAction
  }
}

export async function applyOptimalUnrealIni() {
  const game = state.managedGames.find(g => g.id === state.activeGameId) || state.lastRenderedGame || {};
  const ue = game.modding_info?.unreal_settings;
  if (!ue || !ue.ini_path) {
    alert("No Unreal GameUserSettings.ini detected for this game yet. Click Rescan Config first.");
    return;
  }
  const updates = {};
  for (const row of (ue.settings || [])) {
    if (row.recommended_value !== undefined && row.recommended_value !== null && row.key) {
      updates[row.key] = String(row.recommended_value);
    }
  }
  await previewUnrealIniTweaks(updates, "Pre-Optimal INI edit");
}

export async function restoreUnrealIniBackup() {
  const btn = resolveBtn("");
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

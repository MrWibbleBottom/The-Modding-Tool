import { apiFetch, apiGet, apiPost, state } from "./api.js";
import { loadLogs } from "./app_mgmt.js";

export async function loadPcSpecs(forceScan = false) {
  const btn = document.getElementById("btn-rescan-specs");
  if (btn && forceScan) {
    btn.disabled = true;
    btn.textContent = "Scraping Hardware...";
  }
  if (!forceScan && !state.pcSpecsData) {
    try {
      const cachedSpecsRaw = localStorage.getItem("modtool_cache_pc_specs");
      if (cachedSpecsRaw) {
        const cachedPayload = JSON.parse(cachedSpecsRaw);
        if (cachedPayload?.specs) {
          state.pcSpecsData = cachedPayload.specs;
          const tabBadge = document.getElementById("specs-tab-badge");
          if (tabBadge && cachedPayload.specs.cpu && cachedPayload.specs.gpu) {
            const c = String(cachedPayload.specs.cpu.name || "").replace(/\(R\)|\(TM\)/gi, "");
            const g = String(cachedPayload.specs.gpu.name || "");
            const cm = c.match(/\b(i[3579]-\d+[A-Z]*|Ultra\s+\d+\s+\d+[A-Z]*)\b/i)?.[1] || c.split("@")[0].trim();
            const gm = g.match(/\b(?:RTX|GTX|RX|Arc)\s+([A-Za-z0-9-]+(?:\s+(?:Ti|SUPER|Super|XT|XTX|GRE))?)\b/i)?.[1] || g;
            tabBadge.textContent = `${cm} | ${gm}`;
          }
        }
      }
    } catch {}
  }
  try {
    let specs = null;
    let refArray = [];
    let matchedTier = {};
    if (forceScan) {
      const res = await apiPost("/api/pc-specs/scan", {});
      if (res.ok && res.data.specs) {
        specs = res.data.specs;
        refArray = res.data.unreal_reference_array || [];
        matchedTier = res.data.matched_reference_tier || {};
        try { localStorage.setItem("modtool_cache_pc_specs", JSON.stringify(res.data)); } catch {}
      }
    } else {
      const res = await apiFetch("/api/pc-specs");
      const data = await res.json();
      specs = data.specs;
      refArray = data.unreal_reference_array || [];
      matchedTier = data.matched_reference_tier || {};
      try { localStorage.setItem("modtool_cache_pc_specs", JSON.stringify(data)); } catch {}
    }
    if (!specs) return;
    state.pcSpecsData = specs;

    const pcRefTbody = document.getElementById("pc-reference-array-tbody");
    if (pcRefTbody && Array.isArray(refArray)) {
      pcRefTbody.innerHTML = renderReferenceArrayTableHtml(refArray, matchedTier.tier_id || "tier_flagship_24gb_4090");
    }

    const cpu = specs.cpu || {};
    const gpu = specs.gpu || {};
    const ram = specs.ram || {};
    const disp = specs.display || {};
    const osInfo = specs.os || {};
    const tier = specs.tier || {};

    const formatBriefCpu = (raw) => {
      const s = String(raw || "").replace(/\(R\)|\(TM\)/gi, "").replace(/\s+/g, " ").trim();
      const intelMatch = s.match(/\b(i[3579]-\d+[A-Z]*|Ultra\s+\d+\s+\d+[A-Z]*)\b/i);
      if (intelMatch) return intelMatch[1];
      const amdMatch = s.match(/\bRyzen\s+\d+\s+(\d+[A-Z0-9]*)\b/i);
      if (amdMatch) return amdMatch[1];
      return s.split("@")[0].replace(/\b(Intel|Core|AMD|CPU|Processor)\b/gi, "").replace(/\s+/g, " ").trim() || "CPU";
    };
    const formatBriefGpu = (raw) => {
      const s = String(raw || "").replace(/\s+/g, " ").trim();
      const gpuMatch = s.match(/\b(?:RTX|GTX|RX|Arc)\s+([A-Za-z0-9-]+(?:\s+(?:Ti|SUPER|Super|XT|XTX|GRE))?)\b/i);
      if (gpuMatch) return gpuMatch[1];
      return s.replace(/\b(NVIDIA|GeForce|AMD|Radeon|Intel|Laptop GPU)\b/gi, "").replace(/\s+/g, " ").trim() || "GPU";
    };

    const tabBadge = document.getElementById("specs-tab-badge");
    if (tabBadge) {
      tabBadge.textContent = `${formatBriefCpu(cpu.name)} | ${formatBriefGpu(gpu.name)}`;
    }
    const scrapedAtBadge = document.getElementById("pc-scraped-at-badge");
    if (scrapedAtBadge) {
      scrapedAtBadge.textContent = `Scraped: ${specs.scraped_at || "Live"}`;
    }

    const setTxt = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    setTxt("pc-cpu-name", `${cpu.name || "Unknown"} (${cpu.architecture || "x64"})`);
    setTxt(
      "pc-cpu-cores",
      `${cpu.physical_cores || "?"} Physical Cores / ${cpu.logical_threads || "?"} Logical Threads` +
      (cpu.base_clock_ghz ? ` @ ${cpu.base_clock_ghz} GHz (${cpu.base_clock_mhz} MHz)` : "")
    );
    setTxt(
      "pc-gpu-name",
      `${gpu.name || "Unknown GPU"}` + (gpu.temperature_c != null ? ` (${gpu.temperature_c} °C)` : "")
    );
    setTxt(
      "pc-gpu-vram",
      `${gpu.vram_total_gb || 0} GB Dedicated (${gpu.vram_total_mb || 0} MB Total / ${gpu.vram_free_gb || 0} GB Free) | Driver ${gpu.driver_version || "N/A"}`
    );
    setTxt("pc-gpu-dx", gpu.directx_feature_level || "DirectX 12");
    setTxt(
      "pc-ram-info",
      `${ram.total_gb || 0} GB Total (${ram.available_gb || 0} GB Available / ${ram.used_gb || 0} GB Used — ${ram.load_percent || 0}% Load)`
    );
    setTxt(
      "pc-display-info",
      `${disp.resolution_label || "1920x1080"} @ ${disp.refresh_rate_hz || 60} Hz (${disp.aspect_ratio || "16:9"})`
    );
    setTxt(
      "pc-os-board",
      `${osInfo.product_name || "Windows"} ${osInfo.display_version || ""} (Build ${osInfo.build_number || "?"}) | Board: ${osInfo.motherboard || "PC"}`
    );
    setTxt("pc-tier-name", `${tier.name || "High Tier"} — [${tier.badge || ""}]`);
    setTxt(
      "pc-tier-rec",
      `Preset: ${tier.recommended_ue_preset_label || "Epic (3)"} | Target Output: ${tier.recommended_resolution || disp.resolution_label}`
    );

    const drivesTbody = document.getElementById("pc-drives-tbody");
    if (drivesTbody) {
      const drives = Array.isArray(specs.drives) ? specs.drives : [];
      drivesTbody.innerHTML = drives.map(d => `
        <tr>
          <td><strong>${d.drive}</strong></td>
          <td>${d.total_gb} GB</td>
          <td>${d.used_gb} GB</td>
          <td style="color:#34d399; font-weight:600;">${d.free_gb} GB Free</td>
          <td>${d.used_percent}%</td>
        </tr>
      `).join("");
    }

    const rawPre = document.getElementById("pc-specs-raw");
    if (rawPre) {
      rawPre.textContent = JSON.stringify(specs, null, 2);
    }

    if (forceScan && state.lastRenderedGame) {
      await rescanActiveGameMetadata();
    }
  } catch (err) {
    console.error("Failed to load PC specs:", err);
  } finally {
    if (btn && forceScan) {
      btn.disabled = false;
      btn.textContent = "Refresh PC Specs";
    }
  }
}



export function toggleModdabilityGuide() {
  const panel = document.getElementById("asset-moddability-guide-panel");
  if (!panel) return;
  const opening = panel.style.display === "none";
  panel.style.display = opening ? "block" : "none";
  if (opening && !document.getElementById("asset-moddability-guide-grid")?.children.length) {
    loadModdabilityGuide();
  }
}

export async function loadModdabilityGuide(triggeredKeys = []) {
  const grid = document.getElementById("asset-moddability-guide-grid");
  if (!grid) return;
  const res = await apiGet("/api/game/moddability-guide");
  if (!res.ok || !res.data) return;
  const guide = Array.isArray(res.data.guide) ? res.data.guide : [];
  renderModdabilityGuideGrid(guide, triggeredKeys);
}

export function renderModdabilityGuideGrid(guide, triggeredKeys = []) {
  const grid = document.getElementById("asset-moddability-guide-grid");
  if (!grid || !Array.isArray(guide)) return;
  const trigSet = new Set(triggeredKeys || []);
  const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  grid.innerHTML = guide.map(item => {
    const isTrig = trigSet.has(item.key);
    const sevCls = item.severity === "red" ? "no" : "warn";
    const cardBorder = isTrig ? "border: 1px solid #f59e0b; box-shadow: 0 0 0 1px rgba(245,158,11,0.35);" : "";
    const trigTag = isTrig ? '<span class="pill warn">Detected in Active Game</span>' : "";
    return `<div class="step-card ${item.severity === "red" ? "missing" : "pending"}" style="${cardBorder}">
      <div class="step-card-top">
        <span class="step-card-title">${esc(item.title)}</span>
        <span class="pill ${sevCls}">${esc((item.severity || "red").toUpperCase())}</span>
      </div>
      ${trigTag ? `<div style="margin:3px 0;">${trigTag}</div>` : ""}
      <div style="font-size:0.75rem; color:#e2e8f0; margin-top:4px;">${esc(item.summary)}</div>
      <div style="font-size:0.73rem; color:#fca5a5; margin-top:4px;"><strong>Why:</strong> ${esc(item.why_it_blocks)}</div>
      <div style="font-size:0.73rem; color:#86efac; margin-top:3px;"><strong>Alternative:</strong> ${esc(item.what_you_can_do_instead)}</div>
    </div>`;
  }).join("");
}

export async function runAssetModdabilityScan() {
  const btn = document.getElementById("btn-scan-assets");
  const pill = document.getElementById("asset-analyzer-summary-pill");
  const matrix = document.getElementById("asset-traffic-light-matrix");
  const rootPath = (document.getElementById("fp-game-dir")?.value || "").trim();
  if (btn) { btn.disabled = true; btn.textContent = "Scanning..."; }
  try {
    let r = null;
    try {
      r = await apiPost("/api/game/analyze-assets", {
        game_id: state.activeGameId || "",
        root_path: rootPath
      });
    } catch (err) {
      if (matrix) matrix.innerHTML = `<div class="step-card missing"><div class="step-card-title">Asset Scan Failed</div><div class="step-card-detail">${err?.message || "Could not analyze game directory."}</div></div>`;
      return;
    }
    state.lastAssetAudit = r;
    const s = r.summary || { green: 0, amber: 0, red: 0 };
    if (pill) {
      pill.className = s.green > 0 ? "pill yes" : (s.amber > 0 ? "pill warn" : "pill no");
      pill.textContent = `${r.engine} | ${s.green} Green · ${s.amber} Amber · ${s.red} Red`;
    }
    const mbText = document.getElementById("mb-asset-audit-text");
    if (mbText) {
      mbText.innerHTML = `Asset Audit (<strong>${r.title}</strong> · <code>${r.engine}</code>): <span style="color:#4ade80;">${s.green} Green</span> · <span style="color:#fbbf24;">${s.amber} Amber</span> · <span style="color:#f87171;">${s.red} Red</span>`;
    }
    const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const colorMap = {
      green: { bg: "#16a34a", cls: "ok", label: "GREEN" },
      amber: { bg: "#d97706", cls: "pending", label: "AMBER" },
      red: { bg: "#dc2626", cls: "missing", label: "RED" }
    };
    const layers = Array.isArray(r.layers) ? r.layers : [];
    const compatTools = (Array.isArray(r.available_tools) ? r.available_tools : []).filter(t => t && t.engine_compatible === true);
    if (matrix) {
      const layerCards = layers.map(l => {
        const cm = colorMap[l.status] || colorMap.amber;
        const sampleTxt = Array.isArray(l.detected_items) && l.detected_items.length
          ? l.detected_items.slice(0, 5).map(x => `<code>${esc(x)}</code>`).join(", ")
          : "None";
        const reqTools = Array.isArray(l.required_tools) ? l.required_tools.join(", ") : "";
        return `<div class="step-card ${cm.cls}">
          <div class="step-card-top">
            <span class="step-card-title">${esc(l.layer_name)}</span>
            <span class="pill" style="background:${cm.bg}; color:#fff; border-color:${cm.bg};">${cm.label} (${l.file_count || 0})</span>
          </div>
          <div style="font-size:0.74rem; color:#93c5fd; margin-top:3px;"><strong>Tools:</strong> ${esc(reqTools)} — ${esc(l.tool_status)}</div>
          <div style="font-size:0.74rem; color:#e2e8f0; margin-top:3px;">${esc(l.what_can_change)}</div>
          <div class="step-card-detail" style="margin-top:4px;">Samples: ${sampleTxt}</div>
        </div>`;
      }).join("");
      const toolCards = compatTools.map(t => {
        const badgeCls = t.installed_in_game ? "yes" : (t.host_available ? "warn" : "na");
        const badgeLabel = t.installed_in_game
          ? "Ready in Game"
          : (t.host_available ? "Host Tool Ready (Not Installed in Game)" : "Recommended (Not Installed)");
        const cardCls = t.installed_in_game ? "ok" : (t.host_available ? "pending" : "na");
        return `<div class="step-card ${cardCls}">
          <div class="step-card-top">
            <span class="step-card-title">${esc(t.name)}</span>
            <span class="pill ${badgeCls}">${badgeLabel}</span>
          </div>
          <div style="font-size:0.74rem; color:#cbd5e1; margin-top:3px;">${esc(t.detail || t.install_hint || "")}</div>
          ${t.install_hint && !t.installed_in_game ? `<div class="step-card-detail" style="margin-top:4px;">Hint: ${esc(t.install_hint)}</div>` : ""}
        </div>`;
      }).join("");
      matrix.innerHTML = layerCards + toolCards;
    }
    if (r.guide) {
      renderModdabilityGuideGrid(Object.values(r.guide), r.triggered_blockers || []);
    }
    loadLogs();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Scan Game Assets"; }
  }
}

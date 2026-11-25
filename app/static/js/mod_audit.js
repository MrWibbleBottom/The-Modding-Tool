import { apiFetch, apiGet, apiPost, runTrackedAction, state } from "./api.js";
import { escHtml, resolveBtn } from "./state.js";

let _lastInstalledConflicts = [];
let _lastAuditReport = null;
let _droppedFiles = [];
let _dropzoneWired = false;

function _setStatusBanner(message, tone = "info") {
  const el = document.getElementById("mv-status-banner");
  if (!el) return;
  if (!message) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.style.display = "block";
  const palette = {
    ok: "border:1px solid #2e6b45;background:#102519;color:#9ee6b8;",
    warn: "border:1px solid #7a5d1c;background:#271e0b;color:#f5d07a;",
    error: "border:1px solid #7c2b34;background:#2a1115;color:#f59ea7;",
    info: "border:1px solid #29466a;background:#0e1b2d;color:#a8ccf5;",
  };
  el.setAttribute("style", `${palette[tone] || palette.info}padding:8px 10px;border-radius:6px;font-size:12px;margin-bottom:10px;`);
  el.textContent = message;
}

function _formatStorageModeLabel(mode) {
  const low = String(mode || "zip").toLowerCase();
  if (low === "unpacked") return "Unpacked";
  if (low === "linked") return "Linked";
  return "Zip";
}

function _renderConflictsHtml(conflicts) {
  if (!Array.isArray(conflicts) || conflicts.length === 0) {
    return `<span class="text-muted">No cross-mod Unreal <code>.pak</code> asset collisions detected.</span>`;
  }
  return conflicts.map((c) => {
    const assetName = escHtml(c.asset_name || c.asset_path || "Unknown asset");
    const assetPath = escHtml(c.asset_path || "");
    const detailList = Array.isArray(c.details) ? c.details : [];
    const mods = detailList.length
      ? detailList.map((m) => `${escHtml(m.mod_name || m.mod_id || "unknown")}${m.source ? ` (${escHtml(m.source)})` : ""}`).join(" vs ")
      : Array.isArray(c.conflicting_mods)
        ? c.conflicting_mods.map((m) => (typeof m === "string" ? escHtml(m) : escHtml(m?.mod_name || m?.mod_id || "unknown"))).join(" vs ")
        : escHtml(typeof c.details === "string" ? c.details : "");
    return `
      <div style="border:1px solid #8a5b1e;background:#261a0a;border-radius:6px;padding:8px 10px;margin-bottom:6px;">
        <div style="font-weight:600;color:#f7cf7c;font-size:12px;">Collision: <code>${assetName}</code></div>
        ${assetPath ? `<div class="mono" style="font-size:11px;color:#d8c49a;word-break:break-all;margin-top:2px;">${assetPath}</div>` : ""}
        <div style="font-size:12px;color:#f0dec0;margin-top:3px;">Conflicting mods: ${mods}</div>
      </div>
    `;
  }).join("");
}

function _mergeConflicts(reportConflicts, installedConflicts) {
  const seen = new Set();
  const out = [];
  for (const list of [reportConflicts, installedConflicts]) {
    if (!Array.isArray(list)) continue;
    for (const item of list) {
      const key = String(item?.asset_path || item?.asset_name || "").toLowerCase();
      if (!key || seen.has(key)) continue;
      seen.add(key);
      out.push(item);
    }
  }
  return out;
}

export function renderInstalledPakConflicts(conflicts = []) {
  _lastInstalledConflicts = Array.isArray(conflicts) ? conflicts : [];
  const box = document.getElementById("mv-conflicts-box");
  if (box) {
    if (_lastInstalledConflicts.length === 0) {
      box.style.display = "none";
      box.innerHTML = "";
    } else {
      box.style.display = "block";
      box.innerHTML = `
        <div style="font-weight:600;color:#f7cf7c;font-size:12px;margin-bottom:6px;">
          Cross-Mod Unreal Asset Conflicts Detected (${_lastInstalledConflicts.length})
        </div>
        ${_renderConflictsHtml(_lastInstalledConflicts)}
      `;
    }
  }
  const auditConflictsEl = document.getElementById("mv-audit-conflicts");
  if (auditConflictsEl && _lastAuditReport) {
    const merged = _mergeConflicts(_lastAuditReport.asset_conflicts, _lastInstalledConflicts);
    auditConflictsEl.innerHTML = _renderConflictsHtml(merged);
  }
}

function _wireDropzoneOnce() {
  if (_dropzoneWired) return;
  const dz = document.getElementById("mv-dropzone");
  if (!dz) return;
  _dropzoneWired = true;
  dz.addEventListener("dragover", (e) => {
    e.preventDefault();
    dz.style.borderColor = "#60a5fa";
  });
  dz.addEventListener("dragleave", () => {
    dz.style.borderColor = "#385170";
  });
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.style.borderColor = "#385170";
    const files = Array.from(e.dataTransfer?.files || []);
    if (files.length) {
      _droppedFiles = files;
      _setStatusBanner(`Queued ${files.length} dropped file(s). Click 'Upload & Audit' to ingest.`, "info");
    }
  });
}

function _renderEditableEndpointsPanel(report) {
  const el = document.getElementById("mv-audit-editable-endpoints");
  if (!el) return;
  const cands = Array.isArray(report.candidate_endpoints) ? report.candidate_endpoints : [];
  if (cands.length === 0) {
    el.innerHTML = `<span class="text-muted">No exposed config settings, hooks, or mod asset endpoints discovered in this mod.</span>`;
    return;
  }

  const modIdAttr = escHtml(report.mod_id || "").replace(/'/g, "");
  el.innerHTML = cands.map((ep, idx) => {
    const label = escHtml(ep.name || ep.symbol || "Endpoint");
    const sym = escHtml(ep.symbol || "");
    const cat = escHtml(ep.category || "uncategorized");
    const filePath = escHtml(ep.file_path || "");
    const curVal = escHtml(ep.current_value ?? ep.default_value ?? "");
    const safeSymAttr = sym.replace(/'/g, "&#39;");

    if (ep.editable && filePath) {
      return `
        <div class="mv-ep-row" data-idx="${idx}" data-file="${filePath}" data-sym="${safeSymAttr}" style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #1f2b3a;flex-wrap:wrap;">
          <div style="min-width:220px;flex:1;">
            <div style="font-size:12px;font-weight:600;">${label} <span class="badge badge-dim">${cat}</span></div>
            <div class="mono text-muted" style="font-size:11px;">${filePath} &rarr; ${sym}</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            <input type="text" class="mv-ep-input" value="${curVal}" style="width:150px;font-size:12px;padding:3px 6px;" />
            <button type="button" class="btn-outline" style="padding:3px 8px;font-size:11px;" onclick="saveStoredModEndpointValue('${modIdAttr}', ${idx}, false)">Save</button>
            <button type="button" class="success" style="padding:3px 8px;font-size:11px;" onclick="saveStoredModEndpointValue('${modIdAttr}', ${idx}, true)">Save &amp; Deploy</button>
          </div>
        </div>
      `;
    }

    return `
      <div class="mv-ep-row" data-idx="${idx}" data-sym="${safeSymAttr}" style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #1f2b3a;flex-wrap:wrap;">
        <div style="min-width:220px;flex:1;">
          <div style="font-size:12px;font-weight:600;">${label} <span class="badge badge-dim">${cat}</span> <span class="badge">${escHtml(ep.delivery_type || "Mod File")}</span></div>
          <div class="mono text-muted" style="font-size:11px;word-break:break-all;">${sym}${curVal ? ` (${curVal})` : ""}</div>
        </div>
        <button type="button" class="btn-outline" style="padding:3px 8px;font-size:11px;" onclick="promoteSingleStoredModEndpoint('${modIdAttr}', ${idx})">Promote to Modding Tab</button>
      </div>
    `;
  }).join("");
}

export function renderModAuditBlueprint(report, installedConflicts = null) {
  if (!report || typeof report !== "object") return;
  _lastAuditReport = report;
  if (Array.isArray(installedConflicts)) {
    _lastInstalledConflicts = installedConflicts;
  }

  const panel = document.getElementById("mod-audit-blueprint-panel");
  if (panel) panel.style.display = "block";

  const titleEl = document.getElementById("mv-audit-title");
  if (titleEl) {
    const nexusPart = report.nexus_id ? ` [Nexus #${report.nexus_id}]` : "";
    const verPart = report.version ? ` v${report.version}` : "";
    titleEl.textContent = `${report.mod_name || report.archive_name || report.mod_id || "Stored Mod"}${verPart}${nexusPart}`;
  }

  const metaEl = document.getElementById("mv-audit-meta");
  if (metaEl) {
    const mounts = Array.isArray(report.mount_points) && report.mount_points.length
      ? `Mount Point: ${report.mount_points.join(", ")}`
      : "No .pak mount point";
    const modeLabel = _formatStorageModeLabel(report.storage_mode);
    const epCount = (report.candidate_endpoints || []).length;
    const streamedCount = Array.isArray(report.archived_entries) ? report.archived_entries.length : 0;
    const streamedPart = streamedCount ? ` | Streamed in Archive: ${streamedCount}` : "";
    metaEl.textContent =
      `Storage: ${modeLabel} | Format: ${report.format || "unknown"} | ${mounts} | Active Assets: ${(report.active_assets || []).length} | Exposed Endpoints: ${epCount}${streamedPart}`;
  }

  const descEl = document.getElementById("mv-audit-description");
  if (descEl) {
    const extraPath = report.linked_path ? `Linked path: ${report.linked_path}` : "";
    descEl.textContent = report.description || extraPath || "";
    descEl.style.display = descEl.textContent ? "block" : "none";
  }

  _renderEditableEndpointsPanel(report);

  const assetsEl = document.getElementById("mv-audit-unreal-assets");
  if (assetsEl) {
    const activeAssets = Array.isArray(report.active_assets) ? report.active_assets : [];
    if (activeAssets.length === 0) {
      assetsEl.innerHTML = `<span class="text-muted">No active Unreal <code>.uasset</code>/<code>.umap</code>/<code>.ini</code> entries found.</span>`;
    } else {
      assetsEl.innerHTML = activeAssets.map((a) => {
        const cat = escHtml((typeof a === "object" && a?.category) || "Game Systems");
        const path = escHtml(typeof a === "string" ? a : (a?.path || a?.asset_path || a?.asset_name || ""));
        const ext = escHtml((typeof a === "object" && (a?.ext || a?.extension)) || "");
        return `
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid #1f2b3a;font-size:12px;">
            <span class="mono" style="word-break:break-all;">${path}</span>
            <span style="white-space:nowrap;">
              ${ext ? `<span class="badge badge-dim">${ext}</span> ` : ""}
              <span class="badge">${cat}</span>
            </span>
          </div>
        `;
      }).join("");
    }
  }

  const upropsEl = document.getElementById("mv-audit-uprops");
  if (upropsEl) {
    const symbols = Array.isArray(report.uproperty_symbols) ? report.uproperty_symbols : [];
    const bakDiffs = Array.isArray(report.bak_diffs) ? report.bak_diffs : [];
    const pillsHtml = symbols.length
      ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;">${symbols.slice(0, 60).map((s) => `<span class="badge badge-dim mono">${escHtml(s)}</span>`).join("")}</div>`
      : `<div class="text-muted" style="font-size:12px;margin-bottom:6px;">No UProperty / Component FName tokens extracted.</div>`;
    const diffsHtml = bakDiffs.map((diff) => {
      const added = (diff.added_symbols || []).map((s) => `<span class="badge" style="background:#173623;color:#96eab4;">+${escHtml(s)}</span>`).join(" ");
      const removed = (diff.removed_symbols || []).map((s) => `<span class="badge" style="background:#3a181e;color:#f3a1ab;">-${escHtml(s)}</span>`).join(" ");
      const targetLabel = escHtml(diff.asset || diff.asset_path || diff.backup_file || "");
      return `
        <div style="border-top:1px solid #1f2b3a;padding-top:6px;margin-top:6px;font-size:12px;">
          <div style="font-weight:600;"><code>.bak</code> Diff vs <code>${targetLabel}</code></div>
          <div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px;">${added || '<span class="text-muted">No added FNames</span>'} ${removed}</div>
        </div>
      `;
    }).join("");
    upropsEl.innerHTML = `${pillsHtml}${diffsHtml}`;
  }

  const backupsEl = document.getElementById("mv-audit-backups");
  if (backupsEl) {
    const backups = Array.isArray(report.packed_backups) ? report.packed_backups : [];
    const notes = Array.isArray(report.hygiene_notes) ? report.hygiene_notes : [];
    if (backups.length === 0 && notes.length === 0) {
      backupsEl.innerHTML = `<span class="text-muted">No stray author backup files (<code>.bak</code>, <code>.orig</code>, <code>.mod*</code>) packed.</span>`;
    } else {
      const notesHtml = notes.map((n) => `<div style="color:#f5d07a;font-size:12px;margin-bottom:6px;">${escHtml(n)}</div>`).join("");
      const listHtml = backups.map((b) => {
        const bText = typeof b === "string" ? b : (b?.path || b?.asset_path || b?.asset_name || "");
        return `<div class="mono" style="font-size:11px;color:#d6c096;padding:2px 0;">${escHtml(bText)}</div>`;
      }).join("");
      backupsEl.innerHTML = `${notesHtml}${listHtml}`;
    }
  }

  const conflictsEl = document.getElementById("mv-audit-conflicts");
  if (conflictsEl) {
    const merged = _mergeConflicts(report.asset_conflicts, _lastInstalledConflicts);
    conflictsEl.innerHTML = _renderConflictsHtml(merged);
  }

  const mdEl = document.getElementById("mv-audit-blueprint-md");
  if (mdEl) {
    mdEl.textContent = report.blueprint_markdown || "(No blueprint markdown generated.)";
  }
}

export async function loadModVaultSection(slug = state.activeGameId, installedConflicts = null) {
  _wireDropzoneOnce();
  const targetSlug = slug || state.activeGameId;
  if (!targetSlug) return;
  if (Array.isArray(installedConflicts)) {
    renderInstalledPakConflicts(installedConflicts);
  }
  const tbody = document.getElementById("mv-stored-mods-tbody");
  if (!tbody) return;

  try {
    const res = await apiGet(`/api/games/${encodeURIComponent(targetSlug)}/stored-mods`);
    if (!res.ok) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-muted">Could not load Mod Vault (${escHtml(res.data?.detail || res.status)}).</td></tr>`;
      return;
    }
    const items = Array.isArray(res.data?.stored_mods) ? res.data.stored_mods : [];
    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-muted">No mods in Mod Vault yet. Upload archives, unpacked folders, or link a local path above.</td></tr>`;
      return;
    }

    tbody.innerHTML = items.map((item) => {
      const modId = String(item.mod_id || "");
      const safeModId = escHtml(modId).replace(/'/g, "");
      const title = escHtml(item.mod_name || item.archive_name || item.filename || modId);
      const nexusBadge = item.nexus_id ? `<span class="badge">Nexus #${escHtml(item.nexus_id)}</span>` : "";
      const verBadge = item.version ? `<span class="badge badge-dim">v${escHtml(item.version)}</span>` : "";
      const modeBadge = `<span class="badge">${escHtml(_formatStorageModeLabel(item.storage_mode))}</span>`;
      const summary = item.summary || item;
      const conflictCount = Number(summary.asset_conflicts_count || 0);
      const epCount = Number(summary.endpoints_count || summary.candidate_endpoints_count || 0);
      const details = [
        epCount ? `${epCount} endpoint(s)` : "0 endpoints",
        summary.active_assets_count ? `${summary.active_assets_count} UE assets` : null,
        summary.packed_backups_count ? `${summary.packed_backups_count} .bak` : null,
        summary.uproperty_symbols_count ? `${summary.uproperty_symbols_count} FNames` : null,
        summary.hooks_count ? `${summary.hooks_count} hook(s)` : null,
      ].filter(Boolean).join(" · ");
      const conflictPill = conflictCount
        ? ` <span class="pill warn">${conflictCount} .pak conflict(s)</span>`
        : "";
      const subLine = item.description
        ? `<div class="text-muted" style="font-size:11px;max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml(item.description)}</div>`
        : (item.linked_path ? `<div class="mono text-muted" style="font-size:11px;">${escHtml(item.linked_path)}</div>` : "");

      return `
        <tr>
          <td>
            <div><strong>${title}</strong> ${verBadge} ${nexusBadge}</div>
            ${subLine}
          </td>
          <td>${modeBadge}</td>
          <td style="font-size:12px;">${escHtml(details)}${conflictPill}</td>
          <td class="text-muted" style="font-size:11px;">${escHtml(item.uploaded_at || "").slice(0, 19).replace("T", " ")}</td>
          <td style="white-space:nowrap;">
            <button type="button" class="success" style="padding:3px 8px;font-size:11px;" onclick="deployStoredMod('${safeModId}')">Deploy to Game</button>
            <button type="button" class="btn-outline" style="padding:3px 8px;font-size:11px;" onclick="viewStoredModAudit('${safeModId}')">Inspect Endpoints</button>
            <button type="button" class="btn-outline" style="padding:3px 8px;font-size:11px;" onclick="promoteStoredModEndpoints('${safeModId}')">Promote</button>
            <button type="button" class="btn-danger" style="padding:3px 8px;font-size:11px;" onclick="deleteStoredMod('${safeModId}')">Delete</button>
          </td>
        </tr>
      `;
    }).join("");
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-muted">Failed to load Mod Vault: ${escHtml(err.message || err)}</td></tr>`;
  }
}

export async function uploadModToVault() {
  const slug = state.activeGameId;
  const fileInput = document.getElementById("mv-upload-file");
  const folderInput = document.getElementById("mv-upload-folder");
  const selectedFiles = [
    ...Array.from(fileInput?.files || []),
    ...Array.from(folderInput?.files || []),
    ..._droppedFiles,
  ];
  if (!slug || selectedFiles.length === 0) {
    _setStatusBanner("Select one or more mod archives, unpacked files, or a folder first.", "warn");
    return;
  }

  const btn = resolveBtn("btn-mv-upload");
  const bundleVal = document.getElementById("mv-upload-bundle")?.value || "";
  const sourceVal = document.getElementById("mv-upload-source")?.value || "";
  const notesVal = document.getElementById("mv-upload-notes")?.value || "";

  await runTrackedAction(
    btn,
    async () => {
      const fd = new FormData();
      for (const f of selectedFiles) {
        const rel = f.webkitRelativePath || f.name;
        fd.append("files", f, rel);
      }
      if (bundleVal.trim()) fd.append("bundle_name", bundleVal.trim());
      if (sourceVal.trim()) fd.append("source_label", sourceVal.trim());
      if (notesVal.trim()) fd.append("notes", notesVal.trim());

      const res = await apiFetch(`/api/games/${encodeURIComponent(slug)}/stored-mods/upload`, {
        method: "POST",
        body: fd,
      });
      const text = await res.text();
      let data = {};
      try { data = JSON.parse(text); } catch { data = { raw: text }; }
      if (!res.ok) {
        throw new Error(data.detail || text || `HTTP ${res.status}`);
      }
      if (fileInput) fileInput.value = "";
      if (folderInput) folderInput.value = "";
      _droppedFiles = [];
      const count = data.count || (Array.isArray(data.uploaded) ? data.uploaded.length : 1);
      _setStatusBanner(`Ingested and audited ${count} mod package(s) into Mod Vault.`, "ok");
      const firstReport = data.audit_report || data.uploaded?.[0]?.audit_report;
      if (firstReport) {
        renderModAuditBlueprint(firstReport, _lastInstalledConflicts);
      }
      await loadModVaultSection(slug, _lastInstalledConflicts);
      return data;
    },
    {
      pendingText: `Streaming & auditing ${selectedFiles.length} file(s)...`,
      successText: (d) => `Ingested ${d?.count || 1} mod package(s)`,
    },
  ).catch((err) => {
    _setStatusBanner(`Upload failed: ${err?.message || err}`, "error");
  });
}

export async function browseLocalModPath() {
  const inp = document.getElementById("mv-link-local-path");
  try {
    const res = await apiPost("/api/games/browse", {
      initial_dir: inp ? inp.value : "",
      title: "Select Local Unpacked Mod Folder",
    });
    if (res && res.status === "ok" && res.path && inp) {
      inp.value = res.path;
      _setStatusBanner(`Selected local folder '${res.path}'. Click 'Link Local File / Folder' to ingest without copying.`, "info");
    }
  } catch (err) {
    _setStatusBanner(`Browse failed: ${err?.message || err}`, "error");
  }
}

export async function linkLocalModToVault() {
  const slug = state.activeGameId;
  const localPath = (document.getElementById("mv-link-local-path")?.value || "").trim();
  const bundleName = (document.getElementById("mv-link-bundle-name")?.value || "").trim();
  if (!slug || !localPath) {
    _setStatusBanner("Enter a local unpacked folder, .zip, or .pak path first.", "warn");
    return;
  }
  const btn = resolveBtn("btn-mv-link-local");
  await runTrackedAction(
    btn,
    async () => {
      const res = await apiPost(`/api/games/${encodeURIComponent(slug)}/stored-mods/link-local`, {
        local_path: localPath,
        bundle_name: bundleName,
        source_label: "Local Path",
      });
      _setStatusBanner(`Linked and audited '${res.summary?.mod_name || localPath}'.`, "ok");
      if (res.audit_report) {
        renderModAuditBlueprint(res.audit_report, _lastInstalledConflicts);
      }
      await loadModVaultSection(slug, _lastInstalledConflicts);
      return res;
    },
    {
      pendingText: "Linking & auditing local mod path...",
      successText: "Linked local mod into Mod Vault",
    },
  ).catch((err) => {
    _setStatusBanner(`Link failed: ${err?.message || err}`, "error");
  });
}

export async function viewStoredModAudit(modId) {
  const slug = state.activeGameId;
  if (!slug || !modId) return;
  try {
    const res = await apiGet(`/api/games/${encodeURIComponent(slug)}/stored-mods/${encodeURIComponent(modId)}/audit`);
    if (!res.ok) {
      _setStatusBanner(`Failed to open audit: ${res.data?.detail || res.status}`, "error");
      return;
    }
    if (res.data?.audit_report) {
      renderModAuditBlueprint(res.data.audit_report, _lastInstalledConflicts);
    }
  } catch (err) {
    _setStatusBanner(`Error loading audit: ${err.message || err}`, "error");
  }
}

export async function saveStoredModEndpointValue(modId, idx, andDeploy = false) {
  const slug = state.activeGameId;
  if (!slug || !modId || !_lastAuditReport) return;
  const ep = (_lastAuditReport.candidate_endpoints || [])[idx];
  const container = document.getElementById("mv-audit-editable-endpoints");
  const row = container?.querySelector(`.mv-ep-row[data-idx="${idx}"]`);
  const valInput = row?.querySelector("input.mv-ep-input");
  if (!ep || !ep.file_path || !valInput) return;

  try {
    const res = await apiPost(`/api/games/${encodeURIComponent(slug)}/stored-mods/${encodeURIComponent(modId)}/edit-value`, {
      file_path: ep.file_path,
      symbol: ep.symbol,
      new_value: valInput.value,
    });
    if (res.audit_report) {
      renderModAuditBlueprint(res.audit_report, _lastInstalledConflicts);
    }
    if (andDeploy) {
      const dep = await apiPost(`/api/games/${encodeURIComponent(slug)}/stored-mods/${encodeURIComponent(modId)}/deploy`, {});
      _setStatusBanner(
        `Saved '${ep.symbol}' = '${valInput.value}' and deployed ${(dep.deployed_files || []).length} file(s) (snapshot ${dep.snapshot_id}).`,
        "ok",
      );
    } else {
      _setStatusBanner(`Saved '${ep.symbol}' = '${valInput.value}' in '${ep.file_path}'.`, "ok");
    }
  } catch (err) {
    _setStatusBanner(`Failed to update setting: ${err?.message || err}`, "error");
  }
}

export async function deployStoredMod(modId) {
  const slug = state.activeGameId;
  if (!slug || !modId) return;
  try {
    const res = await apiPost(`/api/games/${encodeURIComponent(slug)}/stored-mods/${encodeURIComponent(modId)}/deploy`, {});
    _setStatusBanner(
      `Snapshot ${res.snapshot_id} created and deployed ${(res.deployed_files || []).length} file(s) to game directory.`,
      "ok",
    );
    await loadModVaultSection(slug, _lastInstalledConflicts);
  } catch (err) {
    _setStatusBanner(`Deploy failed: ${err?.message || err}`, "error");
  }
}

export function deploySelectedStoredMod() {
  if (_lastAuditReport?.mod_id) {
    return deployStoredMod(_lastAuditReport.mod_id);
  }
}

export async function promoteSingleStoredModEndpoint(modId, idx) {
  const slug = state.activeGameId;
  const ep = (_lastAuditReport?.candidate_endpoints || [])[idx];
  if (!slug || !modId || !ep?.symbol) return;
  try {
    const res = await apiPost(
      `/api/games/${encodeURIComponent(slug)}/stored-mods/${encodeURIComponent(modId)}/promote-endpoints`,
      { symbols: [ep.symbol] },
    );
    _setStatusBanner(`Promoted '${ep.symbol}' into the Modding tab catalog.`, "ok");
    if (res.report) {
      await loadModVaultSection(slug, _lastInstalledConflicts);
    }
  } catch (err) {
    _setStatusBanner(`Promote failed: ${err?.message || err}`, "error");
  }
}

export async function promoteSelectedStoredMod() {
  if (_lastAuditReport?.mod_id) {
    return promoteStoredModEndpoints(_lastAuditReport.mod_id);
  }
}

export async function promoteStoredModEndpoints(modId) {
  const slug = state.activeGameId;
  if (!slug || !modId) return;
  try {
    const res = await apiPost(
      `/api/games/${encodeURIComponent(slug)}/stored-mods/${encodeURIComponent(modId)}/promote-endpoints`,
      {},
    );
    _setStatusBanner(`Promoted ${res.promoted_count ?? 0} endpoint(s) into the Modding tab endpoint catalog.`, "ok");
    if (res.report) {
      renderModAuditBlueprint(res.report, _lastInstalledConflicts);
    }
    await loadModVaultSection(slug, _lastInstalledConflicts);
  } catch (err) {
    _setStatusBanner(`Promote failed: ${err?.message || err}`, "error");
  }
}

export async function deleteStoredMod(modId) {
  const slug = state.activeGameId;
  if (!slug || !modId) return;
  try {
    const res = await apiFetch(
      `/api/games/${encodeURIComponent(slug)}/stored-mods/${encodeURIComponent(modId)}`,
      { method: "DELETE" },
    );
    if (!res.ok) {
      const txt = await res.text();
      _setStatusBanner(`Delete failed: ${txt || res.status}`, "error");
      return;
    }
    if (_lastAuditReport?.mod_id === modId) {
      _lastAuditReport = null;
      const panel = document.getElementById("mod-audit-blueprint-panel");
      if (panel) panel.style.display = "none";
    }
    _setStatusBanner("Removed mod from Mod Vault.", "info");
    await loadModVaultSection(slug, _lastInstalledConflicts);
  } catch (err) {
    _setStatusBanner(`Delete error: ${err.message || err}`, "error");
  }
}

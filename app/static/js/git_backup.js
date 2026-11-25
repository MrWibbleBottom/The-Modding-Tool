import { apiFetch, apiGet, apiPost, runTrackedAction } from "./api.js";
import { escHtml, resolveBtn, state } from "./state.js";
import { loadAppVersion, loadLogs } from "./app_mgmt.js";

let APP_BACKUPS_LIST = [];
let APP_BACKUPS_EXPANDED = false;
const APP_BACKUP_ROW_EXPANDED = {};
let _pendingGitPushBranch = "";

export function getLastBackupsDir() {
  return state.lastBackupsDir || "";
}

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
        <td><code>${escHtml(v.version_id)}</code></td>
        <td><span class="pill ${v.kind === "baseline" ? "yes" : "warn"}">${escHtml(v.kind)}</span></td>
        <td>${escHtml(v.created_at)}</td>
        <td>${escHtml(chg)}</td>
        <td><div style="white-space:${isExpanded ? "pre-wrap" : "normal"}; font-size:0.78rem;">${escHtml(previewText)}${expandBtn}</div></td>
        <td><button class="secondary btn-sm" onclick="restoreAppBackupVersion('${escHtml(v.version_id)}')">Restore</button></td>
      </tr>
    `;
  }).join("");
}

export async function loadAppBackups() {
  const tbody = document.getElementById("am-backups-tbody");
  try {
    const res = await apiFetch("/api/app/backups");
    const data = await res.json();
    state.lastBackupsDir = data.backups_dir || "";
    APP_BACKUPS_LIST = data.versions || [];
    renderAppBackupsTable();
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="6">Failed to load backups: ${escHtml(err.message || err)}</td></tr>`;
  }
}

export async function saveAppSnapshot(forceBaseline) {
  const btn = resolveBtn("");
  const reason = document.getElementById("am-backup-reason")?.value || "Manual save from App Management";
  const logEl = document.getElementById("am-backup-log");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const data = await apiPost("/api/app/backup/save", { reason, force_baseline: Boolean(forceBaseline) });
        if (logEl) logEl.textContent = data.summary || "Snapshot saved.";
        await loadAppBackups();
        loadLogs();
        return data;
      },
      {
        pendingText: forceBaseline ? "Saving full baseline snapshot..." : "Saving incremental app snapshot...",
        successText: (d) => d?.summary || "Snapshot saved",
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Save failed: ${err?.message || err}`;
  }
}

export async function restoreAppBackupVersion(versionId) {
  const btn = resolveBtn("");
  if (!confirm(`Restore the entire project to ${versionId}? Any unsaved changes since ${versionId} will be overwritten.`)) {
    return;
  }
  const logEl = document.getElementById("am-backup-log");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const data = await apiPost("/api/app/backup/restore", { version_id: versionId });
        if (logEl) logEl.textContent = `${data.summary}. Reload the page if UI files changed.`;
        await loadAppBackups();
        loadLogs();
        return data;
      },
      {
        pendingText: `Restoring project snapshot ${versionId}...`,
        successText: (d) => d?.summary || `Restored snapshot ${versionId}`,
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Restore failed: ${err?.message || err}`;
  }
}

function _populatePushBranches(branches, preferredBranch = "") {
  const sel = document.getElementById("gh-push-branch");
  if (!sel) return;
  const previous = sel.value || "";
  const cfgBranch = (document.getElementById("gh-branch")?.value || "main").trim() || "main";
  const ordered = [];
  const seen = new Set();
  for (const item of [...(Array.isArray(branches) ? branches : []), preferredBranch, previous, cfgBranch, "main"]) {
    const b = String(item || "").trim();
    if (b && b !== "HEAD" && !seen.has(b)) {
      seen.add(b);
      ordered.push(b);
    }
  }
  sel.innerHTML = "";
  for (const b of ordered) {
    const opt = document.createElement("option");
    opt.value = b;
    opt.textContent = b;
    sel.appendChild(opt);
  }
  const target = (preferredBranch && seen.has(preferredBranch))
    ? preferredBranch
    : (previous && seen.has(previous))
      ? previous
      : (seen.has(cfgBranch) ? cfgBranch : ordered[0] || "main");
  sel.value = target;
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
      _populatePushBranches([d.branch || "main"], d.branch || "main");
      if (tokEl) tokEl.value = d.token_set ? (d.token_masked || "******") : "";
      state.appInstalledShortSha = d.installed_short_sha || "";
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
  const btn = resolveBtn("");
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
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/app/repo-config", {
          owner: ownerVal,
          repo: repoVal,
          branch: (document.getElementById("gh-branch")?.value || "main").trim(),
          token: (document.getElementById("gh-token")?.value || "").trim(),
        });
        const data = res?.data ?? res;
        if (logEl) logEl.textContent = "Repository settings saved.";
        await loadGitHubRepoConfig();
        loadLogs();
        return data;
      },
      {
        pendingText: "Saving GitHub repository configuration...",
        successText: "GitHub repository settings saved",
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Save failed: ${err?.message || err}`;
  }
}

export async function checkGitHubUpdates() {
  const btn = resolveBtn("btn-gh-check");
  const logEl = document.getElementById("gh-sync-log");
  const remEl = document.getElementById("gh-remote-latest");
  const badgeEl = document.getElementById("gh-sync-badge");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/app/check-update", {});
        const d = res?.data ?? res ?? {};
        if (d.status === "ok") {
          if (remEl) remEl.textContent = `${d.short_sha} — ${d.commit_message || ""}`;
          if (badgeEl) {
            badgeEl.className = d.update_available ? "pill warn" : "pill yes";
            badgeEl.textContent = d.update_available ? "Update Available" : "Up to Date";
          }
          if (logEl) logEl.textContent = JSON.stringify(d, null, 2);
          return d;
        }
        if (badgeEl) { badgeEl.className = "pill no"; badgeEl.textContent = d.status || "Error"; }
        if (logEl) logEl.textContent = d.message || "Update check failed.";
        throw new Error(d.message || "Update check failed.");
      },
      {
        pendingText: "Checking GitHub for updates...",
        successText: (d) => d?.update_available ? `Update available (${d.short_sha})` : "Up to date with GitHub",
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = err?.message || "Update check failed.";
  }
}

export async function pullGitHubSync() {
  const btn = resolveBtn("btn-gh-sync");
  if (!confirm("Pull latest files from GitHub? A pre-sync snapshot will be saved first.")) return;
  const logEl = document.getElementById("gh-sync-log");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/app/sync", {});
        const data = res?.data ?? res;
        if (logEl) logEl.textContent = `Synced ${data.extracted_count} file(s). Snapshot: ${data.snapshot}. Archive SHA-256: ${data.archive_sha256}`;
        await loadGitHubRepoConfig();
        loadLogs();
        return data;
      },
      {
        pendingText: "Pulling latest release archive from GitHub...",
        successText: (d) => `Synced ${d?.extracted_count ?? 0} file(s) from GitHub`,
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Sync failed: ${err?.message || err}`;
  }
}

export async function rollbackToSnapshot() {
  const btn = resolveBtn("");
  const sel = document.getElementById("gh-snapshots-select");
  const snap = sel?.value || "";
  const logEl = document.getElementById("gh-sync-log");
  if (!snap) return;
  if (!confirm(`Roll back application files to snapshot '${snap}'?`)) return;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/app/rollback", { snapshot: snap });
        const data = res?.data ?? res;
        if (logEl) logEl.textContent = `Rolled back ${data.restored_count} file(s) from ${snap}. Safety snapshot: ${data.safety_snapshot}. Click Reload Server to apply backend changes.`;
        await loadGitHubRepoConfig();
        loadLogs();
        return data;
      },
      {
        pendingText: `Rolling back to snapshot '${snap}'...`,
        successText: (d) => `Rolled back ${d?.restored_count ?? 0} file(s) from ${snap}`,
      },
    );
  } catch (err) {
    if (logEl) logEl.textContent = `Rollback failed: ${err?.message || err}`;
  }
}

export async function pullGitHubBranches() {
  const btn = resolveBtn("btn-gh-pull-branches");
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
  try {
    await runTrackedAction(
      btn,
      async () => {
        const d = await apiPost("/api/app/git-branches", { owner: ownerVal, repo: repoVal });
        const branches = Array.isArray(d?.branches) ? d.branches : [];
        const preferred = document.getElementById("gh-push-branch")?.value
          || d?.current_branch
          || d?.configured_branch
          || document.getElementById("gh-branch")?.value
          || "main";
        _populatePushBranches(branches, preferred);
        if (d?.status !== "ok") {
          const msg = d?.message || "Failed to pull branches from GitHub.";
          if (logEl) logEl.textContent = `${msg}${branches.length ? ` (Showing local branches: ${branches.join(", ")})` : ""}`;
          throw new Error(msg);
        }
        const remCount = Array.isArray(d.remote_branches) ? d.remote_branches.length : 0;
        const locCount = Array.isArray(d.local_branches) ? d.local_branches.length : 0;
        if (logEl) {
          logEl.textContent = branches.length
            ? `Pulled ${branches.length} branch(es) (${remCount} remote, ${locCount} local): ${branches.join(", ")}`
            : "No branches returned from GitHub.";
        }
        return d;
      },
      {
        pendingText: "Pulling available branches from GitHub...",
        successText: (d) => `Pulled ${(d?.branches || []).length} available branch(es)`,
      },
    );
  } catch (err) {
    if (logEl && !logEl.textContent) logEl.textContent = `Branch pull failed: ${err?.message || err}`;
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
    const cfgBranch = (document.getElementById("gh-branch")?.value || "main").trim() || "main";
    _populatePushBranches(d.branches || [], d.branch || cfgBranch);
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

export function closeGitPushConfirmModal() {
  const ov = document.getElementById("gh-push-confirm-overlay");
  if (ov) ov.style.display = "none";
  _pendingGitPushBranch = "";
}

export function pushAppToGitHub() {
  const pushSel = document.getElementById("gh-push-branch");
  const branch = (pushSel?.value || document.getElementById("gh-branch")?.value || "main").trim() || "main";
  _pendingGitPushBranch = branch;

  const ov = document.getElementById("gh-push-confirm-overlay");
  const branchEl = document.getElementById("gh-push-confirm-branch");
  const confirmBtn = document.getElementById("btn-gh-push-confirm");
  if (branchEl) branchEl.textContent = branch;
  if (confirmBtn) confirmBtn.textContent = `Push to ${branch}`;
  if (ov) {
    ov.style.display = "flex";
    const cancelBtn = document.getElementById("btn-gh-push-cancel");
    if (cancelBtn) cancelBtn.focus();
  }
}

export async function confirmPushAppToGitHub() {
  const pushSel = document.getElementById("gh-push-branch");
  const branch = (
    _pendingGitPushBranch
    || pushSel?.value
    || document.getElementById("gh-branch")?.value
    || "main"
  ).trim() || "main";
  closeGitPushConfirmModal();

  const btn = resolveBtn("btn-gh-push");
  const logEl = document.getElementById("gh-sync-log");
  const errBox = document.getElementById("gh-push-error-box");
  const errSum = document.getElementById("gh-push-error-summary");
  const errPre = document.getElementById("gh-push-error-pre");
  const msg = (document.getElementById("gh-commit-msg")?.value || "").trim();
  if (errBox) errBox.style.display = "none";
  if (logEl) logEl.textContent = `Staging changes, committing, and pushing to '${branch}'...`;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiFetch("/api/app/git-push", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ commit_message: msg, branch }),
        });
        const text = await res.text();
        let data = {};
        try { data = JSON.parse(text); } catch { data = { raw: text }; }
        if (!res.ok) {
          const step = data.step || "push";
          const code = data.exit_code !== undefined ? data.exit_code : res.status;
          const hint = data.hint || data.detail || "Git push failed";
          const details = [data.stderr, data.stdout].filter(Boolean).join("\n\n") || text || "Unknown error";
          if (logEl) logEl.textContent = `Push failed at '${step}' (exit ${code}): ${hint}`;
          if (errSum) errSum.textContent = `Step: ${step} · Exit: ${code} — ${hint}`;
          if (errPre) errPre.textContent = details;
          if (errBox) errBox.style.display = "block";
          await loadLogs();
          const err = new Error(`Step: ${step} · Exit: ${code} — ${hint}`);
          err.details = details;
          throw err;
        }
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
        return data;
      },
      {
        pendingText: `Staging, committing, and pushing to '${branch}'...`,
        successText: (d) => d?.status === "up_to_date"
          ? (d.message || "Already up to date with origin")
          : `Pushed ${d?.short_sha || "HEAD"} to ${d?.branch || branch}`,
      },
    );
  } catch (err) {
    if (!err?.details) {
      if (logEl) logEl.textContent = `Push failed: ${err?.message || err}`;
      if (errSum) errSum.textContent = "Git Push Request Error";
      if (errPre) errPre.textContent = String(err?.message || err);
      if (errBox) errBox.style.display = "block";
    }
  }
}

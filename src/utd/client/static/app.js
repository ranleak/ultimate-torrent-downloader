const state = {
  activeTab: "torrents",
  sourceMode: "magnet",
  inspectorTab: "logs",
  selectedJobId: null,
  jobs: new Map(),
  jobLogs: new Map(),
  transfers: new Map(),
  currentDir: "",
  entries: [],
  selectedPaths: new Set(),
  pendingTorrentFile: null,
  pendingTorrentMeta: null,
  ws: null,
  sse: null,
  wsRetryTimer: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = bytes;
  let idx = 0;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val >= 100 || idx === 0 ? val.toFixed(0) : val.toFixed(1)} ${units[idx]}`;
}

function formatSpeed(bps) {
  return `${formatBytes(bps)}/s`;
}

function formatEta(secs) {
  if (secs == null || secs < 0) return "—";
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function formatTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour12: false });
}

function formatDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

function showToast(message, kind = "info") {
  const container = $("#toast-container");
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function apiJson(url, opts = {}) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      detail = body.detail || detail;
    } catch {
      detail = (await resp.text()) || detail;
    }
    throw new Error(detail);
  }
  return resp.json();
}

// ==================== Tab Navigation ====================

function switchTab(tabName) {
  state.activeTab = tabName;
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tabName}`));
  if (tabName === "explorer") {
    loadDirectory(state.currentDir);
  } else if (tabName === "transfers") {
    loadTransfers();
  } else if (tabName === "settings") {
    loadSettings();
  }
}

$$(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

$("#conn-pill").addEventListener("click", () => switchTab("settings"));
$("#banner-open-settings").addEventListener("click", () => switchTab("settings"));

// ==================== Composer & Torrent Options ====================

$$(".composer-header .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.sourceMode = btn.dataset.source;
    $$(".composer-header .seg-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.source === state.sourceMode)
    );
    $("#source-magnet-panel").classList.toggle("hidden", state.sourceMode !== "magnet");
    $("#source-torrent-panel").classList.toggle("hidden", state.sourceMode !== "torrent");
  });
});

$("#toggle-options-btn").addEventListener("click", () => {
  const drawer = $("#options-drawer");
  const hidden = drawer.classList.toggle("hidden");
  $("#options-chevron").textContent = hidden ? "▾" : "▴";
});

function readOptionsFromForm() {
  const selected = [];
  if (state.sourceMode === "torrent" && state.pendingTorrentMeta) {
    const checkboxes = $$("#preview-files-list input[type='checkbox']");
    const checked = checkboxes.filter((c) => c.checked).map((c) => Number(c.value));
    if (checked.length > 0 && checked.length < checkboxes.length) {
      selected.push(...checked);
    }
  }
  return {
    destination_subdir: $("#opt-subdir").value.trim(),
    max_download_limit: $("#opt-dl-limit").value.trim() || "0",
    max_upload_limit: $("#opt-ul-limit").value.trim() || "0",
    max_peers: Number($("#opt-max-peers").value || 128),
    seed_time_minutes: Number($("#opt-seed-time").value || 0),
    seed_ratio: Number($("#opt-seed-ratio").value || 0),
    disk_cache_mb: Number($("#opt-cache-mb").value || 64),
    split: Number($("#opt-split").value || 16),
    max_connection_per_server: Math.min(16, Math.max(1, Number($("#opt-split").value || 16))),
    sequential: $("#opt-sequential").checked,
    extra_trackers: $("#opt-extra-trackers").checked,
    selected_files: selected,
  };
}

async function handleTorrentFileSelected(file) {
  if (!file) return;
  state.pendingTorrentFile = file;
  $("#dropzone-label").innerHTML = `Selected: <strong>${escapeHtml(file.name)}</strong>`;
  const form = new FormData();
  form.append("file", file);
  try {
    const meta = await apiJson("/api/client/inspect-torrent", { method: "POST", body: form });
    state.pendingTorrentMeta = meta;
    $("#preview-name").textContent = meta.name;
    $("#preview-hash").textContent = `infohash: ${meta.info_hash}${meta.is_private ? " · PRIVATE" : ""}`;
    $("#preview-size").textContent = formatBytes(meta.total_size);
    const list = $("#preview-files-list");
    list.innerHTML = meta.files
      .map(
        (f) => `
      <div class="preview-file-row">
        <label title="${escapeHtml(f.path)}">
          <input type="checkbox" value="${f.index}" checked />
          <span>${escapeHtml(f.path)}</span>
        </label>
        <span class="mono muted">${formatBytes(f.size)}</span>
      </div>`
      )
      .join("");
    $("#torrent-preview").classList.remove("hidden");
  } catch (err) {
    showToast(`Failed to inspect .torrent: ${err.message}`, "error");
  }
}

const dropzone = $("#torrent-dropzone");
const fileInput = $("#torrent-file-input");
dropzone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => handleTorrentFileSelected(fileInput.files[0]));
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dragover");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files.length > 0) {
    handleTorrentFileSelected(e.dataTransfer.files[0]);
  }
});

$("#select-all-files").addEventListener("click", () => {
  $$("#preview-files-list input[type='checkbox']").forEach((c) => {
    c.checked = true;
  });
});
$("#select-none-files").addEventListener("click", () => {
  $$("#preview-files-list input[type='checkbox']").forEach((c) => {
    c.checked = false;
  });
});

$("#submit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#submit-btn");
  btn.disabled = true;
  try {
    const options = readOptionsFromForm();
    if (state.sourceMode === "magnet") {
      const lines = $("#magnet-input")
        .value.split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean);
      if (lines.length === 0) {
        showToast("Enter at least one magnet link or URL.", "error");
        return;
      }
      for (const uri of lines) {
        const job = await apiJson("/api/jobs/magnet", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ uri, options }),
        });
        upsertJob(job);
        selectJob(job.job_id);
      }
      $("#magnet-input").value = "";
      showToast(`Submitted ${lines.length} cloud download(s).`, "success");
    } else {
      if (!state.pendingTorrentFile) {
        showToast("Choose a .torrent file first.", "error");
        return;
      }
      const fd = new FormData();
      fd.append("file", state.pendingTorrentFile);
      fd.append("options", JSON.stringify(options));
      const job = await apiJson("/api/jobs/torrent", { method: "POST", body: fd });
      upsertJob(job);
      selectJob(job.job_id);
      state.pendingTorrentFile = null;
      state.pendingTorrentMeta = null;
      $("#torrent-preview").classList.add("hidden");
      $("#dropzone-label").innerHTML =
        'Drop a <strong>.torrent</strong> file here or <span class="link-like">browse</span>';
      showToast(`Submitted torrent "${job.name}".`, "success");
    }
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
});

// ==================== Jobs State & Rendering ====================

function upsertJob(job) {
  if (!job || !job.job_id) return;
  const existing = state.jobs.get(job.job_id);
  const merged = existing ? { ...existing, ...job } : job;
  if (Array.isArray(job.logs) && job.logs.length > 0) {
    const logsMap = state.jobLogs.get(job.job_id) || new Map();
    for (const l of job.logs) logsMap.set(l.seq, l);
    state.jobLogs.set(job.job_id, logsMap);
  }
  state.jobs.set(job.job_id, merged);
  renderJobs();
  updateAggregateMetrics();
  if (state.selectedJobId === job.job_id) {
    renderInspector();
  }
}

function appendLog(jobId, logEvt) {
  if (!jobId || !logEvt) return;
  const logsMap = state.jobLogs.get(jobId) || new Map();
  logsMap.set(logEvt.seq, logEvt);
  if (logsMap.size > 600) {
    const oldest = logsMap.keys().next().value;
    logsMap.delete(oldest);
  }
  state.jobLogs.set(jobId, logsMap);
  if (state.selectedJobId === jobId && state.inspectorTab === "logs") {
    renderLogsConsole();
  }
}

function updateAggregateMetrics() {
  let dl = 0;
  let ul = 0;
  let active = 0;
  for (const job of state.jobs.values()) {
    dl += Number(job.download_speed || 0);
    ul += Number(job.upload_speed || 0);
    if (!["completed", "cancelled", "error"].includes(job.status)) {
      active += 1;
    }
  }
  $("#total-dl-speed").textContent = formatSpeed(dl);
  $("#total-ul-speed").textContent = formatSpeed(ul);
  $("#active-jobs-count").textContent = String(active);
  $("#badge-jobs").textContent = String(state.jobs.size);
}

function renderJobs() {
  const container = $("#jobs-list");
  const sorted = Array.from(state.jobs.values()).sort((a, b) => b.created_at - a.created_at);
  if (sorted.length === 0) {
    container.innerHTML =
      '<div class="empty-state">No torrents yet. Paste a magnet link or drop a .torrent file above.</div>';
    return;
  }

  container.innerHTML = sorted
    .map((job) => {
      const selectedCls = job.job_id === state.selectedJobId ? "selected" : "";
      const isTerminal = ["completed", "cancelled", "error"].includes(job.status);
      const isPaused = job.status === "paused";
      const isSeeding = job.status === "seeding";

      const actionButtons = [];
      if (!isTerminal && !isPaused) {
        actionButtons.push(`<button class="btn btn-ghost btn-xs" data-act="pause" data-id="${job.job_id}">Pause</button>`);
      }
      if (isPaused || job.status === "error" || job.status === "cancelled") {
        actionButtons.push(`<button class="btn btn-ghost btn-xs" data-act="resume" data-id="${job.job_id}">Resume</button>`);
      }
      if (isSeeding) {
        actionButtons.push(`<button class="btn btn-primary btn-xs" data-act="stop-seeding" data-id="${job.job_id}">Finish Seeding</button>`);
      }
      if (!isTerminal) {
        actionButtons.push(`<button class="btn btn-ghost btn-xs" data-act="throttle" data-id="${job.job_id}">Throttle</button>`);
        actionButtons.push(`<button class="btn btn-danger btn-xs" data-act="cancel" data-id="${job.job_id}">Cancel</button>`);
      } else {
        actionButtons.push(`<button class="btn btn-ghost btn-xs" data-act="explore" data-id="${job.job_id}">Open Volume</button>`);
        actionButtons.push(`<button class="btn btn-danger btn-xs" data-act="delete" data-id="${job.job_id}">Remove</button>`);
      }

      return `
      <div class="job-card ${selectedCls}" data-job-id="${job.job_id}">
        <div class="job-top">
          <div class="job-title">${escapeHtml(job.name)}</div>
          <span class="status-pill status-${job.status}">${escapeHtml(job.status)}</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill" style="width:${Math.min(100, Math.max(0, job.progress || 0))}%"></div>
        </div>
        <div class="job-stats mono">
          <span>${Number(job.progress || 0).toFixed(1)}% · ${formatBytes(job.completed_bytes)} / ${formatBytes(job.total_bytes)}</span>
          <span>↓ ${formatSpeed(job.download_speed)} · ↑ ${formatSpeed(job.upload_speed)}</span>
          <span>Peers ${job.connections || 0} (Seeds ${job.seeders || 0}) · ETA ${formatEta(job.eta_seconds)}</span>
        </div>
        ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}
        <div class="job-actions">${actionButtons.join("")}</div>
      </div>`;
    })
    .join("");
}

$("#jobs-list").addEventListener("click", async (e) => {
  const actBtn = e.target.closest("button[data-act]");
  if (actBtn) {
    e.stopPropagation();
    const { act, id } = actBtn.dataset;
    await handleJobAction(act, id);
    return;
  }
  const card = e.target.closest(".job-card");
  if (card) {
    selectJob(card.dataset.jobId);
  }
});

async function handleJobAction(action, jobId) {
  try {
    if (action === "explore") {
      const job = state.jobs.get(jobId);
      state.currentDir = job?.output_dir || "downloads";
      switchTab("explorer");
      return;
    }
    if (action === "throttle") {
      const job = state.jobs.get(jobId);
      const val = window.prompt(
        "Max download speed limit (0 for unlimited, e.g. 25M or 500K):",
        job?.options?.max_download_limit || "0"
      );
      if (val === null) return;
      const updated = await apiJson(`/api/jobs/${jobId}/options`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_download_limit: val.trim() || "0" }),
      });
      upsertJob(updated);
      showToast(`Speed limit updated to ${val.trim() || "0"}.`, "success");
      return;
    }
    if (action === "delete") {
      const delFiles = window.confirm("Also delete downloaded files from Modal Volume? (Cancel keeps files)");
      await apiJson(`/api/jobs/${jobId}?delete_files=${delFiles}`, { method: "DELETE" });
      state.jobs.delete(jobId);
      state.jobLogs.delete(jobId);
      if (state.selectedJobId === jobId) {
        state.selectedJobId = null;
        renderInspector();
      }
      renderJobs();
      updateAggregateMetrics();
      return;
    }
    const endpoint =
      action === "cancel"
        ? `/api/jobs/${jobId}/cancel`
        : `/api/jobs/${jobId}/${action}`;
    const updated = await apiJson(endpoint, { method: "POST" });
    upsertJob(updated);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function selectJob(jobId) {
  state.selectedJobId = jobId;
  renderJobs();
  let full = state.jobs.get(jobId);
  try {
    full = await apiJson(`/api/jobs/${jobId}`);
    upsertJob(full);
  } catch {
    renderInspector();
  }
  if (full && !["completed", "cancelled", "error"].includes(full.status)) {
    openJobSseFallback(jobId);
  } else if (state.sse) {
    state.sse.close();
    state.sse = null;
  }
}

// ==================== Inspector & Live Logs ====================

$$(".inspector-panel .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.inspectorTab = btn.dataset.inspector;
    $$(".inspector-panel .seg-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.inspector === state.inspectorTab)
    );
    $("#inspector-logs-pane").classList.toggle("hidden", state.inspectorTab !== "logs");
    $("#inspector-files-pane").classList.toggle("hidden", state.inspectorTab !== "files");
    renderInspector();
  });
});

function renderInspector() {
  const job = state.jobs.get(state.selectedJobId);
  if (!job) {
    $("#inspector-title").textContent = "Live Inspector";
    $("#inspector-subtitle").textContent = "Select a job to inspect live container logs & files";
    $("#log-console").innerHTML = "";
    $("#inspector-files-list").innerHTML = '<div class="empty-state">No job selected.</div>';
    return;
  }
  $("#inspector-title").textContent = job.name;
  $("#inspector-subtitle").textContent = `job:${job.job_id} · out:/${job.output_dir}${job.info_hash ? ` · ${job.info_hash}` : ""}`;
  renderLogsConsole();
  renderInspectorFiles(job);
}

function renderLogsConsole() {
  const consoleEl = $("#log-console");
  const logsMap = state.jobLogs.get(state.selectedJobId);
  if (!logsMap || logsMap.size === 0) {
    consoleEl.innerHTML = '<div class="muted">Waiting for container logs...</div>';
    return;
  }
  const levelFilter = $("#log-level-filter").value;
  const search = $("#log-search").value.trim().toLowerCase();
  const rank = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };
  const minRank = levelFilter === "ALL" ? 0 : rank[levelFilter] || 0;

  const lines = Array.from(logsMap.values())
    .sort((a, b) => a.seq - b.seq)
    .filter((l) => (rank[l.level] ?? 1) >= minRank)
    .filter((l) => !search || l.message.toLowerCase().includes(search) || l.source.toLowerCase().includes(search));

  consoleEl.innerHTML = lines
    .map(
      (l) => `
    <div class="log-line">
      <span class="log-ts">${formatTime(l.ts)}</span>
      <span class="log-lvl-${l.level}">[${l.level}]</span>
      <span>${escapeHtml(l.message)}</span>
    </div>`
    )
    .join("");

  if ($("#log-autoscroll").checked) {
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

function renderInspectorFiles(job) {
  const listEl = $("#inspector-files-list");
  if (!job.files || job.files.length === 0) {
    listEl.innerHTML = '<div class="empty-state">Metadata pending or no file list yet.</div>';
    return;
  }
  listEl.innerHTML = job.files
    .map((f) => {
      const pct = f.size > 0 ? Math.min(100, (f.completed / f.size) * 100) : 0;
      const volPath = `${job.output_dir}/${f.path}`.replace(/\/+/g, "/");
      return `
      <div class="inspector-file-item">
        <div style="min-width:0;flex:1;">
          <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(f.path)}</div>
          <div class="muted mono" style="font-size:11px;">${formatBytes(f.completed)} / ${formatBytes(f.size)} (${pct.toFixed(1)}%) · ${f.selected ? "selected" : "skipped"}</div>
        </div>
        <button class="btn btn-primary btn-xs" data-transfer-path="${escapeHtml(volPath)}" ${pct < 100 ? "disabled" : ""}>Save to PC</button>
      </div>`;
    })
    .join("");
}

$("#inspector-files-list").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-transfer-path]");
  if (btn) startPcTransfers([btn.dataset.transferPath]);
});

$("#log-level-filter").addEventListener("change", renderLogsConsole);
$("#log-search").addEventListener("input", renderLogsConsole);
$("#log-sse-toggle").addEventListener("click", () => {
  if (state.selectedJobId) openJobSseFallback(state.selectedJobId, true);
});
$("#log-copy-btn").addEventListener("click", async () => {
  const logsMap = state.jobLogs.get(state.selectedJobId);
  if (!logsMap) return;
  const text = Array.from(logsMap.values())
    .sort((a, b) => a.seq - b.seq)
    .map((l) => `${formatTime(l.ts)} [${l.level}] ${l.source}: ${l.message}`)
    .join("\n");
  await navigator.clipboard.writeText(text);
  showToast("Logs copied to clipboard.", "success");
});

$("#refresh-jobs-btn").addEventListener("click", loadJobs);

async function loadJobs() {
  try {
    const jobs = await apiJson("/api/jobs");
    state.jobs.clear();
    for (const j of jobs) upsertJob(j);
    if (!state.selectedJobId && jobs.length > 0) {
      selectJob(jobs[0].job_id);
    }
  } catch {
    // Ignore when unconfigured
  }
}

// ==================== WebSocket + SSE Live Streaming ====================

function openJobSseFallback(jobId, force = false) {
  if (state.sse) {
    state.sse.close();
    state.sse = null;
  }
  if (!jobId) return;
  // Open SSE directly for the inspected job if WebSocket is disconnected or user forced reconnect.
  if (!force && state.ws && state.ws.readyState === WebSocket.OPEN) {
    // Still connect per-job SSE so logs stream immediately even when WS is multiplexed!
  }
  const logsMap = state.jobLogs.get(jobId);
  let maxSeq = 0;
  if (logsMap) {
    for (const k of logsMap.keys()) maxSeq = Math.max(maxSeq, k);
  }
  const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream?since_seq=${maxSeq}`);
  ["snapshot", "progress", "status"].forEach((evtType) => {
    es.addEventListener(evtType, (ev) => {
      try {
        const parsed = JSON.parse(ev.data);
        upsertJob(parsed.data);
      } catch {}
    });
  });
  es.addEventListener("log", (ev) => {
    try {
      const parsed = JSON.parse(ev.data);
      appendLog(parsed.job_id, parsed.data);
    } catch {}
  });
  es.addEventListener("error", () => {
    // EventSource automatically reconnects based on server retry: 500
  });
  state.sse = es;
}

function setConnIndicator(connState) {
  const pill = $("#conn-pill");
  const label = $("#conn-label");
  pill.className = "conn-pill";
  if (connState === "connected") {
    pill.classList.add("conn-connected");
    label.textContent = "Modal Connected";
    $("#setup-banner").classList.add("hidden");
  } else if (connState === "unconfigured") {
    pill.classList.add("conn-unconfigured");
    label.textContent = "Configure Modal";
    $("#setup-banner").classList.remove("hidden");
  } else {
    pill.classList.add("conn-reconnecting");
    label.textContent = "Reconnecting…";
  }
}

function connectWebSocket() {
  if (state.wsRetryTimer) {
    clearTimeout(state.wsRetryTimer);
    state.wsRetryTimer = null;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/api/ws`);
  state.ws = ws;

  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "connection") {
        setConnIndicator(msg.data?.state);
        if (msg.data?.state === "connected") loadJobs();
      } else if (msg.type === "transfer") {
        upsertTransfer(msg.data);
      } else if (["snapshot", "progress", "status"].includes(msg.type)) {
        upsertJob(msg.data);
      } else if (msg.type === "log") {
        appendLog(msg.job_id, msg.data);
      }
    } catch {}
  };

  ws.onclose = () => {
    setConnIndicator("reconnecting");
    state.wsRetryTimer = setTimeout(connectWebSocket, 2500);
  };
}

// ==================== Modal Volume File Explorer ====================

async function loadDirectory(path) {
  state.currentDir = path || "";
  state.selectedPaths.clear();
  updateSelectionBar();
  renderBreadcrumbs();
  const tbody = $("#exp-tbody");
  tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">Loading remote volume…</td></tr>';
  try {
    state.entries = await apiJson(`/api/files?path=${encodeURIComponent(state.currentDir)}`);
    renderExplorerTable();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-cell">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderBreadcrumbs() {
  const wrap = $("#exp-breadcrumbs");
  const parts = state.currentDir ? state.currentDir.split("/").filter(Boolean) : [];
  const crumbs = ['<button class="crumb-btn" data-dir="">/</button>'];
  let acc = "";
  for (const part of parts) {
    acc = acc ? `${acc}/${part}` : part;
    crumbs.push(`<span class="muted">/</span><button class="crumb-btn" data-dir="${escapeHtml(acc)}">${escapeHtml(part)}</button>`);
  }
  wrap.innerHTML = crumbs.join("");
}

$("#exp-breadcrumbs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-dir]");
  if (btn) loadDirectory(btn.dataset.dir);
});

$("#exp-home-btn").addEventListener("click", () => loadDirectory(""));
$("#exp-up-btn").addEventListener("click", () => {
  if (!state.currentDir) return;
  const parts = state.currentDir.split("/").filter(Boolean);
  parts.pop();
  loadDirectory(parts.join("/"));
});
$("#exp-refresh-btn").addEventListener("click", () => loadDirectory(state.currentDir));
$("#exp-search").addEventListener("input", renderExplorerTable);
$("#exp-sort").addEventListener("change", renderExplorerTable);

function renderExplorerTable() {
  const tbody = $("#exp-tbody");
  const filter = $("#exp-search").value.trim().toLowerCase();
  const sortBy = $("#exp-sort").value;

  const list = state.entries
    .filter((e) => !filter || e.name.toLowerCase().includes(filter))
    .sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      if (sortBy === "size") return b.size - a.size;
      if (sortBy === "mtime") return b.mtime - a.mtime;
      return a.name.localeCompare(b.name);
    });

  if (list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">Directory is empty.</td></tr>';
    return;
  }

  tbody.innerHTML = list
    .map((entry) => {
      const checked = state.selectedPaths.has(entry.path) ? "checked" : "";
      const icon = entry.is_dir ? "📁" : "📄";
      const dlUrl = `/api/files/download?path=${encodeURIComponent(entry.path)}`;
      const zipUrl = `/api/files/archive?paths=${encodeURIComponent(entry.path)}&name=${encodeURIComponent(entry.name + ".zip")}`;
      return `
      <tr>
        <td><input type="checkbox" class="exp-row-check" data-path="${escapeHtml(entry.path)}" ${checked} /></td>
        <td>
          <button class="file-name-btn" data-open-path="${escapeHtml(entry.path)}" data-is-dir="${entry.is_dir}" data-mime="${escapeHtml(entry.mime_type || "")}">
            <span>${icon}</span>
            <span>${escapeHtml(entry.name)}</span>
          </button>
        </td>
        <td class="mono">${entry.is_dir ? "—" : formatBytes(entry.size)}</td>
        <td class="muted">${formatDate(entry.mtime)}</td>
        <td>
          <div class="row-ops">
            <button class="btn btn-primary btn-xs" data-exp-act="transfer" data-path="${escapeHtml(entry.path)}">Save to PC</button>
            ${entry.is_dir
              ? `<a class="btn btn-ghost btn-xs" href="${zipUrl}">ZIP</a>`
              : `<a class="btn btn-ghost btn-xs" href="${dlUrl}">Download</a><button class="btn btn-ghost btn-xs" data-exp-act="preview" data-path="${escapeHtml(entry.path)}" data-mime="${escapeHtml(entry.mime_type || "")}">Preview</button>`
            }
            <button class="btn btn-ghost btn-xs" data-exp-act="rename" data-path="${escapeHtml(entry.path)}">Rename</button>
            <button class="btn btn-danger btn-xs" data-exp-act="delete" data-path="${escapeHtml(entry.path)}">Delete</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

function updateSelectionBar() {
  const count = state.selectedPaths.size;
  $("#exp-selected-count").textContent = `${count} selected`;
  $("#exp-selection-bar").classList.toggle("hidden", count === 0);
}

$("#exp-check-all").addEventListener("change", (e) => {
  const checked = e.target.checked;
  state.selectedPaths.clear();
  if (checked) state.entries.forEach((entry) => state.selectedPaths.add(entry.path));
  renderExplorerTable();
  updateSelectionBar();
});

$("#exp-tbody").addEventListener("change", (e) => {
  if (e.target.classList.contains("exp-row-check")) {
    const p = e.target.dataset.path;
    if (e.target.checked) state.selectedPaths.add(p);
    else state.selectedPaths.delete(p);
    updateSelectionBar();
  }
});

$("#exp-tbody").addEventListener("click", async (e) => {
  const openBtn = e.target.closest(".file-name-btn");
  if (openBtn) {
    const { openPath, isDir, mime } = openBtn.dataset;
    if (isDir === "true") {
      loadDirectory(openPath);
    } else {
      openPreview(openPath, mime);
    }
    return;
  }

  const actBtn = e.target.closest("button[data-exp-act]");
  if (!actBtn) return;
  const { expAct, path, mime } = actBtn.dataset;
  if (expAct === "transfer") {
    await startPcTransfers([path]);
  } else if (expAct === "preview") {
    await openPreview(path, mime);
  } else if (expAct === "rename") {
    const next = window.prompt("New path on Modal Volume:", path);
    if (!next || next === path) return;
    try {
      await apiJson("/api/files/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src_path: path, dst_path: next }),
      });
      showToast("Renamed successfully.", "success");
      loadDirectory(state.currentDir);
    } catch (err) {
      showToast(err.message, "error");
    }
  } else if (expAct === "delete") {
    if (!window.confirm(`Permanently delete "${path}" from Modal Volume?`)) return;
    try {
      await apiJson(`/api/files?path=${encodeURIComponent(path)}&recursive=true`, {
        method: "DELETE",
      });
      showToast("Deleted from Modal Volume.", "success");
      loadDirectory(state.currentDir);
    } catch (err) {
      showToast(err.message, "error");
    }
  }
});

$("#exp-batch-transfer").addEventListener("click", async () => {
  await startPcTransfers(Array.from(state.selectedPaths));
});

$("#exp-batch-zip").addEventListener("click", () => {
  const qs = Array.from(state.selectedPaths)
    .map((p) => `paths=${encodeURIComponent(p)}`)
    .join("&");
  window.location.href = `/api/files/archive?${qs}&name=modal-bundle.zip`;
});

$("#exp-batch-delete").addEventListener("click", async () => {
  const paths = Array.from(state.selectedPaths);
  if (!window.confirm(`Delete ${paths.length} item(s) permanently from Modal Volume?`)) return;
  for (const p of paths) {
    try {
      await apiJson(`/api/files?path=${encodeURIComponent(p)}&recursive=true`, { method: "DELETE" });
    } catch (err) {
      showToast(`${p}: ${err.message}`, "error");
    }
  }
  loadDirectory(state.currentDir);
});

async function openPreview(path, mime) {
  const dialog = $("#preview-dialog");
  const title = $("#preview-dialog-title");
  const body = $("#preview-dialog-body");
  title.textContent = path;
  const inlineUrl = `/api/files/download?path=${encodeURIComponent(path)}&inline=true`;

  if (mime.startsWith("video/")) {
    body.innerHTML = `<video controls autoplay src="${inlineUrl}"></video>`;
  } else if (mime.startsWith("audio/")) {
    body.innerHTML = `<audio controls autoplay style="width:100%;" src="${inlineUrl}"></audio>`;
  } else if (mime.startsWith("image/")) {
    body.innerHTML = `<img src="${inlineUrl}" alt="${escapeHtml(path)}" />`;
  } else {
    body.innerHTML = '<div class="muted">Loading text preview…</div>';
    try {
      const resp = await fetch(inlineUrl, { headers: { Range: "bytes=0-131071" } });
      const text = await resp.text();
      body.innerHTML = `<pre>${escapeHtml(text)}</pre>`;
    } catch (err) {
      body.innerHTML = `<div class="muted">Preview unavailable: ${escapeHtml(err.message)}</div>`;
    }
  }
  dialog.showModal();
}

$("#preview-dialog-close").addEventListener("click", () => {
  $("#preview-dialog-body").innerHTML = "";
  $("#preview-dialog").close();
});

// ==================== Local PC Transfers ====================

async function startPcTransfers(paths) {
  if (!paths || paths.length === 0) return;
  try {
    const items = await apiJson("/api/transfers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths }),
    });
    for (const t of items) upsertTransfer(t);
    showToast(`Started ${items.length} accelerated PC transfer(s).`, "success");
    switchTab("transfers");
  } catch (err) {
    showToast(err.message, "error");
  }
}

function upsertTransfer(t) {
  if (!t || !t.transfer_id) return;
  state.transfers.set(t.transfer_id, t);
  renderTransfers();
}

async function loadTransfers() {
  try {
    const list = await apiJson("/api/transfers");
    state.transfers.clear();
    for (const t of list) state.transfers.set(t.transfer_id, t);
    renderTransfers();
  } catch {}
}

function renderTransfers() {
  const container = $("#transfers-list");
  const sorted = Array.from(state.transfers.values()).sort((a, b) => b.created_at - a.created_at);
  const activeCount = sorted.filter((t) => ["queued", "transferring"].includes(t.status)).length;
  $("#badge-transfers").textContent = String(activeCount);

  if (sorted.length === 0) {
    container.innerHTML =
      '<div class="empty-state">No transfers yet. Use "Save to PC" in Volume Explorer for accelerated multi-stream downloads.</div>';
    return;
  }

  container.innerHTML = sorted
    .map(
      (t) => `
    <div class="transfer-row">
      <div class="job-top">
        <div>
          <strong>${escapeHtml(t.filename)}</strong>
          <div class="muted mono" style="font-size:11px;">${escapeHtml(t.local_path)}</div>
        </div>
        <span class="status-pill status-${t.status === "transferring" ? "downloading" : t.status}">${escapeHtml(t.status)}</span>
      </div>
      <div class="progress-track">
        <div class="progress-fill" style="width:${Math.min(100, t.progress || 0)}%"></div>
      </div>
      <div class="job-stats mono">
        <span>${Number(t.progress || 0).toFixed(1)}% · ${formatBytes(t.transferred_bytes)} / ${formatBytes(t.total_bytes)}</span>
        <span>${formatSpeed(t.speed_bps)}</span>
        <div>
          ${
            ["queued", "transferring"].includes(t.status)
              ? `<button class="btn btn-danger btn-xs" data-tr-act="cancel" data-id="${t.transfer_id}">Cancel</button>`
              : `<button class="btn btn-ghost btn-xs" data-tr-act="open" data-path="${escapeHtml(t.local_path)}">Open Folder</button>
                 <button class="btn btn-ghost btn-xs" data-tr-act="remove" data-id="${t.transfer_id}">Clear</button>`
          }
        </div>
      </div>
      ${t.error ? `<div class="job-error">${escapeHtml(t.error)}</div>` : ""}
    </div>`
    )
    .join("");
}

$("#transfers-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-tr-act]");
  if (!btn) return;
  const { trAct, id, path } = btn.dataset;
  if (trAct === "cancel") {
    const updated = await apiJson(`/api/transfers/${id}/cancel`, { method: "POST" });
    upsertTransfer(updated);
  } else if (trAct === "remove") {
    await apiJson(`/api/transfers/${id}`, { method: "DELETE" });
    state.transfers.delete(id);
    renderTransfers();
  } else if (trAct === "open") {
    await apiJson("/api/client/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
  }
});

$("#open-local-folder-btn").addEventListener("click", async () => {
  await apiJson("/api/client/open-folder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
});

// ==================== Settings ====================

async function loadSettings() {
  try {
    const cfg = await apiJson("/api/client/config");
    $("#cfg-endpoint").value = cfg.modal_endpoint_url || "";
    $("#cfg-token").value = "";
    $("#cfg-token-hint").textContent = cfg.has_token
      ? `(Saved: ${cfg.api_token_masked})`
      : "(Not set)";
    $("#cfg-local-dir").value = cfg.local_download_dir || "";
    $("#cfg-concurrency").value = cfg.transfer_concurrency || 4;
    $("#cfg-chunk-mb").value = cfg.transfer_chunk_mb || 8;
    $("#transfers-dest-label").textContent = `Saving to: ${cfg.local_download_dir}`;
  } catch {}
}

$("#test-conn-btn").addEventListener("click", async () => {
  const resEl = $("#test-conn-result");
  resEl.textContent = "Testing…";
  try {
    const res = await apiJson("/api/client/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        modal_endpoint_url: $("#cfg-endpoint").value.trim(),
        api_token: $("#cfg-token").value.trim(),
      }),
    });
    resEl.textContent = `Connected (${res.latency_ms} ms · engine: ${res.health?.engine})`;
    resEl.style.color = "var(--success)";
  } catch (err) {
    resEl.textContent = `Failed: ${err.message}`;
    resEl.style.color = "var(--danger)";
  }
});

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const payload = {
      modal_endpoint_url: $("#cfg-endpoint").value.trim(),
      local_download_dir: $("#cfg-local-dir").value.trim(),
      transfer_concurrency: Number($("#cfg-concurrency").value || 4),
      transfer_chunk_mb: Number($("#cfg-chunk-mb").value || 8),
    };
    const tokenVal = $("#cfg-token").value.trim();
    if (tokenVal) payload.api_token = tokenVal;

    const saved = await apiJson("/api/client/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("#cfg-token").value = "";
    $("#cfg-token-hint").textContent = saved.has_token ? `(Saved: ${saved.api_token_masked})` : "(Not set)";
    $("#transfers-dest-label").textContent = `Saving to: ${saved.local_download_dir}`;
    showToast("Settings saved.", "success");
    if (state.ws) state.ws.close();
    loadJobs();
  } catch (err) {
    showToast(err.message, "error");
  }
});

// ==================== Bootstrap ====================

(async function init() {
  await loadSettings();
  try {
    const health = await apiJson("/api/client/health");
    if (!health.configured) {
      setConnIndicator("unconfigured");
    } else if (health.upstream) {
      setConnIndicator("connected");
    }
  } catch {}
  await loadJobs();
  await loadTransfers();
  connectWebSocket();
})();

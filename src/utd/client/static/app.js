const state = {
  activeTab: "torrents",
  sourceMode: "magnet",
  inspectorTab: "logs",
  selectedJobId: null,
  jobs: new Map(),
  jobLogs: new Map(),
  cancelledJobIds: new Set(),
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
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

// ==================== Tab Navigation ====================

function switchTab(tabName) {
  state.activeTab = tabName;
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tabName}`));
  // Panels painted lazily while hidden need one catch-up frame when they become visible.
  if (tabName === "transfers") {
    transfersView.orderDirty = true;
    dirty.transfers = true;
    dirty.telemetry = true;
    scheduleFrame();
  } else if (tabName === "torrents") {
    markJobsDirty();
    dirty.telemetry = true;
    scheduleFrame();
  }
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

// ==================== Frame Scheduler, Telemetry & Motion ====================
// A large folder transfer can push thousands of progress events per second into the GUI.
// Re-rendering every list (with its own progress bar) for each of those events is what used
// to burn a whole CPU core. All dynamic painting now funnels through a single animation
// frame: bursts of WebSocket events collapse into one DOM write per frame, hidden panels are
// skipped, unchanged rows are not touched at all, and the decorative "fun" bits are either
// compositor-only CSS or a single small 2D canvas instead of hundreds of independent CSS
// animations fighting for the same compositor layers.

const nowMs =
  typeof performance !== "undefined" && typeof performance.now === "function"
    ? () => performance.now()
    : () => Date.now();

const reducedMotionQuery =
  typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;

const uiPrefs = {
  reducedMotion: Boolean(reducedMotionQuery && reducedMotionQuery.matches),
};

const hasAnimationFrames = typeof requestAnimationFrame === "function";

const dirty = {
  jobs: false,
  transfers: false,
  telemetry: false,
  inspector: false,
  logs: false,
};

const frameLoop = {
  pending: false,
  deferred: false,
  lastTs: 0,
  frameCost: 8,
  quality: 1,
};

const paintClock = { jobs: 0, transfers: 0, inspector: 0, logs: 0 };

// Shared view-model for the PC transfer ledger. Declared here so the frame loop and the
// telemetry painter can read it without depending on script order.
const transfersView = {
  rows: new Map(),
  list: [],
  cards: new Map(),
  dirtyRows: new Set(),
  orderDirty: true,
  hidden: new Set(),
  activeCount: 0,
  summary: {
    speed: 0,
    transferred: 0,
    total: 0,
    done: 0,
    active: 0,
    queued: 0,
    failed: 0,
    eta: null,
    progress: 0,
    remaining: 0,
    count: 0,
  },
};

const motion = {
  canvas: null,
  ctx: null,
  width: 0,
  height: 0,
  dpr: 1,
  energy: 0,
  phase: 0,
  waveform: [],
  particles: [],
  bursts: [],
  spawnAcc: 0,
  milestone: 0,
  finaleDone: false,
};

function pageVisible() {
  if (typeof document === "undefined" || !document) return true;
  return document.hidden !== true && document.visibilityState !== "hidden";
}

function clamp01(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 0;
  return num < 0 ? 0 : num > 1 ? 1 : num;
}

function requestFrame(cb) {
  if (hasAnimationFrames) {
    requestAnimationFrame(cb);
  } else {
    // Non-browser / test environments: one paint per microtask, so callers never block.
    Promise.resolve().then(() => cb(nowMs()));
  }
}

function scheduleFrame() {
  if (frameLoop.pending) return;
  if (!pageVisible()) return; // rAF is frozen in background tabs anyway; repaint on return.
  frameLoop.pending = true;
  requestFrame(onFrame);
}

// Explicit "paint everything that is currently dirty" call used for discrete events such as
// cancelling a job: throttles are bypassed so the change is visible in the same tick.
function flushNow() {
  frameLoop.pending = false;
  flushDirty(nowMs(), true);
}

function onFrame(ts) {
  frameLoop.pending = false;
  const t = typeof ts === "number" && ts > 0 ? ts : nowMs();
  const started = nowMs();
  const delta = frameLoop.lastTs > 0 ? Math.min(120, t - frameLoop.lastTs) : 16.7;
  frameLoop.lastTs = t;

  flushDirty(t);
  const keepAnimating = paintMotion(t, delta);

  const cost = nowMs() - started;
  frameLoop.frameCost = frameLoop.frameCost * 0.82 + cost * 0.18;
  // Shed decoration (never data) when the machine is struggling.
  frameLoop.quality = frameLoop.frameCost > 22 ? 0.3 : frameLoop.frameCost > 12 ? 0.65 : 1;

  if ((keepAnimating || frameLoop.deferred) && hasAnimationFrames) scheduleFrame();
  else frameLoop.lastTs = 0;
}

function flushDirty(t, force = false) {
  // Paints are keyed on what is actually on screen: a hidden panel keeps its dirty flag and
  // catches up in a single frame when the user switches to it (see switchTab).
  let deferred = false;
  if (dirty.telemetry) {
    dirty.telemetry = false;
    paintTelemetry();
  }
  if (dirty.jobs) {
    if (state.activeTab === "torrents") {
      dirty.jobs = false;
      paintClock.jobs = t;
      paintJobs();
    }
  }
  if (dirty.transfers && state.activeTab === "transfers") {
    // Throttled to ~12 repaints/s; the pulse canvas (drawn below) keeps motion at 60fps
    // so the UI still feels alive between data-driven row updates.
    if (force || t - paintClock.transfers >= (uiPrefs.reducedMotion ? 140 : 80)) {
      dirty.transfers = false;
      paintClock.transfers = t;
      paintTransfers(t);
    } else {
      deferred = true;
    }
  }
  if (dirty.inspector) {
    dirty.inspector = false;
    paintInspector();
  }
  if (dirty.logs) {
    if (force || t - paintClock.logs >= 220) {
      dirty.logs = false;
      paintClock.logs = t;
      paintLogs();
    } else {
      deferred = true;
    }
  }
  frameLoop.deferred = deferred;
}

function setText(el, value) {
  if (!el) return;
  const next = String(value ?? "");
  if (el.textContent === next) return;
  el.textContent = next;
}

function setHtml(el, html) {
  if (!el) return;
  const next = String(html ?? "");
  if (el.innerHTML === next) return;
  el.innerHTML = next;
}

function setProgress(el, ratio) {
  if (!el || !el.style || typeof el.style.setProperty !== "function") return;
  el.style.setProperty("--progress", clamp01(ratio).toFixed(4));
}

function paintTelemetry() {
  const jobs = Array.from(state.jobs.values()).filter((job) => !isJobCancelled(job));
  let dl = 0;
  let ul = 0;
  let active = 0;
  for (const job of jobs) {
    dl += Number(job.download_speed || 0);
    ul += Number(job.upload_speed || 0);
    if (!["completed", "cancelled", "error"].includes(job.status)) active += 1;
  }
  setText($("#total-dl-speed"), formatSpeed(dl));
  setText($("#total-ul-speed"), formatSpeed(ul));
  setText($("#active-jobs-count"), String(active));
  setText($("#badge-jobs"), String(state.jobs.size));
  setText($("#badge-transfers"), String(transfersView.activeCount));
}

// --------------------- Motion canvas (single, cheap, GPU-friendly) ---------------------

function initTransferMotion() {
  const canvas = $("#transfers-motion");
  if (!canvas || typeof canvas.getContext !== "function") return;
  let ctx = null;
  try {
    ctx = canvas.getContext("2d", { alpha: true });
  } catch {
    ctx = null;
  }
  if (!ctx) return;
  motion.canvas = canvas;
  motion.ctx = ctx;
  resizeMotionCanvas();

  if (typeof window !== "undefined" && typeof window.ResizeObserver === "function") {
    const observer = new window.ResizeObserver(() => {
      resizeMotionCanvas();
      dirty.transfers = true;
      scheduleFrame();
    });
    observer.observe(canvas);
  } else if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("resize", () => {
      resizeMotionCanvas();
      dirty.transfers = true;
      scheduleFrame();
    });
  }
  dirty.transfers = true;
  scheduleFrame();
}

function resizeMotionCanvas() {
  const canvas = motion.canvas;
  const ctx = motion.ctx;
  if (!canvas || !ctx) return;
  const rect = typeof canvas.getBoundingClientRect === "function" ? canvas.getBoundingClientRect() : null;
  const cssWidth = Math.max(240, Math.round((rect && rect.width) || canvas.clientWidth || 640));
  const cssHeight = Math.max(60, Math.round((rect && rect.height) || canvas.clientHeight || 92));
  const dpr = Math.min(1.5, (typeof window !== "undefined" && window.devicePixelRatio) || 1);
  if (cssWidth === motion.width && cssHeight === motion.height && dpr === motion.dpr) return;
  motion.width = cssWidth;
  motion.height = cssHeight;
  motion.dpr = dpr;
  canvas.width = Math.round(cssWidth * dpr);
  canvas.height = Math.round(cssHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const points = Math.min(150, Math.max(48, Math.round(cssWidth / 9)));
  motion.waveform = new Array(points).fill(0.5);
}

function spawnBurst(xRatio, yRatio, count) {
  if (!motion.ctx || uiPrefs.reducedMotion) return;
  const budget = Math.round(48 * frameLoop.quality);
  for (let i = 0; i < count && motion.bursts.length < budget; i += 1) {
    const angle = Math.random() * Math.PI * 2;
    const speed = 0.05 + Math.random() * 0.22;
    motion.bursts.push({
      x: clamp01(xRatio) * motion.width,
      y: clamp01(yRatio) * motion.height,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed - 0.06,
      life: 1,
      size: 1.4 + Math.random() * 1.8,
      hue: Math.random() < 0.5 ? "45,212,191" : "96,165,250",
    });
  }
}

function maybeCelebrate(summary) {
  if (!motion.ctx) return;
  const pct = summary.progress * 100;
  if (pct < 1) {
    motion.milestone = 0;
    motion.finaleDone = false;
    return;
  }
  if (summary.active === 0 && summary.done > 0 && pct >= 99.5) {
    if (!motion.finaleDone) {
      motion.finaleDone = true;
      spawnBurst(0.5, 0.52, 44);
      dirty.transfers = true;
      scheduleFrame();
      if (typeof setTimeout === "function") {
        setTimeout(() => {
          motion.finaleDone = false;
          dirty.transfers = true;
          scheduleFrame();
        }, 4200);
      }
    }
    return;
  }
  const reached = Math.floor(pct / 10) * 10;
  if (reached > motion.milestone && pct < 99.5) {
    motion.milestone = reached;
    spawnBurst(0.1 + Math.random() * 0.16, 0.55, 14);
    dirty.transfers = true;
    scheduleFrame();
  } else if (reached < motion.milestone) {
    motion.milestone = reached;
  }
}

function paintMotion(t, delta) {
  const ctx = motion.ctx;
  if (!ctx) return false;
  if (!pageVisible() || state.activeTab !== "transfers") return false;

  const summary = transfersView.summary;
  const hasWork = summary.active > 0;
  const targetEnergy = hasWork ? clamp01(0.2 + Math.log1p(summary.speed / 131072) / 3.2) : 0;
  const ease = Math.min(1, Math.max(0.05, delta / 220));
  motion.energy += (targetEnergy - motion.energy) * ease;
  // Snap the tail to zero: an idle GUI must go fully quiet instead of glowing faintly forever.
  if (!hasWork && motion.energy < 0.04) motion.energy = 0;

  drawMotionScene(t, delta);
  return hasWork || motion.bursts.length > 0 || motion.energy > 0;
}

function drawMotionScene(t, delta) {
  const ctx = motion.ctx;
  const w = motion.width;
  const h = motion.height;
  if (!ctx || w <= 0 || h <= 0) return;
  const points = motion.waveform.length;
  const mid = h * 0.56;
  const amp = (0.1 + motion.energy * 0.62) * (h * 0.42);
  const reduced = uiPrefs.reducedMotion;
  const step = reduced ? 0 : delta;

  ctx.clearRect(0, 0, w, h);

  // Faint guide rails (two strokes, no per-point work).
  ctx.strokeStyle = "rgba(143, 155, 179, 0.14)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(w, mid);
  ctx.stroke();

  // Analytic waveform: cheap, deterministic and always "alive" even at a steady bitrate.
  motion.phase += step * (0.00004 + motion.energy * 0.00042);
  const p = motion.phase * 60;
  for (let i = 0; i < points; i += 1) {
    const x = i / (points - 1);
    const wave =
      Math.sin(x * 8.4 + p) * 0.42 +
      Math.sin(x * 21.7 - p * 1.45) * 0.2 +
      Math.sin(x * 3.1 - p * 0.55) * 0.3;
    const envelope = Math.sin(Math.PI * x);
    motion.waveform[i] = mid - wave * amp * envelope;
  }

  // Area fill under the curve.
  const areaGradient = ctx.createLinearGradient(0, 0, 0, h);
  areaGradient.addColorStop(0, "rgba(79, 140, 255, 0.34)");
  areaGradient.addColorStop(1, "rgba(45, 212, 191, 0.02)");
  ctx.beginPath();
  ctx.moveTo(0, motion.waveform[0]);
  for (let i = 1; i < points; i += 1) ctx.lineTo((i / (points - 1)) * w, motion.waveform[i]);
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = areaGradient;
  ctx.fill();

  // The pulse line itself.
  const lineGradient = ctx.createLinearGradient(0, 0, w, 0);
  lineGradient.addColorStop(0, "rgba(79, 140, 255, 0.25)");
  lineGradient.addColorStop(0.55, "rgba(96, 165, 250, 0.95)");
  lineGradient.addColorStop(1, "rgba(45, 212, 191, 0.95)");
  ctx.beginPath();
  ctx.moveTo(0, motion.waveform[0]);
  for (let i = 1; i < points; i += 1) ctx.lineTo((i / (points - 1)) * w, motion.waveform[i]);
  ctx.strokeStyle = lineGradient;
  ctx.lineWidth = 1.8;
  ctx.stroke();

  // Head comet.
  const headX = w;
  const headY = motion.waveform[points - 1];
  ctx.beginPath();
  ctx.arc(headX - 1, headY, 2.4 + motion.energy * 2.2, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(45, 212, 191, 0.9)";
  ctx.fill();

  if (!reduced) {
    drawFlowParticles(ctx, w, h, delta);
    drawBursts(ctx, delta);
  }
  void t;
}

function drawFlowParticles(ctx, w, h, delta) {
  const maxParticles = Math.round(34 * frameLoop.quality);
  if (motion.energy > 0.03) {
    motion.spawnAcc += motion.energy * delta * 0.05;
    while (motion.spawnAcc >= 1 && motion.particles.length < maxParticles) {
      motion.spawnAcc -= 1;
      motion.particles.push({
        x: Math.random() * w * 0.35,
        y: h * (0.4 + Math.random() * 0.3),
        vx: (0.14 + Math.random() * 0.3) * (0.4 + motion.energy) * (delta || 16),
        size: 0.9 + Math.random() * 1.5,
        life: 1,
      });
    }
  }
  const keep = [];
  for (const particle of motion.particles) {
    particle.x += particle.vx;
    particle.life -= delta * 0.0016;
    if (particle.x > w + 8 || particle.life <= 0) continue;
    keep.push(particle);
    ctx.beginPath();
    ctx.arc(particle.x, particle.y, particle.size, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(147, 197, 253, ${(0.55 * particle.life).toFixed(3)})`;
    ctx.fill();
  }
  motion.particles = keep;
}

function drawBursts(ctx, delta) {
  if (motion.bursts.length === 0) return;
  const keep = [];
  for (const spark of motion.bursts) {
    spark.x += spark.vx * delta;
    spark.y += spark.vy * delta;
    spark.vy += 0.00022 * delta;
    spark.life -= delta * 0.0019;
    if (spark.life <= 0) continue;
    keep.push(spark);
    ctx.beginPath();
    ctx.arc(spark.x, spark.y, spark.size, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(${spark.hue}, ${(0.85 * spark.life).toFixed(3)})`;
    ctx.fill();
  }
  motion.bursts = keep;
}

if (reducedMotionQuery && typeof reducedMotionQuery.addEventListener === "function") {
  reducedMotionQuery.addEventListener("change", (ev) => {
    uiPrefs.reducedMotion = Boolean(ev && ev.matches);
    dirty.transfers = true;
    dirty.telemetry = true;
    scheduleFrame();
  });
}

if (typeof document !== "undefined" && document && typeof document.addEventListener === "function") {
  document.addEventListener("visibilitychange", () => {
    if (!pageVisible()) return;
    dirty.jobs = true;
    dirty.transfers = true;
    dirty.telemetry = true;
    scheduleFrame();
  });
}

// ------------------------------- Torrent jobs ledger -------------------------------

function isJobCancelled(job) {
  return Boolean(job && (job.status === "cancelled" || state.cancelledJobIds.has(job.job_id)));
}

function removeJob(jobId) {
  if (!jobId) return;
  const hadJob = state.jobs.delete(jobId);
  state.jobLogs.delete(jobId);
  if (state.selectedJobId === jobId) {
    state.selectedJobId = null;
    if (state.sse) {
      state.sse.close();
      state.sse = null;
    }
    renderInspector();
  }
  if (hadJob) {
    // Discrete, user-visible event (cancel/remove): paint it right away instead of waiting
    // for the next telemetry frame. Everything else keeps its existing DOM node.
    markJobsDirty();
    dirty.telemetry = true;
    flushNow();
  }
}

function upsertJob(job) {
  if (!job || !job.job_id) return;
  if (isJobCancelled(job)) {
    removeJob(job.job_id);
    return;
  }
  const existing = state.jobs.get(job.job_id);
  const merged = existing ? { ...existing, ...job } : job;
  if (Array.isArray(job.logs) && job.logs.length > 0) {
    const logsMap = state.jobLogs.get(job.job_id) || new Map();
    for (const l of job.logs) logsMap.set(l.seq, l);
    state.jobLogs.set(job.job_id, logsMap);
    if (state.selectedJobId === job.job_id) dirty.logs = true;
  }
  state.jobs.set(job.job_id, merged);
  markJobsDirty(job.job_id);
  dirty.telemetry = true;
  if (state.selectedJobId === job.job_id) {
    dirty.inspector = true;
  }
  // Terminal transitions are rare and worth showing immediately; live progress is not.
  if (["completed", "error"].includes(merged.status)) flushNow();
  else scheduleFrame();
}

function appendLog(jobId, logEvt) {
  if (!jobId || !logEvt || state.cancelledJobIds.has(jobId) || !state.jobs.has(jobId)) return;
  const logsMap = state.jobLogs.get(jobId) || new Map();
  logsMap.set(logEvt.seq, logEvt);
  if (logsMap.size > 600) {
    const oldest = logsMap.keys().next().value;
    logsMap.delete(oldest);
  }
  state.jobLogs.set(jobId, logsMap);
  if (state.selectedJobId === jobId && state.inspectorTab === "logs") {
    // Log floods are coalesced like everything else: one console paint per ~220 ms.
    dirty.logs = true;
    scheduleFrame();
  }
}

function updateAggregateMetrics() {
  dirty.telemetry = true;
  scheduleFrame();
}

$("#jobs-list").addEventListener("click", async (e) => {
  const actBtn = e.target.closest("button[data-act]");
  if (actBtn) {
    e.stopPropagation();
    if (actBtn.disabled) return;
    const { act, id } = actBtn.dataset;
    actBtn.disabled = true;
    try {
      await handleJobAction(act, id);
    } finally {
      if (actBtn.isConnected) actBtn.disabled = false;
    }
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
      removeJob(jobId);
      return;
    }
    if (action === "cancel") {
      const prevJob = state.jobs.get(jobId);
      const prevLogs = state.jobLogs.get(jobId);
      const wasSelected = state.selectedJobId === jobId;
      state.cancelledJobIds.add(jobId);
      removeJob(jobId);
      try {
        await apiJson(`/api/jobs/${jobId}/cancel`, { method: "POST" });
      } catch (err) {
        if (err.status === 404) {
          return;
        }
        state.cancelledJobIds.delete(jobId);
        if (prevLogs) {
          state.jobLogs.set(jobId, prevLogs);
        }
        if (prevJob) {
          upsertJob(prevJob);
          if (wasSelected) {
            await selectJob(jobId);
          }
        } else {
          await loadJobs();
        }
        throw err;
      }
      return;
    }
    const updated = await apiJson(`/api/jobs/${jobId}/${action}`, { method: "POST" });
    upsertJob(updated);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function selectJob(jobId) {
  if (!jobId || state.cancelledJobIds.has(jobId)) return;
  const previousId = state.selectedJobId;
  state.selectedJobId = jobId;
  if (previousId && previousId !== jobId) markJobsDirty(previousId);
  markJobsDirty(jobId);
  let full = state.jobs.get(jobId);
  try {
    full = await apiJson(`/api/jobs/${jobId}`);
    if (isJobCancelled(full)) {
      removeJob(jobId);
      return;
    }
    if (state.selectedJobId !== jobId) return;
    upsertJob(full);
  } catch {
    if (state.selectedJobId !== jobId) return;
    renderInspector();
  }
  if (state.selectedJobId !== jobId || !state.jobs.has(jobId)) {
    return;
  }
  if (full && !["completed", "cancelled", "error"].includes(full.status)) {
    openJobSseFallback(jobId);
  } else if (state.sse) {
    state.sse.close();
    state.sse = null;
  }
}

const jobsView = { cards: new Map(), dirty: new Set(), order: "" };

function jobActionButtons(job) {
  const isTerminal = ["completed", "error"].includes(job.status);
  const isPaused = job.status === "paused";
  const isSeeding = job.status === "seeding";
  const actionButtons = [];
  if (!isTerminal && !isPaused) {
    actionButtons.push(`<button class="btn btn-ghost btn-xs" data-act="pause" data-id="${job.job_id}">Pause</button>`);
  }
  if (isPaused || job.status === "error") {
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
  return actionButtons.join("");
}

function createJobCard(job) {
  if (typeof document === "undefined" || !document || typeof document.createElement !== "function") {
    return null;
  }
  const el = document.createElement("div");
  el.className = "job-card";
  el.dataset.jobId = job.job_id;
  el.innerHTML = `
    <div class="job-top">
      <div class="job-title"></div>
      <span class="status-pill"></span>
    </div>
    <div class="progress-track">
      <div class="progress-fill"></div>
    </div>
    <div class="job-stats mono">
      <span class="js-bytes"></span>
      <span class="js-speed"></span>
      <span class="js-peers"></span>
    </div>
    <div class="job-error hidden"></div>
    <div class="job-actions"></div>`;
  return el;
}

function upsertJobCard(card, job) {
  card.classList.toggle("selected", job.job_id === state.selectedJobId);
  card.classList.toggle("is-live", ["downloading", "metadata", "seeding"].includes(job.status));

  // Progress is a compositor-only transform (see .progress-fill) instead of a width reflow.
  setProgress(card.querySelector(".progress-fill"), Number(job.progress || 0) / 100);

  const title = card.querySelector(".job-title");
  const titleText = job.name || "";
  setText(title, titleText);
  if (title && title.title !== titleText) title.title = titleText;

  const pill = card.querySelector(".status-pill");
  if (pill) {
    const nextClass = `status-pill status-${job.status}`;
    if (pill.className !== nextClass) pill.className = nextClass;
    setText(pill, job.status);
  }

  setText(
    card.querySelector(".js-bytes"),
    `${Number(job.progress || 0).toFixed(1)}% · ${formatBytes(job.completed_bytes)} / ${formatBytes(job.total_bytes)}`
  );
  setText(
    card.querySelector(".js-speed"),
    `↓ ${formatSpeed(job.download_speed)} · ↑ ${formatSpeed(job.upload_speed)}`
  );
  setText(
    card.querySelector(".js-peers"),
    `Peers ${job.connections || 0} (Seeds ${job.seeders || 0}) · ETA ${formatEta(job.eta_seconds)}`
  );

  const errorEl = card.querySelector(".job-error");
  if (errorEl) {
    if (job.error) {
      errorEl.classList.remove("hidden");
      setText(errorEl, job.error);
    } else {
      errorEl.classList.add("hidden");
    }
  }

  const actions = card.querySelector(".job-actions");
  setHtml(actions, jobActionButtons(job));
}

function pruneJobsView(keepIds) {
  for (const [id, card] of jobsView.cards) {
    if (!keepIds.has(id)) {
      if (card && typeof card.remove === "function") card.remove();
      jobsView.cards.delete(id);
    }
  }
}

function paintJobs() {
  const container = $("#jobs-list");
  if (!container) return;
  const sorted = Array.from(state.jobs.values())
    .filter((job) => !isJobCancelled(job))
    .sort((a, b) => b.created_at - a.created_at);

  if (sorted.length === 0) {
    pruneJobsView(new Set());
    jobsView.order = "";
    jobsView.dirty.clear();
    setHtml(
      container,
      '<div class="empty-state">No torrents yet. Paste a magnet link or drop a .torrent file above.</div>'
    );
    return;
  }

  if (container.querySelector(".empty-state")) container.innerHTML = "";

  const signature = sorted.map((job) => job.job_id).join(",");
  const reorder = signature !== jobsView.order;
  const keepIds = new Set(sorted.map((job) => job.job_id));
  if (Array.from(jobsView.cards.keys()).some((id) => !keepIds.has(id))) {
    pruneJobsView(keepIds);
  }

  const touchAll = reorder || jobsView.dirty.size === 0;
  for (const job of sorted) {
    let card = jobsView.cards.get(job.job_id);
    if (!card) {
      card = createJobCard(job);
      if (!card) return; // headless environment without DOM construction
      jobsView.cards.set(job.job_id, card);
      upsertJobCard(card, job);
      continue;
    }
    // Only rows that received new telemetry (or a whole-list change) are re-serialised.
    if (touchAll || jobsView.dirty.has(job.job_id)) upsertJobCard(card, job);
  }
  jobsView.dirty.clear();

  // One structural re-append for the whole ledger, and only when the ordering changed.
  // (Appending nodes to a fragment detaches them, so this must never run on a plain tick.)
  if (reorder && typeof document !== "undefined" && document && document.createDocumentFragment) {
    const frag = document.createDocumentFragment();
    for (const job of sorted) frag.appendChild(jobsView.cards.get(job.job_id));
    container.appendChild(frag);
    jobsView.order = signature;
  }
}

function markJobsDirty(jobId) {
  if (jobId) jobsView.dirty.add(jobId);
  else jobsView.order = "";
  dirty.jobs = true;
  scheduleFrame();
}

function renderJobs() {
  markJobsDirty();
}

// ------------------------------- Inspector -------------------------------
// Split into two independent regions so a progress tick never re-serialises the file list
// (which could be hundreds of rows) and a new log line never re-serialises it either.

const inspectorView = {
  jobKey: "",
  filesHtml: "",
  logRendered: "",
  logTail: new Map(),
  placeholder: false,
};

function renderInspector() {
  dirty.inspector = true;
  scheduleFrame();
}

function paintInspector() {
  const job = state.jobs.get(state.selectedJobId);
  if (!job) {
    setText($("#inspector-title"), "Live Inspector");
    setText($("#inspector-subtitle"), "Select a job to inspect live container logs & files");
    setHtml($("#log-console"), "");
    setHtml($("#inspector-files-list"), '<div class="empty-state">No job selected.</div>');
    inspectorView.filesHtml = "";
    inspectorView.jobKey = "";
    inspectorView.logRendered = "";
    inspectorView.placeholder = false;
    return;
  }
  const key = `${job.job_id}|${job.name}|${job.status}|${job.output_dir}|${job.info_hash || ""}`;
  if (key !== inspectorView.jobKey) {
    inspectorView.jobKey = key;
    setText($("#inspector-title"), job.name);
    setText(
      $("#inspector-subtitle"),
      `job:${job.job_id} · out:/${job.output_dir}${job.info_hash ? ` · ${job.info_hash}` : ""}`
    );
  }
  paintInspectorFiles(job);
  paintLogs();
}

function paintInspectorFiles(job) {
  const listEl = $("#inspector-files-list");
  if (!listEl || !job) return;
  let html;
  if (!job.files || job.files.length === 0) {
    html = '<div class="empty-state">Metadata pending or no file list yet.</div>';
  } else {
    html = job.files
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
  if (html !== inspectorView.filesHtml) {
    inspectorView.filesHtml = html;
    listEl.innerHTML = html;
  }
}

// Backwards-compatible name used by inspector code paths/tests: painting is deferred to
// the shared frame loop, so this is just an alias for the scheduled file-list paint.
function renderInspectorFiles() {
  dirty.inspector = true;
  scheduleFrame();
}

function logLineHtml(l) {
  return `\n    <div class="log-line"><span class="log-ts">${formatTime(l.ts)}</span><span class="log-lvl-${l.level}">[${l.level}]</span><span>${escapeHtml(l.message)}</span></div>`;
}

function logLineVisible(l, minRank, search) {
  const rank = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };
  if ((rank[l.level] ?? 1) < minRank) return false;
  if (!search) return true;
  return l.message.toLowerCase().includes(search) || l.source.toLowerCase().includes(search);
}

function paintLogs() {
  const consoleEl = $("#log-console");
  if (!consoleEl) return;
  const jobId = state.selectedJobId;
  const logsMap = jobId ? state.jobLogs.get(jobId) : null;
  const view = inspectorView;
  if (!logsMap || logsMap.size === 0) {
    if (!view.placeholder) {
      view.placeholder = true;
      consoleEl.innerHTML = '<div class="muted">Waiting for container logs...</div>';
    }
    return;
  }

  const levelFilter = $("#log-level-filter") ? $("#log-level-filter").value : "ALL";
  const search = $("#log-search") ? $("#log-search").value.trim().toLowerCase() : "";
  const rank = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };
  const minRank = levelFilter === "ALL" ? 0 : rank[levelFilter] || 0;
  const filterKey = `${jobId}|${levelFilter}|${search}`;

  let tail = 0;
  for (const seq of logsMap.keys()) if (seq > tail) tail = seq;

  const prevTail = view.logTail.get(jobId);
  const canAppend =
    view.logRendered === filterKey && prevTail !== undefined && tail > prevTail;

  if (canAppend) {
    // Fast path: only serialise the handful of lines that actually arrived.
    let appended = "";
    for (let seq = prevTail + 1; seq <= tail; seq += 1) {
      const line = logsMap.get(seq);
      if (line && logLineVisible(line, minRank, search)) appended += logLineHtml(line);
    }
    view.logTail.set(jobId, tail);
    if (appended) {
      if (view.placeholder) {
        view.placeholder = false;
        consoleEl.innerHTML = appended;
      } else if (typeof consoleEl.insertAdjacentHTML === "function") {
        consoleEl.insertAdjacentHTML("beforeend", appended);
      } else {
        consoleEl.innerHTML = consoleEl.innerHTML + appended;
      }
      if ($("#log-autoscroll").checked) consoleEl.scrollTop = consoleEl.scrollHeight;
    }
    return;
  }

  // Slow path: filter change, job switch, or the log ring buffer was compacted.
  const lines = Array.from(logsMap.values())
    .sort((a, b) => a.seq - b.seq)
    .filter((l) => logLineVisible(l, minRank, search));
  view.placeholder = false;
  consoleEl.innerHTML = lines.map(logLineHtml).join("");
  view.logRendered = filterKey;
  view.logTail.set(jobId, tail);
  if ($("#log-autoscroll").checked) {
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

function renderLogsConsole() {
  dirty.logs = true;
  scheduleFrame();
}

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
    for (const j of jobs) {
      if (isJobCancelled(j)) {
        state.jobLogs.delete(j.job_id);
      } else {
        upsertJob(j);
      }
    }
    if (state.selectedJobId && !state.jobs.has(state.selectedJobId)) {
      state.selectedJobId = null;
      if (state.sse) {
        state.sse.close();
        state.sse = null;
      }
    }
    markJobsDirty();
    dirty.telemetry = true;
    dirty.inspector = true;
    flushNow();
    if (!state.selectedJobId && state.jobs.size > 0) {
      const newest = Array.from(state.jobs.values()).sort((a, b) => b.created_at - a.created_at)[0];
      if (newest) selectJob(newest.job_id);
    }
  } catch {
    // Ignore when unconfigured
  }
}// ==================== WebSocket + SSE Live Streaming ====================

function openJobSseFallback(jobId, force = false) {
  if (state.sse) {
    state.sse.close();
    state.sse = null;
  }
  if (!jobId || state.cancelledJobIds.has(jobId)) return;
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
    // Incoming telemetry is folded into the view-model first; the single frame loop above
    // decides what (if anything) actually needs to touch the DOM.
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
    for (const t of items) ingestTransfer(t);
    showToast(`Started ${items.length} accelerated PC transfer(s).`, "success");
    switchTab("transfers");
  } catch (err) {
    showToast(err.message, "error");
  }
}

function upsertTransfer(t) {
  if (!t || !t.transfer_id) return;
  ingestTransfer(t);
}

const ACTIVE_TRANSFER_STATUSES = new Set(["queued", "transferring"]);

// Coalesce a burst of transfer events into the shared view-model, then ask for one paint.
// Nothing here touches the DOM directly: thousands of events in a second become a single
// frame of work (and often zero work, because only values that changed are written).

function ingestTransfer(t) {
  if (!t || !t.transfer_id) return;
  if (t.status === "cancelled") {
    const prevRow = transfersView.rows.get(t.transfer_id);
    transfersView.rows.delete(t.transfer_id);
    transfersView.list = transfersView.list.filter((row) => row.transfer_id !== t.transfer_id);
    transfersView.hidden.add(t.transfer_id);
    if (prevRow && ACTIVE_TRANSFER_STATUSES.has(prevRow.status)) transfersView.activeCount -= 1;
    transfersView.orderDirty = true;
    dirty.transfers = true;
    dirty.telemetry = true;
    scheduleFrame();
    return;
  }
  if (!state.transfers.has(t.transfer_id) && transfersView.hidden.has(t.transfer_id)) return;

  const prev = transfersView.rows.get(t.transfer_id);
  const prevStatus = prev ? prev.status : null;
  const row = prev ? Object.assign(prev, t) : { ...t };
  transfersView.rows.set(row.transfer_id, row);
  state.transfers.set(row.transfer_id, row);
  transfersView.dirtyRows.add(row.transfer_id);

  // Per-event work is O(1): a status transition is the only thing that changes ordering,
  // counts, or the celebration state. Progress ticks just mark the row dirty.
  if (prevStatus !== row.status) {
    if (!prev) {
      transfersView.list = Array.from(transfersView.rows.values()).sort(
        (a, b) => (b.created_at || 0) - (a.created_at || 0)
      );
      transfersView.orderDirty = true;
    }
    if (ACTIVE_TRANSFER_STATUSES.has(row.status)) transfersView.activeCount += 1;
    if (prevStatus && ACTIVE_TRANSFER_STATUSES.has(prevStatus)) transfersView.activeCount -= 1;
    if (transfersView.activeCount < 0) transfersView.activeCount = 0;
    dirty.telemetry = true;
    maybeCelebrate(computeTransferSummary());
  }
  dirty.transfers = true;
  scheduleFrame();
}

async function loadTransfers() {
  try {
    const list = await apiJson("/api/transfers");
    state.transfers.clear();
    transfersView.rows.clear();
    transfersView.list = [];
    transfersView.hidden.clear();
    for (const t of list) {
      if (t.status === "cancelled") continue;
      transfersView.rows.set(t.transfer_id, { ...t });
      state.transfers.set(t.transfer_id, transfersView.rows.get(t.transfer_id));
    }
    transfersView.list = Array.from(transfersView.rows.values()).sort(
      (a, b) => (b.created_at || 0) - (a.created_at || 0)
    );
    transfersView.activeCount = transfersView.list.filter((row) =>
      ACTIVE_TRANSFER_STATUSES.has(row.status)
    ).length;
    transfersView.summary = computeTransferSummary();
    transfersView.orderDirty = true;
    dirty.telemetry = true;
    dirty.transfers = true;
    scheduleFrame();
  } catch {}
}

function renderTransfers() {
  dirty.transfers = true;
  scheduleFrame();
}

function computeTransferSummary() {
  const rows = transfersView.list;
  let speed = 0;
  let transferred = 0;
  let total = 0;
  let done = 0;
  let active = 0;
  let queued = 0;
  let failed = 0;
  let remaining = 0;
  for (const row of rows) {
    transferred += Number(row.transferred_bytes || 0);
    total += Number(row.total_bytes || 0);
    if (row.status === "transferring") {
      active += 1;
      speed += Number(row.speed_bps || 0);
    } else if (row.status === "queued") {
      queued += 1;
    } else if (row.status === "completed") {
      done += 1;
    } else if (row.status === "error") {
      failed += 1;
    }
  }
  remaining = Math.max(0, total - transferred);
  return {
    speed,
    transferred,
    total,
    done,
    active,
    queued,
    failed,
    count: rows.length,
    progress: total > 0 ? transferred / total : 0,
    remaining,
    eta: active > 0 && speed > 0 ? remaining / speed : null,
  };
}

function transferRowHtml(t) {
  return `
    <div class="transfer-row-head">
      <div class="transfer-row-name">
        <strong>${escapeHtml(t.filename)}</strong>
        <div class="muted mono transfer-path">${escapeHtml(t.local_path)}</div>
      </div>
      <span class="status-pill status-${t.status === "transferring" ? "downloading" : t.status}">${escapeHtml(t.status)}</span>
    </div>
    <div class="progress-track">
      <div class="progress-fill"></div>
    </div>
    <div class="job-stats mono">
      <span class="js-bytes"></span>
      <span class="js-speed"></span>
      <div class="js-actions"></div>
    </div>
    <div class="job-error hidden"></div>`;
}

function transferActionsHtml(t) {
  if (["queued", "transferring"].includes(t.status)) {
    return `<button class="btn btn-danger btn-xs" data-tr-act="cancel" data-id="${t.transfer_id}">Cancel</button>`;
  }
  return `<button class="btn btn-ghost btn-xs" data-tr-act="open" data-path="${escapeHtml(t.local_path)}">Open Folder</button>
                 <button class="btn btn-ghost btn-xs" data-tr-act="remove" data-id="${t.transfer_id}">Clear</button>`;
}

function createTransferRow(t) {
  if (typeof document === "undefined" || !document || typeof document.createElement !== "function") {
    return null;
  }
  const el = document.createElement("div");
  el.className = "transfer-row";
  el.dataset.transferId = t.transfer_id;
  el.innerHTML = transferRowHtml(t);
  return el;
}

function upsertTransferRow(el, t) {
  el.classList.toggle("is-live", t.status === "transferring");
  el.classList.toggle("is-done", t.status === "completed");
  el.classList.toggle("is-failed", t.status === "error");

  setProgress(el.querySelector(".progress-fill"), Number(t.progress || 0) / 100);
  setText(
    el.querySelector(".js-bytes"),
    `${Number(t.progress || 0).toFixed(1)}% · ${formatBytes(t.transferred_bytes)} / ${formatBytes(t.total_bytes)}`
  );
  setText(
    el.querySelector(".js-speed"),
    t.status === "transferring" ? formatSpeed(t.speed_bps) : t.status === "completed" ? "done" : ""
  );
  setHtml(el.querySelector(".js-actions"), transferActionsHtml(t));

  const errorEl = el.querySelector(".job-error");
  if (errorEl) {
    if (t.error) {
      errorEl.classList.remove("hidden");
      setText(errorEl, t.error);
    } else {
      errorEl.classList.add("hidden");
    }
  }
  const statusEl = el.querySelector(".status-pill");
  if (statusEl) {
    const nextClass = `status-pill status-${t.status === "transferring" ? "downloading" : t.status}`;
    if (statusEl.className !== nextClass) statusEl.className = nextClass;
    setText(statusEl, t.status);
  }
}

const transferViewSignature = { last: "" };

// A ledger can hold thousands of files; only the rows near the viewport (plus the active
// ones, so the "in flight" story stays visible) get re-serialised on a telemetry frame.
function transferPaintWindow() {
  const scroller = $("#transfers-list");
  const rowHeight = 92;
  const windowRows = 24;
  let top = 0;
  if (scroller && typeof scroller.scrollTop === "number") top = scroller.scrollTop;
  const first = Math.max(0, Math.floor(top / rowHeight) - 4);
  return { first, last: first + windowRows };
}

function paintTransfers() {
  const container = $("#transfers-list");
  if (!container) return;
  const rows = transfersView.list;

  if (rows.length === 0) {
    for (const [id, card] of transfersView.cards) {
      if (typeof card.remove === "function") card.remove();
      transfersView.cards.delete(id);
    }
    setHtml(
      container,
      '<div class="empty-state">No transfers yet. Use "Save to PC" in Volume Explorer for accelerated multi-stream downloads.</div>'
    );
    transferViewSignature.last = "";
    transfersView.dirtyRows.clear();
    transfersView.orderDirty = true;
    paintTransferSummary();
    return;
  }

  if (container.querySelector(".empty-state")) container.innerHTML = "";
  const signature = rows.map((row) => row.transfer_id).join(",");
  const reorder = transfersView.orderDirty || signature !== transferViewSignature.last;
  const windowRange = transferPaintWindow();
  const touchAll = reorder && rows.length <= windowRange.last;

  for (let i = 0; i < rows.length; i += 1) {
    const t = rows[i];
    let card = transfersView.cards.get(t.transfer_id);
    if (!card) {
      card = createTransferRow(t);
      if (!card) return;
      transfersView.cards.set(t.transfer_id, card);
      upsertTransferRow(card, t);
      continue;
    }
    const inWindow = i >= windowRange.first && i < windowRange.last;
    const hot = t.status === "transferring" || transfersView.dirtyRows.has(t.transfer_id);
    if (touchAll || (inWindow && hot)) upsertTransferRow(card, t);
  }
  for (const [id, card] of transfersView.cards) {
    if (!transfersView.rows.has(id)) {
      if (typeof card.remove === "function") card.remove();
      transfersView.cards.delete(id);
    }
  }
  transfersView.dirtyRows.clear();
  transfersView.orderDirty = false;
  paintTransferSummary();

  if (reorder && typeof document !== "undefined" && document && document.createDocumentFragment) {
    // Only re-attach when the ledger actually changed shape (new/removed/finished files).
    const frag = document.createDocumentFragment();
    for (const row of rows) {
      const card = transfersView.cards.get(row.transfer_id);
      if (card) frag.appendChild(card);
    }
    container.appendChild(frag);
    transferViewSignature.last = signature;
  }
}

function paintTransferSummary() {
  const summary = computeTransferSummary();
  transfersView.summary = summary;
  setText($("#transfers-count"), String(summary.count));
  setText($("#transfers-speed"), summary.speed > 0 ? formatSpeed(summary.speed) : "idle");
  setText($("#transfers-eta"), summary.eta != null ? formatEta(Math.round(summary.eta)) : "—");
  setText(
    $("#transfers-bytes"),
    `${formatBytes(summary.transferred)} / ${formatBytes(summary.total)}`
  );
  setText($("#transfers-pct"), `${(summary.progress * 100).toFixed(1)}%`);
  setProgress($("#transfers-progress"), summary.progress);
  const card = $("#transfer-hero");
  if (card) {
    card.classList.toggle("is-active", summary.active > 0);
    card.classList.toggle("is-finished", summary.active === 0 && summary.count > 0);
    card.classList.toggle("is-idle", summary.active === 0 && summary.count === 0);
    card.classList.toggle("is-celebrating", motion.finaleDone);
  }
  setText(
    $("#transfers-status-line"),
    summary.active > 0
      ? `${summary.active} stream${summary.active === 1 ? "" : "s"} pulling${summary.queued ? ` · ${summary.queued} queued` : ""}${summary.failed ? ` · ${summary.failed} failed` : ""}`
      : summary.count > 0
        ? `Ledger settled · ${summary.done}/${summary.count} complete${summary.failed ? ` · ${summary.failed} failed` : ""}`
        : "Nothing queued — pick files in Volume Explorer and choose Save to PC."
  );
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
    transfersView.rows.delete(id);
    transfersView.list = transfersView.list.filter((row) => row.transfer_id !== id);
    transfersView.orderDirty = true;
    state.transfers.delete(id);
    transfersView.activeCount = transfersView.list.filter((row) =>
      ACTIVE_TRANSFER_STATUSES.has(row.status)
    ).length;
    dirty.telemetry = true;
    dirty.transfers = true;
    scheduleFrame();
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
  initTransferMotion();
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
  dirty.telemetry = true;
  scheduleFrame();
  connectWebSocket();
})();

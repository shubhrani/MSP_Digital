/* Furnace Process Monitor - dashboard polling & rendering.
 * No build step, no frameworks: plain fetch + DOM updates. */

const FURNACE_SLUG = document.body.dataset.furnaceSlug;
const API_BASE = `/furnace/${FURNACE_SLUG}`;

const STATUS_POLL_MS = 2000;
const HISTORY_POLL_MS = 8000;
const FRAMES_POLL_MS = 6000;
const TICK_MS = 1000;

const STAGE_LABELS = {
  scrap_charging: "Scrap Charging",
  sponge_charging: "Sponge Charging",
  tapping: "Tapping",
  IDLE: "Idle",
};

let lastStatus = null;
let lastTiltCount = 0;

function fmtClock(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

function fmtWallClock(date) {
  return date.toLocaleTimeString([], { hour12: false });
}

function stageInfo(stageName, status) {
  const events = status.events || [];
  const completed = events.find((e) => e.event === stageName);
  if (completed) {
    return { state: "complete", seconds: (parseFloat(completed.duration_minutes) || 0) * 60 };
  }
  if (status.current_process === stageName) {
    const start = status.current_process_start ? new Date(status.current_process_start) : null;
    const seconds = start ? (Date.now() - start.getTime()) / 1000 : 0;
    return { state: "active", seconds };
  }
  return { state: "pending", seconds: 0 };
}

function applyStageDOM(el, name, info, expectedRange) {
  el.classList.remove("stage--active", "stage--complete");
  if (info.state === "active") el.classList.add("stage--active");
  if (info.state === "complete") el.classList.add("stage--complete");

  const timerEl = el.querySelector("[data-timer]");
  const fillEl = el.querySelector("[data-fill]");
  timerEl.textContent = fmtClock(info.seconds);

  const maxMinutes = expectedRange ? expectedRange[1] : 60;
  const pct = Math.min(100, (info.seconds / 60 / maxMinutes) * 100);
  fillEl.style.width = `${info.state === "pending" ? 0 : pct}%`;
}

function renderStatus(status) {
  lastStatus = status;

  // Camera pill
  const dot = document.getElementById("camera-dot");
  const text = document.getElementById("camera-text");
  dot.classList.remove("is-connected", "is-down");
  if (status.model_error) {
    dot.classList.add("is-down");
    text.textContent = "Model error";
  } else if (status.camera_connected) {
    dot.classList.add("is-connected");
    text.textContent = "Live";
  } else {
    dot.classList.add("is-down");
    text.textContent = "Offline";
  }

  // Heat card
  document.getElementById("heat-id").textContent = status.heat_id || "No active heat";
  const stageBadge = document.getElementById("heat-stage");
  stageBadge.textContent = STAGE_LABELS[status.current_process] || STAGE_LABELS.IDLE;
  stageBadge.classList.toggle("is-active", status.state !== "IDLE");

  // Process line stages
  const expected = status.expected_duration_min || {};
  ["scrap_charging", "sponge_charging", "tapping"].forEach((name) => {
    const el = document.querySelector(`.stage[data-stage="${name}"]`);
    if (el) applyStageDOM(el, name, stageInfo(name, status), expected[name]);
  });

  // Connectors
  const spongeInfo = stageInfo("sponge_charging", status);
  const tappingInfo = stageInfo("tapping", status);
  const c1 = document.querySelector('[data-connector="1"]');
  const c2 = document.querySelector('[data-connector="2"]');
  c1.classList.remove("is-live", "is-done");
  c2.classList.remove("is-live", "is-done");
  if (spongeInfo.state === "active") c1.classList.add("is-live");
  if (spongeInfo.state === "complete" || tappingInfo.state !== "pending") c1.classList.add("is-done");
  if (tappingInfo.state === "active") c2.classList.add("is-live");
  if (tappingInfo.state === "complete") c2.classList.add("is-done");

  // Tilt badge
  const badge = document.getElementById("tilt-badge");
  document.getElementById("tilt-count").textContent = status.tilt_count;
  badge.classList.toggle("has-tilts", status.tilt_count > 0);
  if (status.tilt_count > lastTiltCount) {
    badge.classList.remove("just-ticked");
    // force reflow so the animation can re-trigger
    void badge.offsetWidth;
    badge.classList.add("just-ticked");
  }
  lastTiltCount = status.tilt_count;

  // Track active heat ID for frame selector
  if (status.heat_id && status.heat_id !== currentActiveHeatId) {
    currentActiveHeatId = status.heat_id;
    updateHeatSelectDropdown();
  }

  // Event log
  document.getElementById("event-log").textContent = status.last_event || "";

  renderStreams(status.all_streams);
  renderBinding(status);
  announceFaults(status.all_streams);

  renderPyro(status.all_temperatures);
  renderDebug(status);
  renderEvents(status.events);
}

function tick() {
  if (!lastStatus) return;
  const expected = lastStatus.expected_duration_min || {};
  ["scrap_charging", "sponge_charging", "tapping"].forEach((name) => {
    const el = document.querySelector(`.stage[data-stage="${name}"]`);
    if (el) applyStageDOM(el, name, stageInfo(name, lastStatus), expected[name]);
  });
}

function statusChipHTML(status) {
  const cls = status === "COMPLETE" ? "complete" : "in-progress";
  const label = status === "COMPLETE" ? "Complete" : "In progress";
  return `<span class="status-chip ${cls}">${label}</span>`;
}

function renderHistory(rows) {
  const body = document.getElementById("history-body");
  if (!rows || rows.length === 0) {
    body.innerHTML = '<tr><td colspan="7" class="history-empty">No completed heats yet</td></tr>';
    return;
  }

  // Most recent first.
  const sorted = [...rows].reverse();
  body.innerHTML = sorted
    .map((r) => {
      const start = r.start_time ? new Date(r.start_time) : null;
      const startText = start ? start.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
      const dur = r.duration_minutes ? `${Math.round(parseFloat(r.duration_minutes))}m` : "—";
      const shortId = (r.heat_id || "").replace(/^[A-Za-z]+_/, "#");
      return `<tr>
        <td><strong>${shortId}</strong></td>
        <td>${startText}</td>
        <td>${dur}</td>
        <td>${r.tilt_count || 0}</td>
        <td>${statusChipHTML(r.status)}</td>
        <td>
          <button type="button" class="btn-history-frames" onclick="selectHeatAndScroll('${r.heat_id}')" title="View process detection frames for ${shortId}">
            🖼️ Frames
          </button>
        </td>
        <td><a href="${API_BASE}/api/heats/${encodeURIComponent(r.heat_id)}/csv" class="btn-csv-link" download>CSV</a></td>
      </tr>`;
    })
    .join("");
}

function renderPyro(allTemps) {
  const grid = document.getElementById("pyro-grid");
  if (!allTemps || Object.keys(allTemps).length === 0) {
    grid.innerHTML = '<div class="pyro-empty">No PYRO data</div>';
    return;
  }
  grid.innerHTML = Object.entries(allTemps).map(([slug, t]) => {
    const hasValue = t.value != null;
    const val = hasValue ? Number(t.value).toFixed(1) : "—";
    const age = t.data_age_seconds != null ? `${t.data_age_seconds.toFixed(1)}s` : "—";
    const state = !t.connected ? "down" : (t.is_stale ? "stale" : "live");

    const errLine = t.last_error
      ? `<div class="pyro-tile__error">${t.last_error}</div>`
      : (hasValue ? "" : `<div class="pyro-tile__error">No value returned
          — check Group/Unit/_field on <code>${t.url || "?"}</code></div>`);

    return `<div class="pyro-tile is-${state}">
      <div class="pyro-tile__name">${t.furnace_name || slug} ${t.assigned_stream ? `<span class="pyro-tile__stream">&middot; ${t.assigned_stream}</span>` : ""}</div>
      <div class="pyro-tile__field">${t.field || t.pyro_field || ""} @ ${t.bucket || ""}</div>
      <div class="pyro-tile__value">${val}</div>
      <div class="pyro-tile__age">age ${age} · ${state}</div>
      ${errLine}
    </div>`;
  }).join("");
}

let _seenFaulty = new Set();
function announceFaults(streams) {
  if (!streams) return;
  streams.forEach((s) => {
    if (s.status === "faulty" && !_seenFaulty.has(s.stream_id)) {
      _seenFaulty.add(s.stream_id);
      const el = document.getElementById("event-log");
      if (el) {
        el.textContent = `⚠ ${s.stream_id} (${s.name}) lost active fire - swapped out`;
        el.style.borderColor = "var(--red)";
        setTimeout(() => { el.style.borderColor = ""; }, 8000);
      }
    }
  });
}

function renderStreams(streams) {
  const grid = document.getElementById("streams-grid");
  if (!grid) return;
  if (!streams || streams.length === 0) {
    grid.innerHTML = '<div class="streams-empty">No streams</div>';
    return;
  }
  grid.innerHTML = streams.map((s) => {
    const chip = {
      active:       ["active",  "Live"],
      spare:        ["spare",   "Spare"],
      maintenance:  ["maint",   "Maintenance"],
      faulty:       ["faulty",  "No activefire"],
      unknown:      ["unknown", "Unknown"],
    }[s.status] || ["unknown", s.status];
    return `<div class="stream-tile is-${chip[0]}">
      <div class="stream-tile__head">
        <span class="stream-tile__id">${s.stream_id}</span>
        <span class="stream-tile__chip">${chip[1]}</span>
      </div>
      <div class="stream-tile__name">${s.name}</div>
      <div class="stream-tile__meta">
        ${s.assigned_slot ? `Assigned &rarr; ${s.assigned_slot}` : "Unassigned"}
      </div>
    </div>`;
  }).join("");
}

function renderBinding(status) {
  const el = document.getElementById("heat-binding");
  if (!el) return;
  if (!status.stream) { el.textContent = "—"; return; }
  el.innerHTML = `Camera: <strong>${status.stream.name || status.stream.stream_id}</strong>
                  &middot; PYRO <code>${status.stream.pyro_field || "—"}</code>`;
}

function renderDebug(status) {
  const grid = document.getElementById("debug-grid");
  const baselines = status.pyro_baselines || {};
  const rows = [
    ["State", status.state],
    ["Heat ID", status.heat_id || "—"],
    ["Tilt count", status.tilt_count],
    ["Furnace moving", status.furnace_moving ? "yes" : "no"],
    ["Move started", status.furnace_move_start || "—"],
    ["Tap candidate", status.tapping_candidate_active ? "yes" : "no"],
    ["Candidate start", status.tapping_candidate_start || "—"],
    ["Candidate deadline", status.tapping_candidate_deadline || "—"],
    ["Last tilt end", status.last_tilt_end || "—"],
    ["Last activefire", status.last_activefire_at || "—"],
    ["Activefire timeout (min)", status.activefire_timeout_minutes],
    ["Tapping pre-roll (s)", status.tapping_pre_roll_seconds],
    ["Candidate window (min)", status.tapping_candidate_window_minutes],
    ["Scrap max (min)", status.scrap_max_minutes],
    ["Tapping max (min)", status.tapping_max_minutes],
    ["Last event", status.last_event || "—"],
  ];
  let html = rows.map(([k, v]) =>
    `<div><span>${k}</span><span>${v ?? "—"}</span></div>`).join("");
  for (const [field, val] of Object.entries(baselines)) {
    html += `<div><span>${field} @ candidate</span><span>${val ?? "—"}</span></div>`;
  }
  grid.innerHTML = html;
}

function renderEvents(events) {
  const el = document.getElementById("debug-events");
  if (!events || events.length === 0) {
    el.innerHTML = '<div class="frame-empty">No events yet</div>';
    return;
  }
  el.innerHTML = [...events].reverse().map((e) => {
    const t1 = (e.start_time || "").slice(11, 19) || "—";
    const t2 = (e.end_time || "").slice(11, 19) || "…";
    return `<div class="debug-event">
      <span class="debug-event__name">${e.event || "?"}</span>
      <span class="debug-event__time">${t1} → ${t2}</span>
      <span class="debug-event__dur">${e.duration_minutes || "—"} min</span>
    </div>`;
  }).join("");
}

// -----------------------------------------------------------------------------
// Process Detection Frames & Developer Debug Inspector
// -----------------------------------------------------------------------------
let selectedHeatId = "";             // "" means active heat; "all" means all heats; or specific heat ID
let frameViewMode = "annotated";     // "annotated" (bounding box) or "raw" (no bounding box)
let modalVisualMode = "annotated";   // "annotated" | "raw" | "split"
let modalInspectTab = "det";         // "det" | "state" | "json"
let cachedFrames = [];
let currentModalFrameIndex = -1;
let currentActiveHeatId = "";
let availableHeatsList = [];

async function pollFrameHeats() {
  try {
    const res = await fetch(`${API_BASE}/api/frames/heats`);
    if (!res.ok) return;
    availableHeatsList = await res.json() || [];
    updateHeatSelectDropdown();
  } catch (err) {
    console.error("Failed to load heats with frames", err);
  }
}

function updateHeatSelectDropdown() {
  const sel = document.getElementById("frame-heat-select");
  if (!sel) return;

  const currentChoice = selectedHeatId;
  const activeLabel = currentActiveHeatId ? `(${currentActiveHeatId.replace(/^[A-Za-z]+_/, '#')})` : '';
  let html = `<option value="">🔥 Current Heat ${activeLabel}</option>`;
  html += `<option value="all">🌐 All Heats (Combined View)</option>`;

  if (availableHeatsList.length > 0) {
    html += `<optgroup label="Past Heats">`;
    for (const h of availableHeatsList) {
      const short = (h.heat_id || "").replace(/^[A-Za-z]+_/, '#');
      const countLabel = h.frame_count ? `(${h.frame_count} frames)` : '';
      const isSel = h.heat_id === currentChoice ? 'selected' : '';
      html += `<option value="${h.heat_id}" ${isSel}>${short} ${countLabel}</option>`;
    }
    html += `</optgroup>`;
  }

  sel.innerHTML = html;
  if (currentChoice) {
    sel.value = currentChoice;
  }
}

function onHeatSelectChanged() {
  const sel = document.getElementById("frame-heat-select");
  if (!sel) return;
  selectedHeatId = sel.value;
  pollFrames();
}

function setFrameViewMode(mode) {
  frameViewMode = mode;
  const btnAnn = document.getElementById("btn-view-annotated");
  const btnRaw = document.getElementById("btn-view-raw");
  if (btnAnn) btnAnn.classList.toggle("is-active", mode === "annotated");
  if (btnRaw) btnRaw.classList.toggle("is-active", mode === "raw");
  renderFrames(cachedFrames);
}

function selectHeatAndScroll(heatId) {
  selectedHeatId = heatId;
  const sel = document.getElementById("frame-heat-select");
  if (sel) sel.value = heatId;
  pollFrames();
  const el = document.getElementById("frames-section");
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.classList.add("frames-section--highlight");
    setTimeout(() => el.classList.remove("frames-section--highlight"), 1600);
  }
}

function renderFrames(frames) {
  cachedFrames = frames || [];
  const strip = document.getElementById("frame-strip");
  const badge = document.getElementById("frames-count-badge");
  if (badge) {
    badge.textContent = `${cachedFrames.length} frame${cachedFrames.length === 1 ? '' : 's'}`;
  }

  if (!strip) return;
  if (!cachedFrames || cachedFrames.length === 0) {
    const hint = selectedHeatId ? `for ${selectedHeatId}` : "for this heat";
    strip.innerHTML = `<div class="frame-empty">No frames captured yet ${hint}</div>`;
    return;
  }

  strip.innerHTML = cachedFrames.map((f, idx) => {
    const imgUrl = (frameViewMode === "raw" ? (f.url_raw || f.url) : (f.url_annotated || f.url)) || f.url;
    const eventName = (f.event_name || "event").replace(/_/g, " ");
    const ts = (f.timestamp || "").replace("T", " ").slice(11, 19);
    const dateStr = (f.timestamp || "").slice(0, 10);
    const detCount = f.detections_count != null ? f.detections_count : (f.detections ? f.detections.length : 0);
    const tempVal = f.temperature && f.temperature.value != null ? `${Number(f.temperature.value).toFixed(0)}°C` : null;

    return `
      <div class="frame-tile" onclick="openDebugModalByIndex(${idx})" title="Click to inspect detections and debug info for ${eventName}">
        <div class="frame-tile__img-wrap">
          <img src="${imgUrl}" alt="${eventName}" loading="lazy">
          <div class="frame-tile__overlay">
            <span class="frame-tile__inspect-hint">🔍 Inspect</span>
          </div>
          <div class="frame-tile__top-badges">
            ${detCount > 0 ? `<span class="tile-badge tile-badge--det">🎯 ${detCount}</span>` : ''}
            ${tempVal ? `<span class="tile-badge tile-badge--temp">🔥 ${tempVal}</span>` : ''}
          </div>
        </div>
        <div class="frame-tile__info">
          <div class="frame-tile__label" title="${eventName}">${eventName}</div>
          <div class="frame-tile__meta">
            <span class="frame-tile__ts">${ts}</span>
            <span class="frame-tile__date">${dateStr}</span>
          </div>
        </div>
      </div>
    `;
  }).join("");
}

async function pollFrames() {
  try {
    let url = `${API_BASE}/api/frames`;
    const params = new URLSearchParams();
    if (selectedHeatId) {
      params.set("heat_id", selectedHeatId);
    }
    params.set("limit", "100");
    url += `?${params.toString()}`;

    const res = await fetch(url);
    if (res.ok) {
      renderFrames(await res.json());
    }
  } catch (err) {
    console.error("Frames poll failed", err);
  }
}

// -----------------------------------------------------------------------------
// Developer Debug Modal
// -----------------------------------------------------------------------------
function openDebugModalByIndex(index) {
  if (!cachedFrames || index < 0 || index >= cachedFrames.length) return;
  currentModalFrameIndex = index;
  openDebugModal(cachedFrames[index]);
}

function openDebugModal(f) {
  if (!f) return;
  const modal = document.getElementById("debug-modal");
  if (!modal) return;

  const eventName = f.event_name || "event";
  const heatId = f.heat_id || "—";
  const stage = f.stage || "IDLE";
  const ts = (f.timestamp || "").replace("T", " ").slice(0, 19);

  // Header info
  document.getElementById("modal-event-chip").textContent = eventName.toUpperCase();
  document.getElementById("modal-title").textContent = eventName.replace(/_/g, " ");
  document.getElementById("modal-heat-tag").textContent = `Heat: ${heatId}  |  Stage: ${stage}`;

  // Downloads
  const rawUrl = f.url_raw || f.url || "#";
  const annUrl = f.url_annotated || f.url || "#";
  const btnRaw = document.getElementById("modal-download-raw");
  const btnAnn = document.getElementById("modal-download-ann");
  if (btnRaw) btnRaw.href = rawUrl;
  if (btnAnn) btnAnn.href = annUrl;

  // Visual Images
  const splitRawImg = document.getElementById("modal-split-raw-img");
  const splitAnnImg = document.getElementById("modal-split-ann-img");
  const splitRawLink = document.getElementById("modal-split-raw-link");
  const splitAnnLink = document.getElementById("modal-split-ann-link");

  if (splitRawImg) splitRawImg.src = rawUrl;
  if (splitAnnImg) splitAnnImg.src = annUrl;
  if (splitRawLink) splitRawLink.href = rawUrl;
  if (splitAnnLink) splitAnnLink.href = annUrl;

  // Single View Image depending on modalVisualMode
  updateModalVisualDisplay(rawUrl, annUrl);

  // Telemetry Pills
  const dims = f.frame_dimensions ? `${f.frame_dimensions.width}×${f.frame_dimensions.height}` : "HD Frame";
  const detCount = f.detections_count != null ? f.detections_count : (f.detections ? f.detections.length : 0);
  const tempVal = f.temperature && f.temperature.value != null ? `${Number(f.temperature.value).toFixed(1)}°C` : "No PYRO";

  document.getElementById("pill-dimensions").textContent = `📏 ${dims}`;
  document.getElementById("pill-detections").textContent = `🎯 ${detCount} detection${detCount === 1 ? '' : 's'}`;
  document.getElementById("pill-temp").textContent = `🔥 ${tempVal}`;
  document.getElementById("pill-timestamp").textContent = `⏱️ ${ts}`;

  // Detections Panel
  document.getElementById("inspect-det-count").textContent = detCount;
  renderModalDetections(f.detections || []);

  // State & Diagnostics Panel
  renderModalDiagnostics(f);

  // Raw JSON Panel
  const jsonPre = document.getElementById("modal-json-pre");
  if (jsonPre) {
    jsonPre.querySelector("code").textContent = JSON.stringify(f, null, 2);
  }

  // Show modal
  modal.style.display = "flex";
  document.body.style.overflow = "hidden"; // Prevent background scroll
}

function updateModalVisualDisplay(rawUrl, annUrl) {
  const singleBox = document.getElementById("visual-single-box");
  const splitBox = document.getElementById("visual-split-box");
  const mainImg = document.getElementById("modal-main-img");
  const imgLink = document.getElementById("modal-img-link");

  const vtabAnn = document.getElementById("vtab-annotated");
  const vtabRaw = document.getElementById("vtab-raw");
  const vtabSplit = document.getElementById("vtab-split");

  if (vtabAnn) vtabAnn.classList.toggle("is-active", modalVisualMode === "annotated");
  if (vtabRaw) vtabRaw.classList.toggle("is-active", modalVisualMode === "raw");
  if (vtabSplit) vtabSplit.classList.toggle("is-active", modalVisualMode === "split");

  if (modalVisualMode === "split") {
    if (singleBox) singleBox.style.display = "none";
    if (splitBox) splitBox.style.display = "grid";
  } else {
    if (splitBox) splitBox.style.display = "none";
    if (singleBox) singleBox.style.display = "block";

    const targetUrl = modalVisualMode === "raw" ? rawUrl : annUrl;
    if (mainImg) mainImg.src = targetUrl;
    if (imgLink) imgLink.href = targetUrl;
  }
}

function setModalVisualMode(mode) {
  modalVisualMode = mode;
  if (currentModalFrameIndex >= 0 && cachedFrames[currentModalFrameIndex]) {
    const f = cachedFrames[currentModalFrameIndex];
    updateModalVisualDisplay(f.url_raw || f.url, f.url_annotated || f.url);
  }
}

function setModalInspectTab(tab) {
  modalInspectTab = tab;
  ["det", "state", "json"].forEach((t) => {
    const btn = document.getElementById(`itab-${t}`);
    const panel = document.getElementById(`ipanel-${t}`);
    if (btn) btn.classList.toggle("is-active", t === tab);
    if (panel) panel.classList.toggle("is-active", t === tab);
  });
}

function renderModalDetections(detections) {
  const container = document.getElementById("modal-detections-container");
  if (!container) return;

  if (!detections || detections.length === 0) {
    container.innerHTML = '<div class="inspect-empty">No objects detected on this frame</div>';
    return;
  }

  container.innerHTML = detections.map((d, i) => {
    const conf = d.confidence != null ? d.confidence : 0;
    const confPct = d.confidence_pct || `${(conf * 100).toFixed(1)}%`;
    const confColor = conf >= 0.8 ? "var(--cyan)" : (conf >= 0.5 ? "var(--amber)" : "var(--text-muted)");
    const box = d.box || [0, 0, 0, 0];
    const boxStr = `[${box.map((v) => Math.round(v)).join(", ")}]`;
    const dimsStr = `${d.width || Math.round(box[2] - box[0])} × ${d.height || Math.round(box[3] - box[1])} px`;

    return `
      <div class="det-card ${d.is_relevant ? 'is-relevant' : ''}">
        <div class="det-card__head">
          <div class="det-card__title">
            <span class="det-badge">#${i + 1}</span>
            <span class="det-name">${d.name || "Object"}</span>
            <span class="det-cls-id">class ${d.class_id}</span>
          </div>
          <div class="det-conf" style="color: ${confColor};">
            <strong>${confPct}</strong>
          </div>
        </div>
        <div class="det-conf-bar">
          <div class="det-conf-fill" style="width: ${confPct}; background: ${confColor};"></div>
        </div>
        <div class="det-card__coords">
          <span>BBox: <code>${boxStr}</code></span>
          <span>Size: <code>${dimsStr}</code></span>
          ${d.is_relevant ? '<span class="det-tag-relevant">Target Class</span>' : ''}
        </div>
      </div>
    `;
  }).join("");
}

function renderModalDiagnostics(f) {
  const table = document.getElementById("modal-diag-table");
  if (!table) return;

  const rows = [
    ["Event Name", f.event_name || "—"],
    ["Description / Reason", f.description || "—"],
    ["Stage at Capture", f.stage || "—"],
    ["Heat ID", f.heat_id || "—"],
    ["Furnace ID", f.furnace_id || "—"],
    ["Capture Timestamp", (f.timestamp || "—").replace("T", " ")],
    ["Saved At", (f.saved_at || "—").replace("T", " ")],
    ["Tilt Count", f.tilt_count ?? "—"],
    ["PYRO Temperature", f.temperature?.value != null ? `${Number(f.temperature.value).toFixed(1)}°C (${f.temperature.field || 'PYRO'})` : "None"],
    ["Camera Stream", f.stream?.stream_id ? `${f.stream.stream_id} (${f.stream.rtsp_url || ''})` : "—"],
    ["Resolution", f.frame_dimensions ? `${f.frame_dimensions.width} × ${f.frame_dimensions.height} (channels: ${f.frame_dimensions.channels || 3})` : "—"],
    ["Confirmed Classes", (f.confirmed_classes && f.confirmed_classes.length > 0)
        ? f.confirmed_classes.map((c) => `${c.name} (${c.class_id})`).join(", ") : "None"],
    ["Raw Image File", f.filename_raw || "—"],
    ["Annotated Image File", f.filename_annotated || f.filename || "—"],
  ];

  table.innerHTML = rows.map(([k, v]) => `
    <div class="diag-row">
      <div class="diag-label">${k}</div>
      <div class="diag-val">${v}</div>
    </div>
  `).join("");
}

function closeDebugModal() {
  const modal = document.getElementById("debug-modal");
  if (modal) modal.style.display = "none";
  document.body.style.overflow = "";
}

function copyModalJson() {
  if (currentModalFrameIndex < 0 || !cachedFrames[currentModalFrameIndex]) return;
  const f = cachedFrames[currentModalFrameIndex];
  const text = JSON.stringify(f, null, 2);

  navigator.clipboard.writeText(text).then(() => {
    const btn1 = document.getElementById("modal-btn-copy-json");
    const btn2 = document.getElementById("btn-copy-json-inner");
    if (btn1) { btn1.textContent = "✓ Copied!"; setTimeout(() => { btn1.textContent = "📋 Copy JSON"; }, 2000); }
    if (btn2) { btn2.textContent = "✓ Copied!"; setTimeout(() => { btn2.textContent = "📋 Copy"; }, 2000); }
  }).catch((err) => {
    console.error("Clipboard copy failed", err);
  });
}

// Keyboard shortcuts for modal
window.addEventListener("keydown", (e) => {
  const modal = document.getElementById("debug-modal");
  if (!modal || modal.style.display === "none") return;

  if (e.key === "Escape") {
    closeDebugModal();
  } else if (e.key === "ArrowLeft") {
    if (currentModalFrameIndex > 0) {
      openDebugModalByIndex(currentModalFrameIndex - 1);
    }
  } else if (e.key === "ArrowRight") {
    if (cachedFrames && currentModalFrameIndex < cachedFrames.length - 1) {
      openDebugModalByIndex(currentModalFrameIndex + 1);
    }
  }
});

async function pollStatus() {
  try {
    const res = await fetch(`${API_BASE}/api/status`);
    if (res.ok) renderStatus(await res.json());
  } catch (err) {
    console.error("Status poll failed", err);
  }
}

async function pollHistory() {
  try {
    const res = await fetch(`${API_BASE}/api/heats`);
    if (res.ok) renderHistory(await res.json());
  } catch (err) {
    console.error("History poll failed", err);
  }
}

function updateClock() {
  document.getElementById("clock").textContent = fmtWallClock(new Date());
}

updateClock();
pollStatus();
pollHistory();
pollFrameHeats();
pollFrames();

setInterval(updateClock, TICK_MS);
setInterval(tick, TICK_MS);
setInterval(pollStatus, STATUS_POLL_MS);
setInterval(pollHistory, HISTORY_POLL_MS);
setInterval(pollFrames, FRAMES_POLL_MS);
setInterval(pollFrameHeats, FRAMES_POLL_MS * 2);

// ---------------------------------------------------------------------
// Video stream self-healing
// ---------------------------------------------------------------------
// The Flask /video_feed route is already written to survive worker swaps
// (it re-reads slot.camera_worker in a loop). But the browser can still
// lose the connection on a server restart, a network blip, or a tab
// suspend. The `error` event is the browser's signal for "the image
// stream ended" - re-point the src at the same URL after a short delay
// and MJPEG picks up again.
(function setupVideoReconnect() {
  const img = document.getElementById("video");
  if (!img) return;
  img.addEventListener("error", () => {
    setTimeout(() => {
      img.src = `${API_BASE}/video_feed?t=${Date.now()}`;
    }, 2000);
  });
  // Also reconnect on tab-visibility change: browsers throttle and may
  // close the underlying connection on long-hidden tabs.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible"
        && (!img.complete || img.naturalWidth === 0)) {
      img.src = `${API_BASE}/video_feed?t=${Date.now()}`;
    }
  });
})();



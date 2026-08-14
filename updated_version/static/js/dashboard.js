/* Furnace Process Monitor - dashboard polling & rendering.
 * No build step, no frameworks: plain fetch + DOM updates. */

const FURNACE_SLUG = document.body.dataset.furnaceSlug;
const API_BASE = `/furnace/${FURNACE_SLUG}`;

const STATUS_POLL_MS = 2000;
const HISTORY_POLL_MS = 8000;
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

  // Event log
  document.getElementById("event-log").textContent = status.last_event || "";
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
    body.innerHTML = '<tr><td colspan="6" class="history-empty">No completed heats yet</td></tr>';
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
        <td>${shortId}</td>
        <td>${startText}</td>
        <td>${dur}</td>
        <td>${r.tilt_count || 0}</td>
        <td>${statusChipHTML(r.status)}</td>
        <td><a href="${API_BASE}/api/heats/${encodeURIComponent(r.heat_id)}/csv" download>CSV</a></td>
      </tr>`;
    })
    .join("");
}

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

setInterval(updateClock, TICK_MS);
setInterval(tick, TICK_MS);
setInterval(pollStatus, STATUS_POLL_MS);
setInterval(pollHistory, HISTORY_POLL_MS);

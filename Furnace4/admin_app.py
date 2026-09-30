"""
Furnace DB Admin  --  standalone CRUD web app
=============================================

A separate Flask application that talks to the SAME SQL Server database and
the SAME tables (dbo.Heats, dbo.HeatEvents) the monitor writes to.

CREDENTIALS
-----------
Both the SQL connection AND the web login use the credentials already in
config.py:

    config.SQL_SERVER    = "10.135.20.195,1433"
    config.SQL_DATABASE  = "Furnace"
    config.SQL_UID       = "sa"
    config.SQL_PASSWORD  = "Mampa#80631"

Nothing extra to configure. Just run:

    python3 admin_app.py
    open http://<host>:8001

Optional overrides via environment variables (leave unset to use the
config.py values):

    ADMIN_USERNAME   -> defaults to config.SQL_UID
    ADMIN_PASSWORD   -> defaults to config.SQL_PASSWORD
    ADMIN_PORT       -> defaults to 8001
    ADMIN_HOST       -> defaults to 0.0.0.0
    ADMIN_SECRET_KEY -> Flask session secret; random per boot if unset

UI BEHAVIOUR (changed from the previous version)
--------------------------------------------------
Create / edit / delete no longer navigate to a different page. Every list
and detail view now has:

  * a "New" button that opens a modal dialog,
  * "Edit" buttons that open the same dialog pre-filled,
  * "Delete" buttons that ask for confirmation and then remove the row,

all handled with small JSON API endpoints under /api/... and a page-local
JS refresh that re-fetches just the affected table/section (no window
navigation, no full page reload). Toast notifications replace the old
flash-message-after-redirect pattern.

WARNINGS
--------
* The monitor writes to these tables too. Editing a heat's heat_id (the
  primary key) also rewrites HeatEvents.heat_id, because no FK is declared
  in the schema. Renaming a heat that the monitor currently has open will
  make the monitor lose track of it -- only edit COMPLETE heats.
* No CSRF protection. Keep this on the plant LAN.
"""

import datetime
import logging
import os
import traceback

from flask import (
    Flask, request, redirect, url_for, flash, abort, session,
    render_template, jsonify,
)
from jinja2 import DictLoader

import config
import db


# ---------------------------------------------------------------------------
# Logging -- full tracebacks to the console on any 500
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("furnace_admin")


# ---------------------------------------------------------------------------
# Config -- web login defaults to the SQL credentials in config.py
# ---------------------------------------------------------------------------
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", config.SQL_UID)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", config.SQL_PASSWORD)

ADMIN_PORT     = int(os.getenv("ADMIN_PORT", "8001"))
ADMIN_HOST     = os.getenv("ADMIN_HOST", "0.0.0.0")
ADMIN_DEBUG    = os.getenv("ADMIN_DEBUG", "0").lower() in ("1", "true", "yes")

# If both are empty, the login prompt is skipped entirely. That only happens
# if config.SQL_UID / config.SQL_PASSWORD are also empty, which would mean
# the SQL connection itself would fail anyway.
AUTH_ENABLED = bool(ADMIN_USERNAME and ADMIN_PASSWORD)


# ---------------------------------------------------------------------------
# Templates (inline; the app is single-file by design)
# ---------------------------------------------------------------------------
_LAYOUT = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}Furnace DB Admin{% endblock %}</title>
<style>
:root{
  --bg:#0a0d11; --panel:#12161c; --raise:#171c24; --border:#232b35; --soft:#1a2029;
  --text:#edeff2; --dim:#8a93a0; --mute:#545e6b;
  --amber:#ff8a1e; --amber-s:rgba(255,138,30,.16);
  --cyan:#3fd0c9; --cyan-s:rgba(63,208,201,.14);
  --red:#e5484d; --red-s:rgba(229,72,77,.16);
  --green:#43b581; --green-s:rgba(67,181,129,.14);
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:14px;
  background-image:radial-gradient(circle at 15% 0%,rgba(255,138,30,.05),transparent 45%),
                   radial-gradient(circle at 85% 100%,rgba(63,208,201,.04),transparent 45%);}
a{color:var(--amber);text-decoration:none}
a:hover{text-decoration:underline}

header.top{display:flex;align-items:center;justify-content:space-between;
  padding:16px 28px;border-bottom:1px solid var(--soft);gap:16px;flex-wrap:wrap}
header.top .brand{display:flex;align-items:center;gap:12px}
header.top .mark{width:36px;height:36px;border-radius:8px;display:flex;
  align-items:center;justify-content:center;font-weight:700;
  background:linear-gradient(155deg,var(--amber),#c95f00);color:var(--bg)}
header.top h1{margin:0;font-size:17px;letter-spacing:.02em}
header.top p{margin:2px 0 0;color:var(--dim);font-size:12px}
nav.top-nav{display:flex;gap:6px;padding:4px;background:var(--panel);
  border:1px solid var(--soft);border-radius:999px}
nav.top-nav a{padding:7px 14px;border-radius:999px;color:var(--dim);
  font-size:12.5px;letter-spacing:.02em}
nav.top-nav a:hover{color:var(--text);text-decoration:none}
nav.top-nav a.is-active{background:linear-gradient(155deg,var(--amber),#c95f00);
  color:var(--bg)}

main{padding:22px 28px 48px;max-width:1400px;margin:0 auto}

.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px;border:1px solid transparent}
.flash.ok{background:var(--green-s);border-color:rgba(67,181,129,.4);color:#8fe2b8}
.flash.error{background:var(--red-s);border-color:rgba(229,72,77,.4);color:#f3a9ac}

.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px 20px}
.card+.card{margin-top:18px}
.card h2{margin:0 0 12px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.stat{padding:16px 18px;border:1px solid var(--border);border-radius:10px;background:var(--panel)}
.stat__num{font:600 26px var(--mono);color:var(--amber)}
.stat__lbl{color:var(--dim);font-size:12px;letter-spacing:.05em;text-transform:uppercase;margin-top:4px}

.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin-bottom:14px}
.toolbar .field{display:flex;flex-direction:column;gap:4px;min-width:140px}
.toolbar label{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute)}
.toolbar input,.toolbar select{padding:8px 10px;border-radius:6px;
  border:1px solid var(--border);background:var(--raise);color:var(--text);font-size:13px}

.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:6px;
  border:1px solid var(--border);background:var(--raise);color:var(--text);
  font-size:12.5px;cursor:pointer;font-family:inherit}
.btn:hover{border-color:var(--amber);text-decoration:none;color:var(--amber)}
.btn--primary{background:linear-gradient(155deg,var(--amber),#c95f00);
  color:var(--bg);border-color:transparent;font-weight:600}
.btn--primary:hover{color:var(--bg);filter:brightness(1.05)}
.btn--danger{color:#f3a9ac;border-color:rgba(229,72,77,.5)}
.btn--danger:hover{background:var(--red-s);color:#f3a9ac;border-color:var(--red)}
.btn--small{padding:4px 10px;font-size:11.5px}

table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--mute);padding:8px 10px;border-bottom:1px solid var(--border);
  position:sticky;top:0;background:var(--panel);z-index:1;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--soft);
  font-family:var(--mono);font-size:12.5px;color:var(--text);white-space:nowrap}
tr:hover td{background:rgba(255,138,30,.04)}
.tbl-wrap{overflow-x:auto;max-height:70vh;overflow-y:auto;
  border:1px solid var(--border);border-radius:10px}

.chip{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;
  font-family:system-ui,sans-serif;background:var(--soft);color:var(--dim)}
.chip.ok{background:var(--green-s);color:var(--green)}
.chip.warn{background:var(--amber-s);color:var(--amber)}
.chip.err{background:var(--red-s);color:var(--red)}

.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}
.field label{font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim)}
.field input,.field select,.field textarea{
  padding:9px 11px;border-radius:6px;border:1px solid var(--border);
  background:var(--raise);color:var(--text);font:inherit;font-size:13px}
.field input:focus,.field select:focus,.field textarea:focus{
  outline:none;border-color:var(--amber)}
.field .hint{font-size:11px;color:var(--mute)}
.field--full{grid-column:1/-1}

.actions{display:flex;gap:8px;justify-content:flex-end;margin-top:8px;
  padding-top:14px;border-top:1px solid var(--soft);flex-wrap:wrap}
.pager{display:flex;gap:6px;align-items:center;justify-content:center;padding:14px 0}
.pager a,.pager span{padding:5px 11px;border-radius:6px;border:1px solid var(--border);
  background:var(--raise);font-size:12.5px;color:var(--text)}
.pager .current{background:var(--amber);color:var(--bg);border-color:transparent;font-weight:600}
.pager .disabled{opacity:.4;pointer-events:none}

.empty{padding:40px;text-align:center;color:var(--mute)}
.kv{display:grid;grid-template-columns:200px 1fr;gap:2px 16px;font-size:13px}
.kv dt{color:var(--dim);font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;
  padding:6px 0;border-bottom:1px dashed var(--soft)}
.kv dd{margin:0;padding:6px 0;border-bottom:1px dashed var(--soft);
  font-family:var(--mono);word-break:break-word}
.back-link{font-size:12.5px;color:var(--dim);margin-bottom:14px;display:inline-block}

/* ---- modal + toast (new) ---- */
#modal-root{position:fixed;inset:0;z-index:50;display:none}
.modal-backdrop{position:fixed;inset:0;background:rgba(5,7,10,.72);
  display:flex;align-items:center;justify-content:center;padding:20px;overflow:auto}
.modal-panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;
  width:100%;max-width:640px;max-height:90vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal-head{display:flex;align-items:center;justify-content:space-between;
  padding:16px 20px;border-bottom:1px solid var(--soft)}
.modal-head h3{margin:0;font-size:15px}
.modal-x{background:none;border:none;color:var(--dim);font-size:22px;cursor:pointer;line-height:1}
.modal-x:hover{color:var(--text)}
.modal-body{padding:18px 20px}
.form-error{background:var(--red-s);border:1px solid rgba(229,72,77,.4);
  color:#f3a9ac;padding:8px 12px;border-radius:8px;margin-bottom:12px;font-size:12.5px}

#toast-root{position:fixed;top:18px;right:18px;z-index:60;display:flex;flex-direction:column;gap:8px}
.toast{padding:10px 16px;border-radius:8px;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.35);
  opacity:1;transform:translateX(0);transition:opacity .25s,transform .25s;max-width:320px}
.toast--ok{background:#153826;border:1px solid rgba(67,181,129,.5);color:#8fe2b8}
.toast--error{background:#3a1518;border:1px solid rgba(229,72,77,.5);color:#f3a9ac}
.toast--out{opacity:0;transform:translateX(20px)}
</style>
</head><body>

<header class="top">
  <div class="brand">
    <div class="mark">DB</div>
    <div>
      <h1>Furnace DB Admin</h1>
      <p>{{ config.SQL_SERVER }} / {{ config.SQL_DATABASE }} (login: {{ config.SQL_UID }})</p>
    </div>
  </div>
  <nav class="top-nav">
    <a href="{{ url_for('home') }}">Overview</a>
    <a href="{{ url_for('heats_list') }}">Heats</a>
    <a href="{{ url_for('events_list') }}">Events</a>
    {% if auth_enabled %}<a href="{{ url_for('logout') }}">Logout</a>{% endif %}
  </nav>
</header>

<main>
  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in msgs %}
      <div class="flash {{ cat }}">{{ msg }}</div>
    {% endfor %}
  {% endwith %}
  {% block body %}{% endblock %}
</main>

<div id="modal-root"></div>
<div id="toast-root"></div>

<script>
// ---------------------------------------------------------------------
// Field metadata mirrors HEAT_FORM_FIELDS / EVENT_FORM_FIELDS in Python.
// ---------------------------------------------------------------------
const HEAT_FIELDS = [
  {col:'heat_id',          label:'heat_id (primary key)',  kind:'text'},
  {col:'FurnaceId',        label:'FurnaceId',               kind:'text'},
  {col:'furnace',          label:'furnace (line)',          kind:'text'},
  {col:'stand',            label:'stand',                   kind:'text'},
  {col:'heat_number',      label:'heat_number',             kind:'number'},
  {col:'heat_status',      label:'heat_status',             kind:'select', options:['IN_PROGRESS','COMPLETE','ABORTED']},
  {col:'heat_start_time',  label:'heat_start_time',         kind:'datetime'},
  {col:'heat_end_time',    label:'heat_end_time',           kind:'datetime'},
  {col:'heat_duration',    label:'heat_duration (min)',     kind:'text', hint:'auto-calculated from start/end on save'},
  {col:'scrap_duration',   label:'scrap_duration (min)',    kind:'text'},
  {col:'sponge_duration',  label:'sponge_duration (min)',   kind:'text'},
  {col:'tapping_duration', label:'tapping_duration (min)',  kind:'text'},
  {col:'heat_event_match', label:'heat_event_match',        kind:'text'},
  {col:'position',         label:'position',                kind:'text'},
  {col:'operation_status', label:'operation_status',        kind:'text'},
];

const EVENT_FIELDS = [
  {col:'heat_id',          label:'heat_id',            kind:'text'},
  {col:'event_name',       label:'event_name',         kind:'text', hint:'e.g. scrap_charging'},
  {col:'FurnaceId',        label:'FurnaceId',          kind:'text', hint:'e.g. 4/1'},
  {col:'event_id',         label:'event_id (unique)',  kind:'text', hint:'leave blank for a new auto key'},
  {col:'cross_event_id',   label:'cross_event_id',     kind:'text'},
  {col:'cross_event_name', label:'cross_event_name',   kind:'text'},
  {col:'start_time',       label:'start_time',         kind:'datetime'},
  {col:'end_time',         label:'end_time',           kind:'datetime'},
  {col:'duration',         label:'duration (min)',     kind:'text', hint:'auto-calculated from start/end on save'},
  {col:'status',           label:'status',             kind:'text', hint:'COMPLETED / IN_PROGRESS'},
  {col:'confidence',       label:'confidence',         kind:'text'},
  {col:'sop_duration',     label:'sop_duration',       kind:'text'},
];

function escapeAttr(s) {
  return String(s === undefined || s === null ? '' : s)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

function fieldInputHtml(field, value, extra) {
  const val = (value === undefined || value === null) ? '' : value;
  const id = 'f_' + field.col;
  if (field.kind === 'select') {
    let opts = '<option value="">(none)</option>';
    (field.options || []).forEach(o => {
      opts += `<option value="${o}" ${String(val) === o ? 'selected' : ''}>${o}</option>`;
    });
    return `<select id="${id}" name="${field.col}">${opts}</select>`;
  }
  if (field.kind === 'datetime') {
    return `<input type="datetime-local" id="${id}" name="${field.col}" value="${escapeAttr(val)}">`;
  }
  if (field.kind === 'number') {
    return `<input type="number" id="${id}" name="${field.col}" value="${escapeAttr(val)}">`;
  }
  let listAttr = '';
  if (field.col === 'heat_id' && extra && extra.heat_id) {
    listAttr = ' list="heat-ids-datalist"';
  }
  const placeholder = field.hint ? ` placeholder="${escapeAttr(field.hint)}"` : '';
  return `<input type="text" id="${id}" name="${field.col}"${listAttr} value="${escapeAttr(val)}"${placeholder}>`;
}

function buildFormHtml(fields, values, extra) {
  let html = '<form id="modal-form" class="modal-form" novalidate>';
  html += '<div class="form-error" style="display:none"></div>';
  html += '<div class="grid2">';
  fields.forEach(f => {
    html += `<div class="field"><label>${f.label}</label>${fieldInputHtml(f, values[f.col], extra)}`;
    if (f.hint) html += `<span class="hint">${f.hint}</span>`;
    html += '</div>';
  });
  html += '</div>';
  if (extra && extra.heat_id) {
    html += '<datalist id="heat-ids-datalist">' +
      extra.heat_id.map(h => `<option value="${escapeAttr(h)}">`).join('') +
      '</datalist>';
  }
  html += '<div class="actions">' +
    '<button type="button" class="btn" data-close-modal="cancel">Cancel</button>' +
    '<button type="submit" class="btn btn--primary">Save</button></div>';
  html += '</form>';
  return html;
}

function wireDurationAutoCalc(formEl, startCol, endCol, durationCol) {
  const startEl = formEl.querySelector(`[name="${startCol}"]`);
  const endEl = formEl.querySelector(`[name="${endCol}"]`);
  const durEl = formEl.querySelector(`[name="${durationCol}"]`);
  if (!startEl || !endEl || !durEl) return;
  function recalc() {
    if (!startEl.value || !endEl.value) return;
    const start = new Date(startEl.value);
    const end = new Date(endEl.value);
    if (isNaN(start) || isNaN(end)) return;
    const mins = (end - start) / 60000;
    durEl.value = Number.isInteger(mins) ? String(mins) : mins.toFixed(1);
  }
  startEl.addEventListener('change', recalc);
  endEl.addEventListener('change', recalc);
}

function collectFormData(formEl, fields) {
  const out = {};
  fields.forEach(f => {
    const el = formEl.querySelector(`[name="${f.col}"]`);
    out[f.col] = el ? el.value : '';
  });
  return out;
}

function openModal(title, bodyHtml, onMount) {
  const root = document.getElementById('modal-root');
  root.innerHTML =
    '<div class="modal-backdrop">' +
      '<div class="modal-panel" role="dialog" aria-modal="true">' +
        '<div class="modal-head"><h3>' + title + '</h3>' +
          '<button type="button" class="modal-x" aria-label="Close">&times;</button></div>' +
        '<div class="modal-body">' + bodyHtml + '</div>' +
      '</div>' +
    '</div>';
  root.style.display = 'block';
  const backdrop = root.querySelector('.modal-backdrop');
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) closeModal(); });
  root.querySelectorAll('.modal-x, [data-close-modal="cancel"]').forEach(btn => {
    btn.addEventListener('click', closeModal);
  });
  const formEl = root.querySelector('#modal-form');
  if (onMount) onMount(formEl, root);
}

function closeModal() {
  const root = document.getElementById('modal-root');
  root.innerHTML = '';
  root.style.display = 'none';
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});

function showToast(message, type) {
  const root = document.getElementById('toast-root');
  const el = document.createElement('div');
  el.className = 'toast ' + (type === 'error' ? 'toast--error' : 'toast--ok');
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => {
    el.classList.add('toast--out');
    setTimeout(() => el.remove(), 300);
  }, 3200);
}

function showFormError(formEl, error) {
  const el = formEl.querySelector('.form-error');
  if (el) {
    el.style.display = 'block';
    el.textContent = error || 'Something went wrong.';
  }
}

async function refreshFragment(containerId, fragmentName) {
  const container = document.getElementById(containerId);
  if (!container) return;
  try {
    const res = await fetch(window.location.pathname + window.location.search, {
      headers: { 'X-Fragment': fragmentName },
    });
    if (res.ok) container.innerHTML = await res.text();
  } catch (e) {
    // network hiccup - leave the current view as-is
  }
}
function refreshHeatsResults() { return refreshFragment('heats-results', 'results'); }
function refreshEventsResults() { return refreshFragment('events-results', 'results'); }
function refreshHeatInfo() { return refreshFragment('heat-info', 'info'); }
function refreshHeatEvents() { return refreshFragment('heat-events', 'events'); }

async function openHeatModal(heatId, context) {
  let values = {};
  if (heatId) {
    const res = await fetch(`/api/heats/${encodeURIComponent(heatId)}`);
    if (!res.ok) { showToast('Could not load that heat', 'error'); return; }
    values = await res.json();
  }
  const bodyHtml = buildFormHtml(HEAT_FIELDS, values);
  openModal(heatId ? `Edit ${heatId}` : 'Create heat', bodyHtml, (formEl) => {
    wireDurationAutoCalc(formEl, 'heat_start_time', 'heat_end_time', 'heat_duration');
    formEl.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const payload = collectFormData(formEl, HEAT_FIELDS);
      const url = heatId ? `/api/heats/${encodeURIComponent(heatId)}` : '/api/heats';
      let data;
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        data = await res.json();
      } catch (e) {
        showFormError(formEl, 'Network error - is the server running?');
        return;
      }
      if (!data.ok) { showFormError(formEl, data.error); return; }
      closeModal();
      showToast(heatId ? `Heat ${data.heat_id} updated` : `Heat ${data.heat_id} created`, 'ok');
      if (context === 'detail') {
        if (heatId && data.heat_id !== heatId) {
          window.location.href = '/heats/' + encodeURIComponent(data.heat_id);
          return;
        }
        refreshHeatInfo();
      } else if (document.getElementById('heats-results')) {
        refreshHeatsResults();
      }
    });
  });
}

async function deleteHeat(heatId, context) {
  if (!confirm(`Delete heat ${heatId} and all its events?`)) return;
  let data;
  try {
    const res = await fetch(`/api/heats/${encodeURIComponent(heatId)}/delete`, { method: 'POST' });
    data = await res.json();
  } catch (e) {
    showToast('Network error - is the server running?', 'error');
    return;
  }
  if (!data.ok) { showToast(data.error || 'Delete failed', 'error'); return; }
  showToast(`Deleted heat ${heatId}`, 'ok');
  if (context === 'detail') {
    window.location.href = '/heats';
  } else if (document.getElementById('heats-results')) {
    refreshHeatsResults();
  }
}

async function openEventModal(eventId, prefillHeatId) {
  let values = {};
  if (eventId) {
    const res = await fetch(`/api/events/${eventId}`);
    if (!res.ok) { showToast('Could not load that event', 'error'); return; }
    values = await res.json();
  } else if (prefillHeatId) {
    values = { heat_id: prefillHeatId };
  }
  let heatIds = [];
  try {
    const idRes = await fetch('/api/heat-ids');
    if (idRes.ok) heatIds = (await idRes.json()).ids || [];
  } catch (e) { /* datalist is a nice-to-have */ }

  const bodyHtml = buildFormHtml(EVENT_FIELDS, values, { heat_id: heatIds });
  openModal(eventId ? `Edit event #${eventId}` : 'Create event', bodyHtml, (formEl) => {
    wireDurationAutoCalc(formEl, 'start_time', 'end_time', 'duration');
    formEl.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const payload = collectFormData(formEl, EVENT_FIELDS);
      const url = eventId ? `/api/events/${eventId}` : '/api/events';
      let data;
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        data = await res.json();
      } catch (e) {
        showFormError(formEl, 'Network error - is the server running?');
        return;
      }
      if (!data.ok) { showFormError(formEl, data.error); return; }
      closeModal();
      showToast(eventId ? `Event #${eventId} updated` : 'Event created', 'ok');
      if (document.getElementById('events-results')) refreshEventsResults();
      if (document.getElementById('heat-events')) refreshHeatEvents();
    });
  });
}

async function deleteEvent(eventId) {
  if (!confirm(`Delete event #${eventId}?`)) return;
  let data;
  try {
    const res = await fetch(`/api/events/${eventId}/delete`, { method: 'POST' });
    data = await res.json();
  } catch (e) {
    showToast('Network error - is the server running?', 'error');
    return;
  }
  if (!data.ok) { showToast(data.error || 'Delete failed', 'error'); return; }
  showToast(`Deleted event #${eventId}`, 'ok');
  if (document.getElementById('events-results')) refreshEventsResults();
  if (document.getElementById('heat-events')) refreshHeatEvents();
}

// Delegated click handling -- works even after a fragment refresh replaces
// the DOM underneath these buttons, because the listener lives on <body>.
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  e.preventDefault();
  const action = el.dataset.action;
  if (action === 'new-heat') openHeatModal(null, el.dataset.context);
  else if (action === 'edit-heat') openHeatModal(el.dataset.heatId, el.dataset.context);
  else if (action === 'delete-heat') deleteHeat(el.dataset.heatId, el.dataset.context);
  else if (action === 'new-event') openEventModal(null, el.dataset.heatId);
  else if (action === 'edit-event') openEventModal(el.dataset.eventId, null);
  else if (action === 'delete-event') deleteEvent(el.dataset.eventId);
});
</script>
</body></html>
"""

_T_LOGIN = """{% extends "layout.html" %}
{% block title %}Login - DB Admin{% endblock %}
{% block body %}
<div class="card" style="max-width:420px;margin:60px auto">
  <h2>Sign in</h2>
  <p style="color:var(--dim);font-size:12.5px;margin-top:0">
    Use the SQL Server credentials from <code>config.py</code>
    (<code>SQL_UID</code> / <code>SQL_PASSWORD</code>).
  </p>
  <form method="post">
    <div class="field">
      <label>Username</label>
      <input type="text" name="username" autofocus required
             value="{{ config.SQL_UID }}">
    </div>
    <div class="field">
      <label>Password</label>
      <input type="password" name="password" required>
    </div>
    <div class="actions">
      <button class="btn btn--primary" type="submit">Sign in</button>
    </div>
  </form>
</div>
{% endblock %}"""

_T_HOME = """{% extends "layout.html" %}
{% block body %}
<div class="stats">
  <div class="stat">
    <div class="stat__num">{{ heats_total }}</div>
    <div class="stat__lbl">Heats</div>
  </div>
  <div class="stat">
    <div class="stat__num">{{ events_total }}</div>
    <div class="stat__lbl">Events</div>
  </div>
  <div class="stat">
    <div class="stat__num">{{ by_furnace|length }}</div>
    <div class="stat__lbl">Furnaces</div>
  </div>
</div>

<div class="card" style="margin-top:18px">
  <h2>Heats by furnace</h2>
  {% if by_furnace %}
    <table>
      <thead><tr><th>FurnaceId</th><th>Heats</th></tr></thead>
      <tbody>
        {% for f, n in by_furnace %}
        <tr>
          <td><a href="{{ url_for('heats_list', furnace=f) }}">{{ f }}</a></td>
          <td>{{ n }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <div class="empty">No heats yet.</div>
  {% endif %}
</div>

<div class="card">
  <h2>Latest 10 heats</h2>
  {% if recent %}
    <table>
      <thead>
        <tr><th>heat_id</th><th>FurnaceId</th><th>Status</th><th>Start</th><th></th></tr>
      </thead>
      <tbody>
        {% for h in recent %}
        <tr>
          <td>{{ h.heat_id }}</td>
          <td>{{ h.FurnaceId or "" }}</td>
          <td><span class="chip {{ 'ok' if h.heat_status == 'COMPLETE' else 'warn' }}">{{ h.heat_status or "" }}</span></td>
          <td>{{ h.heat_start_time or "" }}</td>
          <td><a class="btn btn--small" href="{{ url_for('heat_detail', heat_id=h.heat_id) }}">View</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <div class="empty">No heats yet.</div>
  {% endif %}
</div>
{% endblock %}"""

_T_HEATS_LIST = """{% extends "layout.html" %}
{% block title %}Heats - DB Admin{% endblock %}
{% block body %}
<a class="back-link" href="{{ url_for('home') }}">&#8592; Overview</a>

<div class="toolbar">
  <form method="get" class="toolbar" style="flex:1">
    <div class="field">
      <label>Search</label>
      <input type="text" name="q" value="{{ q }}" placeholder="heat_id / furnace / stand">
    </div>
    <div class="field">
      <label>FurnaceId</label>
      <select name="furnace">
        <option value="">(any)</option>
        {% for f in furnaces %}
          <option value="{{ f }}" {% if f == furnace %}selected{% endif %}>{{ f }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Status</label>
      <select name="status">
        <option value="">(any)</option>
        {% for s in statuses %}
          <option value="{{ s }}" {% if s == status %}selected{% endif %}>{{ s }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Per page</label>
      <select name="per_page">
        {% for n in [25, 50, 100, 200] %}
          <option value="{{ n }}" {% if n == per_page %}selected{% endif %}>{{ n }}</option>
        {% endfor %}
      </select>
    </div>
    <button class="btn" type="submit">Filter</button>
    <a class="btn" href="{{ url_for('heats_list') }}">Reset</a>
    <button type="button" class="btn btn--primary" data-action="new-heat">+ New heat</button>
  </form>
</div>

<div id="heats-results">
  {% include "heats_results_fragment.html" %}
</div>
{% endblock %}"""

_T_HEATS_RESULTS_FRAGMENT = """<div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>heat_id</th>
        <th>FurnaceId</th>
        <th>#</th>
        <th>Status</th>
        <th>Start</th>
        <th>End</th>
        <th>Total</th>
        <th>Scrap</th>
        <th>Sponge</th>
        <th>Tap</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {% for h in rows %}
      <tr>
        <td><a href="{{ url_for('heat_detail', heat_id=h.heat_id) }}">{{ h.heat_id }}</a></td>
        <td>{{ h.FurnaceId or "" }}</td>
        <td>{{ h.heat_number if h.heat_number is not none else "" }}</td>
        <td>
          <span class="chip {{ 'ok' if h.heat_status == 'COMPLETE' else 'warn' }}">
            {{ h.heat_status or "" }}
          </span>
        </td>
        <td>{{ h.heat_start_time or "" }}</td>
        <td>{{ h.heat_end_time or "" }}</td>
        <td>{{ h.heat_duration or "" }}</td>
        <td>{{ h.scrap_duration or "" }}</td>
        <td>{{ h.sponge_duration or "" }}</td>
        <td>{{ h.tapping_duration or "" }}</td>
        <td>
          <button type="button" class="btn btn--small" data-action="edit-heat" data-heat-id="{{ h.heat_id }}">Edit</button>
          <button type="button" class="btn btn--small btn--danger" data-action="delete-heat" data-heat-id="{{ h.heat_id }}">Delete</button>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="11"><div class="empty">No heats match your filter.</div></td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>

{% if pages > 1 %}
<div class="pager">
  {% if page > 1 %}
    <a href="{{ url_for('heats_list', page=page-1, per_page=per_page, q=q, furnace=furnace, status=status) }}">&#8592; Prev</a>
  {% else %}
    <span class="disabled">&#8592; Prev</span>
  {% endif %}
  <span class="current">{{ page }} / {{ pages }}</span>
  {% if page < pages %}
    <a href="{{ url_for('heats_list', page=page+1, per_page=per_page, q=q, furnace=furnace, status=status) }}">Next &#8594;</a>
  {% else %}
    <span class="disabled">Next &#8594;</span>
  {% endif %}
  <span style="color:var(--mute);margin-left:10px">{{ total }} row(s)</span>
</div>
{% else %}
<div style="text-align:center;color:var(--mute);font-size:12px;padding:10px">{{ total }} row(s)</div>
{% endif %}"""

_T_HEAT_DETAIL = """{% extends "layout.html" %}
{% block title %}{{ heat.heat_id }} - DB Admin{% endblock %}
{% block body %}
<a class="back-link" href="{{ url_for('heats_list') }}">&#8592; Heats</a>

<div id="heat-info">
  {% include "heat_info_fragment.html" %}
</div>

<div id="heat-events">
  {% include "heat_events_fragment.html" %}
</div>
{% endblock %}"""

_T_HEAT_INFO_FRAGMENT = """<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:start;gap:16px;flex-wrap:wrap">
    <h2 style="margin-bottom:0">{{ heat.heat_id }}</h2>
    <div style="display:flex;gap:8px">
      <button type="button" class="btn" data-action="edit-heat"
              data-heat-id="{{ heat.heat_id }}" data-context="detail">Edit</button>
      <button type="button" class="btn btn--danger" data-action="delete-heat"
              data-heat-id="{{ heat.heat_id }}" data-context="detail">Delete</button>
    </div>
  </div>

  <dl class="kv" style="margin-top:14px">
    {% for k, v in heat.items() %}
      <dt>{{ k }}</dt>
      <dd>{{ v if v is not none else '—' }}</dd>
    {% endfor %}
  </dl>
</div>"""

_T_HEAT_EVENTS_FRAGMENT = """<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:16px">
    <h2 style="margin:0">Events ({{ events|length }})</h2>
    <button type="button" class="btn btn--primary btn--small"
            data-action="new-event" data-heat-id="{{ heat.heat_id }}">+ New event</button>
  </div>
  <div class="tbl-wrap" style="margin-top:12px;max-height:50vh">
    <table>
      <thead>
        <tr>
          <th>id</th><th>event_name</th><th>FurnaceId</th>
          <th>start_time</th><th>end_time</th><th>duration</th>
          <th>status</th><th>confidence</th><th></th>
        </tr>
      </thead>
      <tbody>
        {% for e in events %}
        <tr>
          <td>{{ e.id }}</td>
          <td>{{ e.event_name or "" }}</td>
          <td>{{ e.FurnaceId or "" }}</td>
          <td>{{ e.start_time or "" }}</td>
          <td>{{ e.end_time or "" }}</td>
          <td>{{ e.duration or "" }}</td>
          <td>{{ e.status or "" }}</td>
          <td>{{ e.confidence or "" }}</td>
          <td>
            <button type="button" class="btn btn--small" data-action="edit-event" data-event-id="{{ e.id }}">Edit</button>
            <button type="button" class="btn btn--small btn--danger" data-action="delete-event" data-event-id="{{ e.id }}">Delete</button>
          </td>
        </tr>
        {% else %}
        <tr><td colspan="9"><div class="empty">No events for this heat.</div></td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>"""

_T_EVENTS_LIST = """{% extends "layout.html" %}
{% block title %}Events - DB Admin{% endblock %}
{% block body %}
<a class="back-link" href="{{ url_for('home') }}">&#8592; Overview</a>

<div class="toolbar">
  <form method="get" class="toolbar" style="flex:1">
    <div class="field">
      <label>Search</label>
      <input type="text" name="q" value="{{ q }}" placeholder="heat_id / event_name / event_id">
    </div>
    <div class="field">
      <label>Event name</label>
      <select name="event_name">
        <option value="">(any)</option>
        {% for n in event_names %}
          <option value="{{ n }}" {% if n == event_name %}selected{% endif %}>{{ n }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Per page</label>
      <select name="per_page">
        {% for n in [25, 50, 100, 200] %}
          <option value="{{ n }}" {% if n == per_page %}selected{% endif %}>{{ n }}</option>
        {% endfor %}
      </select>
    </div>
    <button class="btn" type="submit">Filter</button>
    <a class="btn" href="{{ url_for('events_list') }}">Reset</a>
    <button type="button" class="btn btn--primary" data-action="new-event">+ New event</button>
  </form>
</div>

<div id="events-results">
  {% include "events_results_fragment.html" %}
</div>
{% endblock %}"""

_T_EVENTS_RESULTS_FRAGMENT = """<div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th>id</th><th>heat_id</th><th>event_name</th><th>FurnaceId</th>
        <th>start_time</th><th>end_time</th><th>duration</th>
        <th>status</th><th>created_at</th><th></th>
      </tr>
    </thead>
    <tbody>
      {% for e in rows %}
      <tr>
        <td>{{ e.id }}</td>
        <td>
          {% if e.heat_id %}
            <a href="{{ url_for('heat_detail', heat_id=e.heat_id) }}">{{ e.heat_id }}</a>
          {% endif %}
        </td>
        <td>{{ e.event_name or "" }}</td>
        <td>{{ e.FurnaceId or "" }}</td>
        <td>{{ e.start_time or "" }}</td>
        <td>{{ e.end_time or "" }}</td>
        <td>{{ e.duration or "" }}</td>
        <td>{{ e.status or "" }}</td>
        <td>{{ e.created_at or "" }}</td>
        <td>
          <button type="button" class="btn btn--small" data-action="edit-event" data-event-id="{{ e.id }}">Edit</button>
          <button type="button" class="btn btn--small btn--danger" data-action="delete-event" data-event-id="{{ e.id }}">Delete</button>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="10"><div class="empty">No events match your filter.</div></td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>

{% if pages > 1 %}
<div class="pager">
  {% if page > 1 %}
    <a href="{{ url_for('events_list', page=page-1, per_page=per_page, q=q, event_name=event_name) }}">&#8592; Prev</a>
  {% else %}<span class="disabled">&#8592; Prev</span>{% endif %}
  <span class="current">{{ page }} / {{ pages }}</span>
  {% if page < pages %}
    <a href="{{ url_for('events_list', page=page+1, per_page=per_page, q=q, event_name=event_name) }}">Next &#8594;</a>
  {% else %}<span class="disabled">Next &#8594;</span>{% endif %}
  <span style="color:var(--mute);margin-left:10px">{{ total }} row(s)</span>
</div>
{% else %}
<div style="text-align:center;color:var(--mute);font-size:12px;padding:10px">{{ total }} row(s)</div>
{% endif %}"""

TEMPLATES = {
    "layout.html":                   _LAYOUT,
    "login.html":                    _T_LOGIN,
    "home.html":                     _T_HOME,
    "heats_list.html":                _T_HEATS_LIST,
    "heats_results_fragment.html":    _T_HEATS_RESULTS_FRAGMENT,
    "heat_detail.html":               _T_HEAT_DETAIL,
    "heat_info_fragment.html":        _T_HEAT_INFO_FRAGMENT,
    "heat_events_fragment.html":      _T_HEAT_EVENTS_FRAGMENT,
    "events_list.html":               _T_EVENTS_LIST,
    "events_results_fragment.html":   _T_EVENTS_RESULTS_FRAGMENT,
}


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("ADMIN_SECRET_KEY") or os.urandom(24).hex()
app.jinja_loader = DictLoader(TEMPLATES)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _html_dt(value):
    """Render a value as the string an HTML datetime-local input expects."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%dT%H:%M")
    s = str(value).replace(" ", "T")
    return s[:16]


app.jinja_env.globals["html_dt"] = _html_dt


@app.context_processor
def _inject_globals():
    return {
        "config": config,
        "auth_enabled": AUTH_ENABLED,
    }


# ---------------------------------------------------------------------------
# Error handlers -- always log the traceback, show it if ADMIN_DEBUG=1
# ---------------------------------------------------------------------------
def _render_error_page(exc, status_code=500):
    tb = traceback.format_exc()
    logger.error("Unhandled exception:\n%s", tb)
    err_str = str(exc)
    is_db_err = "pyodbc" in str(type(exc)) or "08001" in err_str or "SQL" in err_str or "login" in err_str.lower()
    title = "Database Connection Failed" if is_db_err else f"{type(exc).__name__} (Error {status_code})"

    trace_block = f'<h3>Traceback:</h3><pre class="trace">{tb}</pre>' if (ADMIN_DEBUG or not is_db_err) else (
        '<p style="color:#94a3b8;font-size:13px;margin-top:14px;">Run with <code>$env:ADMIN_DEBUG="1"; python admin_app.py</code> to display the full stack trace on this page.</p>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 40px 20px; }}
        .card {{ max-width: 820px; margin: 0 auto; background: #1e293b; border-radius: 8px; border: 1px solid #334155; padding: 28px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
        h1 {{ color: #ef4444; font-size: 22px; margin-top: 0; }}
        .details {{ background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 14px; font-family: monospace; font-size: 13px; color: #fca5a5; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }}
        .info-table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
        .info-table td {{ padding: 6px 10px; border-bottom: 1px solid #334155; font-size: 14px; }}
        .info-table td:first-child {{ color: #94a3b8; width: 160px; }}
        .checklist {{ background: #1e293b; border-left: 4px solid #38bdf8; padding: 12px 16px; margin: 20px 0; }}
        .checklist h3 {{ margin: 0 0 8px 0; font-size: 15px; color: #38bdf8; }}
        .checklist ul {{ margin: 0; padding-left: 20px; color: #cbd5e1; font-size: 14px; }}
        .checklist li {{ margin-bottom: 6px; }}
        pre.trace {{ background: #020617; color: #cbd5e1; padding: 12px; border-radius: 6px; font-size: 12px; overflow-x: auto; }}
        .btn {{ display: inline-block; background: #3b82f6; color: #fff; text-decoration: none; padding: 8px 16px; border-radius: 4px; font-size: 14px; margin-top: 10px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>{title}</h1>
        <p>The Admin Dashboard could not load data because the SQL Server database is unreachable or rejected the connection.</p>
        
        <table class="info-table">
            <tr><td>SQL Server</td><td><code>{config.SQL_SERVER}</code></td></tr>
            <tr><td>Database</td><td><code>{config.SQL_DATABASE}</code></td></tr>
            <tr><td>Username</td><td><code>{config.SQL_UID}</code></td></tr>
        </table>

        <div class="details">{err_str}</div>

        <div class="checklist">
            <h3>Troubleshooting Checklist</h3>
            <ul>
                <li><strong>Network / VPN:</strong> Ensure your machine is on the same network or VPN as <code>{config.SQL_SERVER}</code>.</li>
                <li><strong>SQL Server Service:</strong> Verify SQL Server is running and listening on port 1433.</li>
                <li><strong>Connection Test:</strong> Run <code>python check_sql.py</code> in your terminal to test connectivity and credentials.</li>
                <li><strong>Debug Mode:</strong> Run <code>$env:ADMIN_DEBUG="1"; python admin_app.py</code> to see live interactive error traces.</li>
            </ul>
        </div>

        {trace_block}

        <a class="btn" href="javascript:location.reload()">Retry Connection</a>
    </div>
</body>
</html>"""
    return html, status_code


@app.errorhandler(500)
def _handle_500(exc):
    return _render_error_page(exc, 500)


@app.errorhandler(Exception)
def _handle_uncaught(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    return _render_error_page(exc, 500)


def _parse_dt(text):
    """Parse 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM', or 'YYYY-MM-DD'.
    Empty -> None.  Raises ValueError on a bad format."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date/time: {text!r}")


def _parse_int(text):
    text = (text or "").strip() if isinstance(text, str) else text
    if text in (None, ""):
        return None
    return int(text)


def _parse_str(text, max_len=None):
    text = (text or "").strip() if isinstance(text, str) else text
    if text in (None, ""):
        return None
    text = str(text)
    if max_len and len(text) > max_len:
        raise ValueError(f"Value too long (max {max_len} chars)")
    return text


def _minutes_between(start, end):
    """start/end are datetime objects (or None). Returns a formatted
    minutes string ('60' or '12.5'), or None if either side is missing."""
    if start is None or end is None:
        return None
    delta_minutes = (end - start).total_seconds() / 60.0
    if delta_minutes == int(delta_minutes):
        return str(int(delta_minutes))
    return f"{delta_minutes:.1f}"


def _rows_to_dicts(cursor):
    """pyodbc rows -> list of dicts, with datetime -> 'YYYY-MM-DD HH:MM:SS'
    and binary columns shown as a small descriptive string."""
    cols = [c[0] for c in cursor.description]
    out = []
    for row in cursor.fetchall():
        d = {}
        for i, name in enumerate(cols):
            v = row[i]
            if isinstance(v, datetime.datetime):
                d[name] = v.strftime("%Y-%m-%d %H:%M:%S")
            elif hasattr(v, "quantize"):                     # Decimal
                d[name] = float(v)
            elif isinstance(v, (bytes, bytearray)):          # varbinary
                d[name] = f"<binary {len(v)} bytes>"
            else:
                d[name] = v
        out.append(d)
    return out


def _one(cursor):
    rows = _rows_to_dicts(cursor)
    return rows[0] if rows else None


def _conn():
    return db.connect()


# ---------------------------------------------------------------------------
# Auth (uses the SQL credentials from config.py)
# ---------------------------------------------------------------------------
@app.before_request
def _require_login():
    if not AUTH_ENABLED:
        return
    if request.endpoint in ("login", "static", "healthz"):
        return
    if not session.get("ok"):
        # AJAX/API calls get a JSON 401 instead of a redirect to /login,
        # so a modal never silently posts into a login page.
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Not signed in"}), 401
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session["ok"] = True
            session["user"] = u
            nxt = request.args.get("next") or url_for("home")
            return redirect(nxt)
        flash("Wrong username or password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {db.HEATS}")
        heats_total = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {db.EVENTS}")
        events_total = cur.fetchone()[0]
        cur.execute(f"""
            SELECT FurnaceId, COUNT(*) FROM {db.HEATS}
            GROUP BY FurnaceId ORDER BY FurnaceId
        """)
        by_furnace = [(r[0] or "(blank)", r[1]) for r in cur.fetchall()]
        cur.execute(f"""
            SELECT TOP 10 heat_id, FurnaceId, heat_status, heat_start_time
            FROM {db.HEATS}
            ORDER BY COALESCE(heat_start_time, insertion_time) DESC, heat_id
        """)
        recent = _rows_to_dicts(cur)
        return render_template(
            "home.html",
            heats_total=heats_total, events_total=events_total,
            by_furnace=by_furnace, recent=recent,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Heats
# ---------------------------------------------------------------------------
HEAT_LIST_COLS = [
    "heat_id", "FurnaceId", "furnace", "stand", "heat_number",
    "heat_status", "heat_start_time", "heat_end_time",
    "heat_duration", "scrap_duration", "sponge_duration", "tapping_duration",
]

# (column, label, input kind).  Binary / large-text columns are hidden.
# NOTE: keep this in sync with the HEAT_FIELDS array in the layout's <script>.
HEAT_FORM_FIELDS = [
    ("heat_id",          "heat_id (primary key)",  "text"),
    ("FurnaceId",        "FurnaceId",              "text"),
    ("furnace",          "furnace (line)",         "text"),
    ("stand",            "stand",                  "text"),
    ("heat_number",      "heat_number",            "number"),
    ("heat_status",      "heat_status",            "select"),
    ("heat_start_time",  "heat_start_time",        "datetime"),
    ("heat_end_time",    "heat_end_time",          "datetime"),
    ("heat_duration",    "heat_duration (min)",    "text"),
    ("scrap_duration",   "scrap_duration (min)",   "text"),
    ("sponge_duration",  "sponge_duration (min)",  "text"),
    ("tapping_duration", "tapping_duration (min)", "text"),
    ("heat_event_match", "heat_event_match",       "text"),
    ("position",         "position",               "text"),
    ("operation_status", "operation_status",       "text"),
]


def _heat_form_values(data):
    """data is either request.form or a plain dict decoded from JSON --
    both support .get(), so this works for the HTML form path and the
    JSON API path alike."""
    values = {
        "heat_id":          _parse_str(data.get("heat_id"), 64),
        "FurnaceId":        _parse_str(data.get("FurnaceId"), 16),
        "furnace":          _parse_str(data.get("furnace"), 50),
        "stand":            _parse_str(data.get("stand"), 50),
        "heat_number":      _parse_int(data.get("heat_number")),
        "heat_status":      _parse_str(data.get("heat_status"), 20),
        "heat_start_time":  _parse_dt(data.get("heat_start_time")),
        "heat_end_time":    _parse_dt(data.get("heat_end_time")),
        "heat_duration":    _parse_str(data.get("heat_duration"), 50),
        "scrap_duration":   _parse_str(data.get("scrap_duration"), 50),
        "sponge_duration":  _parse_str(data.get("sponge_duration"), 50),
        "tapping_duration": _parse_str(data.get("tapping_duration"), 50),
        "heat_event_match": _parse_str(data.get("heat_event_match"), 5),
        "position":         _parse_str(data.get("position"), 20),
        "operation_status": _parse_str(data.get("operation_status"), 20),
    }
    # Whenever both ends of the heat are known, heat_duration is derived
    # from them rather than trusted from whatever was typed into the form --
    # so editing either time always keeps the minutes figure correct.
    computed = _minutes_between(values["heat_start_time"], values["heat_end_time"])
    if computed is not None:
        values["heat_duration"] = computed
    return values


@app.route("/heats")
def heats_list():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    except ValueError:
        page, per_page = 1, 50
    q = (request.args.get("q") or "").strip()
    furnace = (request.args.get("furnace") or "").strip()
    status = (request.args.get("status") or "").strip()

    where, params = [], []
    if q:
        where.append("(heat_id LIKE ? OR furnace LIKE ? OR stand LIKE ?)")
        params += [f"%{q}%"] * 3
    if furnace:
        where.append("FurnaceId = ?")
        params.append(furnace)
    if status:
        where.append("heat_status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {db.HEATS} {where_sql}", *params)
        total = cur.fetchone()[0]

        offset = (page - 1) * per_page
        cur.execute(f"""
            SELECT {", ".join(HEAT_LIST_COLS)}
            FROM {db.HEATS}
            {where_sql}
            ORDER BY COALESCE(heat_start_time, insertion_time) DESC, heat_id
            OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
        """, *params, offset, per_page)
        rows = _rows_to_dicts(cur)
        pages = max(1, (total + per_page - 1) // per_page)

        ctx = dict(rows=rows, total=total, page=page, pages=pages,
                   per_page=per_page, q=q, furnace=furnace, status=status)

        if request.headers.get("X-Fragment") == "results":
            return render_template("heats_results_fragment.html", **ctx)

        cur.execute(f"SELECT DISTINCT FurnaceId FROM {db.HEATS} ORDER BY FurnaceId")
        furnaces = [r[0] for r in cur.fetchall() if r[0]]
        cur.execute(f"SELECT DISTINCT heat_status FROM {db.HEATS} ORDER BY heat_status")
        statuses = [r[0] for r in cur.fetchall() if r[0]]

        return render_template("heats_list.html", **ctx,
                               furnaces=furnaces, statuses=statuses)
    finally:
        conn.close()


@app.route("/heats/<heat_id>")
def heat_detail(heat_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {db.HEATS} WHERE heat_id = ?", heat_id)
        heat = _one(cur)
        if not heat:
            abort(404)
        # Hide the bulky / binary columns from the detail view.
        for c in ("timeline_json", "detected_classes",
                  "original_frame", "plotted_image"):
            heat.pop(c, None)

        cur.execute(f"""
            SELECT id, event_name, FurnaceId, start_time, end_time,
                   duration, status, confidence
            FROM {db.EVENTS}
            WHERE heat_id = ?
            ORDER BY COALESCE(start_time, created_at), id
        """, heat_id)
        events = _rows_to_dicts(cur)

        frag = request.headers.get("X-Fragment")
        if frag == "info":
            return render_template("heat_info_fragment.html", heat=heat)
        if frag == "events":
            return render_template("heat_events_fragment.html", heat=heat, events=events)
        return render_template("heat_detail.html", heat=heat, events=events)
    finally:
        conn.close()


def _all_heat_ids(cur, limit=200):
    cur.execute(f"SELECT TOP {int(limit)} heat_id FROM {db.HEATS} "
                f"ORDER BY COALESCE(heat_start_time, insertion_time) DESC")
    return [r[0] for r in cur.fetchall() if r[0]]


# ---------------------------------------------------------------------------
# HeatEvents
# ---------------------------------------------------------------------------
EVENT_LIST_COLS = [
    "id", "heat_id", "event_name", "FurnaceId",
    "start_time", "end_time", "duration", "status", "created_at",
]

# NOTE: keep this in sync with the EVENT_FIELDS array in the layout's <script>.
EVENT_FORM_FIELDS = [
    # (column, label, kind, hint)
    ("heat_id",          "heat_id",          "text",     ""),
    ("event_name",       "event_name",       "text",     "e.g. scrap_charging"),
    ("FurnaceId",        "FurnaceId",        "text",     "e.g. 4/1"),
    ("event_id",         "event_id (unique)", "text",    "leave blank for a new auto key"),
    ("cross_event_id",   "cross_event_id",   "text",     ""),
    ("cross_event_name", "cross_event_name", "text",     ""),
    ("start_time",       "start_time",       "datetime", ""),
    ("end_time",         "end_time",         "datetime", ""),
    ("duration",         "duration (min)",   "text",     ""),
    ("status",           "status",           "text",     "COMPLETED / IN_PROGRESS"),
    ("confidence",       "confidence",       "text",     ""),
    ("sop_duration",     "sop_duration",     "text",     ""),
]


def _event_form_values(data):
    values = {
        "heat_id":          _parse_str(data.get("heat_id"), 64),
        "event_name":       _parse_str(data.get("event_name"), 100),
        "FurnaceId":        _parse_str(data.get("FurnaceId"), 16),
        "event_id":         _parse_str(data.get("event_id"), 64),
        "cross_event_id":   _parse_str(data.get("cross_event_id"), 64),
        "cross_event_name": _parse_str(data.get("cross_event_name"), 100),
        "start_time":       _parse_dt(data.get("start_time")),
        "end_time":         _parse_dt(data.get("end_time")),
        "duration":         _parse_str(data.get("duration"), 50),
        "status":           _parse_str(data.get("status"), 20),
        "confidence":       _parse_str(data.get("confidence"), 20),
        "sop_duration":     _parse_str(data.get("sop_duration"), 50),
    }
    # Same rule as heats: start_time/end_time are the source of truth for
    # duration, so changing either time on save keeps duration in sync.
    computed = _minutes_between(values["start_time"], values["end_time"])
    if computed is not None:
        values["duration"] = computed
    return values


@app.route("/events")
def events_list():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    except ValueError:
        page, per_page = 1, 50
    q = (request.args.get("q") or "").strip()
    event_name = (request.args.get("event_name") or "").strip()

    where, params = [], []
    if q:
        where.append("(heat_id LIKE ? OR event_name LIKE ? OR event_id LIKE ?)")
        params += [f"%{q}%"] * 3
    if event_name:
        where.append("event_name = ?")
        params.append(event_name)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {db.EVENTS} {where_sql}", *params)
        total = cur.fetchone()[0]

        offset = (page - 1) * per_page
        cur.execute(f"""
            SELECT {", ".join(EVENT_LIST_COLS)}
            FROM {db.EVENTS}
            {where_sql}
            ORDER BY COALESCE(start_time, created_at) DESC, id DESC
            OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
        """, *params, offset, per_page)
        rows = _rows_to_dicts(cur)
        pages = max(1, (total + per_page - 1) // per_page)

        ctx = dict(rows=rows, total=total, page=page, pages=pages,
                   per_page=per_page, q=q, event_name=event_name)

        if request.headers.get("X-Fragment") == "results":
            return render_template("events_results_fragment.html", **ctx)

        cur.execute(f"""
            SELECT DISTINCT event_name FROM {db.EVENTS}
            WHERE event_name IS NOT NULL ORDER BY event_name
        """)
        event_names = [r[0] for r in cur.fetchall() if r[0]]

        return render_template("events_list.html", **ctx, event_names=event_names)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# JSON API -- used by the modals in every list/detail page. Every endpoint
# here returns JSON and never redirects, which is what keeps the UI on the
# same page after a create / edit / delete.
# ---------------------------------------------------------------------------
@app.route("/api/heats/<heat_id>")
def api_heat_get(heat_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {db.HEATS} WHERE heat_id = ?", heat_id)
        heat = _one(cur)
        if not heat:
            return jsonify({"ok": False, "error": "not found"}), 404
        out = {}
        for col, _, kind in HEAT_FORM_FIELDS:
            v = heat.get(col)
            out[col] = _html_dt(v) if kind == "datetime" else v
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/heats", methods=["POST"])
def api_heat_create():
    data = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        values = _heat_form_values(data)
        if not values["heat_id"]:
            raise ValueError("heat_id is required")
        cols = ["heat_id"] + [c for c, _, _ in HEAT_FORM_FIELDS if c != "heat_id"]
        placeholders = ", ".join("?" for _ in cols)
        sql = (f"INSERT INTO {db.HEATS} ({', '.join(cols)}, "
               f"insertion_time, updated_time) VALUES "
               f"({placeholders}, SYSDATETIME(), SYSDATETIME())")
        params = [values[c] for c in cols]
        conn.cursor().execute(sql, *params)
        conn.commit()
        return jsonify({"ok": True, "heat_id": values["heat_id"]})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/heats/<heat_id>", methods=["POST"])
def api_heat_update(heat_id):
    data = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        cur = conn.cursor()
        values = _heat_form_values(data)
        new_id = values.pop("heat_id") or heat_id

        if new_id != heat_id:
            # Rename PK; propagate to events (no FK is declared).
            cur.execute(
                f"UPDATE {db.EVENTS} SET heat_id = ? WHERE heat_id = ?",
                new_id, heat_id,
            )

        assignments = ", ".join(f"{c} = ?" for c in values)
        params = list(values.values()) + [heat_id]
        cur.execute(
            f"UPDATE {db.HEATS} SET {assignments}, "
            f"updated_time = SYSDATETIME() WHERE heat_id = ?",
            *params,
        )
        if cur.rowcount == 0:
            raise ValueError(f"No heat with heat_id={heat_id!r}")
        conn.commit()
        return jsonify({"ok": True, "heat_id": new_id})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/heats/<heat_id>/delete", methods=["POST"])
def api_heat_delete(heat_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        # Events first, matching the monitor's own delete order.
        cur.execute(f"DELETE FROM {db.EVENTS} WHERE heat_id = ?", heat_id)
        n_events = cur.rowcount
        cur.execute(f"DELETE FROM {db.HEATS} WHERE heat_id = ?", heat_id)
        n_heats = cur.rowcount
        conn.commit()
        if n_heats == 0:
            return jsonify({"ok": False, "error": f"No heat with heat_id={heat_id!r}"}), 404
        return jsonify({"ok": True, "events_deleted": n_events})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/events/<int:event_id>")
def api_event_get(event_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {db.EVENTS} WHERE id = ?", event_id)
        ev = _one(cur)
        if not ev:
            return jsonify({"ok": False, "error": "not found"}), 404
        out = {}
        for col, _, kind, _ in EVENT_FORM_FIELDS:
            v = ev.get(col)
            out[col] = _html_dt(v) if kind == "datetime" else v
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/events", methods=["POST"])
def api_event_create():
    data = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        values = _event_form_values(data)
        cols = [c for c, _, _, _ in EVENT_FORM_FIELDS]
        placeholders = ", ".join("?" for _ in cols)
        sql = (f"INSERT INTO {db.EVENTS} ({', '.join(cols)}, "
               f"created_at, updated_at) VALUES "
               f"({placeholders}, SYSDATETIME(), SYSDATETIME())")
        params = [values[c] for c in cols]
        conn.cursor().execute(sql, *params)
        conn.commit()
        return jsonify({"ok": True, "heat_id": values.get("heat_id")})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/events/<int:event_id>", methods=["POST"])
def api_event_update(event_id):
    data = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        cur = conn.cursor()
        values = _event_form_values(data)
        assignments = ", ".join(f"{c} = ?" for c in values)
        params = list(values.values()) + [event_id]
        cur.execute(
            f"UPDATE {db.EVENTS} SET {assignments}, "
            f"updated_at = SYSDATETIME() WHERE id = ?",
            *params,
        )
        if cur.rowcount == 0:
            raise ValueError(f"No event with id={event_id}")
        conn.commit()
        return jsonify({"ok": True, "id": event_id, "heat_id": values.get("heat_id")})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/events/<int:event_id>/delete", methods=["POST"])
def api_event_delete(event_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {db.EVENTS} WHERE id = ?", event_id)
        n = cur.rowcount
        conn.commit()
        if n == 0:
            return jsonify({"ok": False, "error": f"No event with id={event_id}"}), 404
        return jsonify({"ok": True})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/heat-ids")
def api_heat_ids():
    conn = _conn()
    try:
        cur = conn.cursor()
        return jsonify({"ids": _all_heat_ids(cur, limit=200)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    try:
        conn = _conn()
        try:
            conn.cursor().execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return "ok", 200
    except Exception as exc:
        return f"error: {exc}", 500


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not config.SQL_ENABLED:
        print("WARNING: config.SQL_ENABLED is 0.  The admin app needs SQL on; "
              "set SQL_ENABLED=1 in the environment before launching.")

    print("=" * 62)
    print("Furnace DB Admin")
    print("=" * 62)
    print(f"SQL server     : {config.SQL_SERVER}")
    print(f"SQL database   : {config.SQL_DATABASE}")
    print(f"SQL login      : {config.SQL_UID}")
    print(f"Web login user : {ADMIN_USERNAME}")
    print(f"Web login pass : {'(from config.SQL_PASSWORD)' if ADMIN_PASSWORD == config.SQL_PASSWORD else '(overridden)'}")
    print(f"Auth enabled   : {AUTH_ENABLED}")
    print(f"Debug mode     : {ADMIN_DEBUG}")
    print(f"Listening on   : http://{ADMIN_HOST}:{ADMIN_PORT}")
    print("=" * 62)

    app.run(host=ADMIN_HOST, port=ADMIN_PORT, debug=ADMIN_DEBUG,
            threaded=True, use_reloader=False)


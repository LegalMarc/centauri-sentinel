"use strict";
const PALETTES = ["day", "dusk", "night", "bright"];
const VIEW_NAMES = { monitor: "Monitor", history: "History", settings: "Settings" };
const $ = (id) => document.getElementById(id);
// Initial values rendered by the server; live updates replace them.
const state = {
  threshold: Number(document.body.dataset.threshold) || 0,
  confirmCount: Number(document.body.dataset.confirmCount) || 1,
};

function fmtDuration(sec) {
  sec = Math.floor(Number(sec) || 0);
  if (sec <= 0) return "—";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}
function fmtClock(date) {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// Every fetch goes through here: a 401 means the session is gone, so go to the
// login page instead of leaving the polling silently failing.
function sentinelFetch(url, opts) {
  const headers = new Headers((opts && opts.headers) || {});
  headers.set("X-Requested-With", "sentinel");
  return fetch(url, { ...opts, headers }).then((res) => {
    if (res.status === 401) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    }
    return res;
  });
}

function toast(message, tone) {
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.tone = tone || "ok";
  el.setAttribute("role", "status");
  el.textContent = message;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 250); }, 3200);
}

/* ---- Views ------------------------------------------------------------- */
function showView(name, { push = false } = {}) {
  if (!VIEW_NAMES[name]) name = "monitor";
  document.querySelectorAll(".view-tab").forEach((tab) => {
    const on = tab.dataset.view === name;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
  });
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${name}`; });
  const bar = $("topbar");
  if (bar) bar.pageName = VIEW_NAMES[name];
  try { localStorage.setItem("sentinel_view", name); } catch (e) {}
  if (location.hash.slice(1) !== name) history[push ? "pushState" : "replaceState"](null, "", `#${name}`);
}

/* ---- Palette ----------------------------------------------------------- */
function cyclePalette() {
  const root = document.documentElement;
  const next = PALETTES[(PALETTES.indexOf(root.getAttribute("data-obc-theme")) + 1) % PALETTES.length];
  root.setAttribute("data-obc-theme", next);
  try { localStorage.setItem("sentinel_palette", next); } catch (e) {}
  toast(`Palette: ${next}`);
}

/* ---- Live telemetry ---------------------------------------------------- */
const WATCHER_TONES = { ARMED: "ok", PAUSED: "caution", WARMUP: "caution", STALLED: "alarm", CAMERA_OFFLINE: "alarm", OFFLINE: "alarm" };

function setTemp(prefix, value, target) {
  const v = Number(value), t = Number(target);
  $(`${prefix}-temp`).textContent = Number.isFinite(v) && v > 0.5 ? Math.round(v) : "—";
  $(`${prefix}-target`).textContent = Number.isFinite(t) && t > 0 ? `target ${Math.round(t)}°C` : "off";
  const track = $(`${prefix}-track`);
  const pct = Number.isFinite(t) && t > 0 && Number.isFinite(v) ? Math.min(100, (v / t) * 100) : 0;
  track.firstElementChild.style.width = `${pct}%`;
  track.dataset.atTarget = String(t > 0 && Math.abs(v - t) <= 2);
}

function setStreak(hits, total) {
  const el = $("streak");
  el.replaceChildren(...Array.from({ length: Math.min(total, 20) }, (_, i) => {
    const dot = document.createElement("i");
    if (i < hits) dot.className = "hit";
    return dot;
  }));
  $("streak-text").textContent = `${hits} of ${total}`;
}

function render(d) {
  const watcher = d.watcher_state || "IDLE";
  const chip = $("watcher-chip");
  chip.dataset.state = watcher;
  chip.dataset.tone = WATCHER_TONES[watcher] || "";
  chip.textContent = watcher.replace("_", " ").toLowerCase();
  $("watcher-state").textContent = watcher.replace("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
  const hb = $("heartbeat");
  hb.textContent = d.tick_age;
  hb.classList.toggle("stale", !!d.tick_age_stale);

  // Job
  const ps = d.print_state || "offline";
  const printing = ps === "printing", paused = ps === "paused";
  const done = ps === "completed" || ps === "complete";
  $("job-state").textContent = d.printer_state || ps;
  const file = d.filename && d.filename !== "—" ? d.filename : "No job loaded";
  $("job-file").textContent = file;
  $("job-file").title = d.filename || "";
  const progress = done ? 100 : Math.max(0, Math.min(100, Number(d.progress) || 0));
  $("progress-fill").style.width = `${progress}%`;
  $("progress").setAttribute("aria-valuenow", progress.toFixed(1));
  $("progress-pct").textContent = `${progress.toFixed(1)}%`;
  $("job-layer").textContent = d.total_layers > 0 ? `${d.current_layer} / ${d.total_layers}` : "—";
  const active = printing || paused || done;
  $("job-elapsed").textContent = active ? fmtDuration(d.elapsed_seconds) : "—";
  $("job-remaining").textContent = printing || paused ? fmtDuration(d.remaining_seconds) : "—";
  $("job-eta").textContent = printing && d.remaining_seconds > 0
    ? fmtClock(new Date(Date.now() + d.remaining_seconds * 1000)) : "—";

  // Temperatures
  setTemp("nozzle", d.extruder_temp, d.extruder_target);
  setTemp("bed", d.bed_temp, d.bed_target);

  // Failure watch
  if (typeof d.ml_score_threshold === "number") state.threshold = d.ml_score_threshold;
  if (typeof d.ml_confirm_count === "number") state.confirmCount = d.ml_confirm_count;
  const score = typeof d.last_ml_score === "number" && Number.isFinite(d.last_ml_score) ? d.last_ml_score : null;
  $("ml-score").textContent = score === null ? "—" : score.toFixed(2);
  $("ml-gauge-fill").style.width = `${score === null ? 0 : Math.min(100, score * 100)}%`;
  $("ml-gauge").dataset.over = String(score !== null && score >= state.threshold);
  $("ml-gauge").setAttribute("aria-valuenow", score === null ? "0" : score.toFixed(3));
  $("ml-gauge-threshold").style.left = `${state.threshold * 100}%`;
  $("ml-threshold").textContent = state.threshold.toFixed(2);
  const age = $("ml-age");
  age.textContent = `scored ${d.last_ml_score_age || "never"}`;
  age.classList.toggle("stale", !!d.last_ml_score_stale);
  setStreak(Number(d.confirm_streak) || 0, state.confirmCount);
  if (typeof d.detection_enabled === "boolean") {
    const chip = $("detection-chip");
    if (d.detection_enabled) {
      chip.dataset.tone = "ok";
      chip.textContent = "On";
    } else {
      chip.dataset.tone = "caution";
      chip.textContent = d.snooze_until ? `Snoozed to ${fmtClock(new Date(d.snooze_until))}` : "Off";
    }
  }

  // Camera
  const live = !!d.camera_connected;
  const img = $("camera-feed-img");
  $("cam-chip").dataset.live = String(live);
  $("cam-status").textContent = live ? "LIVE" : "OFFLINE";
  if (live && img.hidden) {
    img.hidden = false;
    $("camera-offline").hidden = true;
    img.src = "/stream?t=" + Date.now();
  } else if (!live) {
    img.hidden = true;
    $("camera-offline").hidden = false;
  }
  $("camera").dataset.state = paused ? "paused" : "live";
  $("cam-banner").hidden = !paused;
  $("cam-banner").textContent = "Paused · check the print, then resume";

  // Controls
  // Toggle the attribute (obc-button does not reflect its property) so CSS
  // can hide unavailable commands on phones.
  $("btn-pause").toggleAttribute("disabled", !printing);
  $("btn-resume").toggleAttribute("disabled", !paused);
  if (!stopArmed) $("btn-stop").toggleAttribute("disabled", !(printing || paused));

  // Model preview
  const panel = $("preview-panel");
  if (d.thumbnail_base64) {
    $("gcode-preview-img").src = `data:image/png;base64,${d.thumbnail_base64}`;
    panel.hidden = false;
  } else {
    panel.hidden = true;
  }
}

function poll() {
  sentinelFetch("/api/printer")
    .then((res) => { if (!res.ok) throw new Error(`status ${res.status}`); return res.json(); })
    .then(render)
    .catch((err) => console.error("Live update failed:", err));
}

/* ---- Camera ------------------------------------------------------------ */
function onFrameLoad() {
  const img = $("camera-feed-img");
  if (!img.naturalWidth) return;
  $("camera").style.setProperty("--ar", `${img.naturalWidth} / ${img.naturalHeight}`);
  document.querySelector(".monitor").style.setProperty("--arn", String(img.naturalWidth / img.naturalHeight));
  $("cam-res").textContent = `${img.naturalWidth}×${img.naturalHeight}`;
}
function onCameraError() {
  // An <img> cannot read its status code: probe the API so an expired session
  // lands on the login page instead of a permanent "camera offline".
  sentinelFetch("/api/printer").catch(() => {});
  $("camera-feed-img").hidden = true;
  $("camera-offline").hidden = false;
  $("cam-chip").dataset.live = "false";
  $("cam-status").textContent = "OFFLINE";
}
function toggleFullscreen() {
  const cam = $("camera");
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else if (cam.requestFullscreen) cam.requestFullscreen().catch(() => toast("Full screen is not available here", "warning"));
}

/* ---- Commands ---------------------------------------------------------- */
function sendControl(action, label) {
  return sentinelFetch(`/api/control/${action}`, { method: "POST" })
    .then(async (res) => {
      const body = await res.json().catch(() => ({}));
      if (res.ok) { toast(`${label} sent to the printer`); setTimeout(poll, 800); }
      else toast(body.detail || `${label} failed`, "alarm");
    })
    .catch((err) => toast(`${label} failed: ${err.message}`, "alarm"));
}

let stopArmed = null;
function onStop() {
  const btn = $("btn-stop"), label = $("stop-label");
  if (btn.disabled) return;
  if (stopArmed) {
    if (Date.now() - stopArmed.at < 600) return;
    clearInterval(stopArmed.timer);
    stopArmed = null;
    btn.classList.remove("arming");
    label.textContent = "Stopping…";
    btn.toggleAttribute("disabled", true);
    sendControl("stop", "Stop").finally(() => { label.textContent = "Stop"; });
    return;
  }
  let left = 5;
  btn.classList.add("arming");
  label.textContent = `Confirm stop · ${left}`;
  stopArmed = {
    at: Date.now(),
    timer: setInterval(() => {
      left -= 1;
      if (left <= 0) {
        clearInterval(stopArmed.timer);
        stopArmed = null;
        btn.classList.remove("arming");
        label.textContent = "Stop";
      } else {
        label.textContent = `Confirm stop · ${left}`;
      }
    }, 1000),
  };
}

function onSnooze() {
  const select = $("snooze-duration");
  const seconds = parseInt(select.value, 10);
  sentinelFetch("/api/control/snooze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seconds }),
  })
    .then((res) => {
      if (res.ok) { toast(`Detection snoozed for ${select.options[select.selectedIndex].text}`, "warning"); poll(); }
      else toast("Snooze failed", "alarm");
    })
    .catch((err) => toast(`Snooze failed: ${err.message}`, "alarm"));
}

let clearArmed = false;
function onClearHistory() {
  const btn = $("btn-clear-data");
  if (!clearArmed) {
    clearArmed = true;
    btn.textContent = "Click again to delete everything";
    setTimeout(() => { clearArmed = false; btn.textContent = "Clear history"; }, 5000);
    return;
  }
  btn.disabled = true;
  btn.textContent = "Clearing…";
  sentinelFetch("/api/data/clear", { method: "DELETE" })
    .then((res) => {
      if (res.ok) { toast("History cleared"); setTimeout(() => location.reload(), 900); }
      else { toast("Could not clear history", "alarm"); btn.disabled = false; btn.textContent = "Clear history"; }
    })
    .catch((err) => { toast(`Could not clear history: ${err.message}`, "alarm"); btn.disabled = false; btn.textContent = "Clear history"; });
}

/* ---- Settings ---------------------------------------------------------- */
function validateInput(input) {
  const ok = input.checkValidity();
  input.setAttribute("aria-invalid", String(!ok));
  return ok;
}
function saveSettings(event) {
  if (event) event.preventDefault();
  // form.elements includes the printer address, which joins via form="settings-form".
  const inputs = [...$("settings-form").elements].filter((el) => el.classList.contains("input"));
  const allValid = inputs.map(validateInput).every(Boolean);
  if (!allValid) { toast("Fix the highlighted fields first", "warning"); return; }
  const payload = {
    printer_ip: $("input-printer-ip").value.trim(),
    ml_score_threshold: parseFloat($("input-ml-score-threshold").value),
    ml_confirm_count: parseInt($("input-ml-confirm-count").value, 10),
    ml_poll_interval_seconds: parseInt($("input-ml-poll-interval").value, 10),
    detection_warmup_seconds: parseInt($("input-detection-warmup").value, 10),
  };
  // Without the component bundle the toggle has no state; omit the field
  // rather than overwrite a saved "on" with false.
  if (customElements.get("obc-toggle-switch")) {
    payload.ml_failure_auto_stop = !!$("input-ml-failure-auto-stop").checked;
  }
  sentinelFetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(async (res) => {
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        state.threshold = payload.ml_score_threshold;
        state.confirmCount = payload.ml_confirm_count;
        toast("Settings saved");
        poll();
      } else {
        toast(data.detail || "Settings were not saved", "alarm");
      }
    })
    .catch((err) => toast(`Settings were not saved: ${err.message}`, "alarm"));
}

/* ---- Wiring (no inline handlers: strict CSP) ---------------------------- */
// Event times arrive as UTC; show them in the viewer's local time.
document.querySelectorAll("time[datetime]").forEach((el) => {
  const when = new Date(el.getAttribute("datetime"));
  if (Number.isNaN(when.getTime())) return;
  const today = new Date().toDateString() === when.toDateString();
  el.textContent = today ? fmtClock(when) : when.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + fmtClock(when);
  el.title = when.toLocaleString();
});
document.querySelectorAll("[data-goto]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showView(a.dataset.goto, { push: true }); }));
document.querySelectorAll("[data-duration]").forEach((el) => { el.textContent = fmtDuration(el.dataset.duration); });
$("job-remaining").textContent = fmtDuration($("job-remaining").dataset.seconds);
setStreak(0, state.confirmCount);

document.querySelectorAll(".view-tab").forEach((tab) => tab.addEventListener("click", () => showView(tab.dataset.view, { push: true })));
$("btn-pause").addEventListener("click", (e) => { if (!e.currentTarget.disabled) sendControl("pause", "Pause"); });
$("btn-resume").addEventListener("click", (e) => { if (!e.currentTarget.disabled) sendControl("resume", "Resume"); });
$("btn-stop").addEventListener("click", onStop);
$("btn-snooze").addEventListener("click", onSnooze);
$("btn-clear-data").addEventListener("click", onClearHistory);
$("btn-save").addEventListener("click", saveSettings);
$("settings-form").addEventListener("submit", saveSettings);
[...$("settings-form").elements].filter((el) => el.classList.contains("input")).forEach((input) => {
  input.addEventListener("blur", () => validateInput(input));
  input.addEventListener("input", () => { if (input.getAttribute("aria-invalid") === "true") validateInput(input); });
});
$("btn-fullscreen").addEventListener("click", toggleFullscreen);
// Keep the camera sized so the job strip under it stays above the fold.
new ResizeObserver(([entry]) => {
  document.querySelector(".monitor").style.setProperty("--job-h", `${Math.ceil(entry.borderBoxSize[0].blockSize)}px`);
}).observe(document.querySelector(".job"));
$("camera-feed-img").addEventListener("load", onFrameLoad);
$("camera-feed-img").addEventListener("error", onCameraError);

const topbar = $("topbar");
topbar.addEventListener("dimming-button-clicked", cyclePalette);
// Narrow screens fold the dimming button into the top bar's overflow button.
topbar.addEventListener("left-more-button-clicked", cyclePalette);
topbar.addEventListener("menu-button-clicked", () => showView("monitor"));
const userMenu = $("user-menu");
if (userMenu) {
  topbar.addEventListener("user-button-clicked", (e) => {
    e.stopPropagation();
    userMenu.hidden = !userMenu.hidden;
    topbar.userButtonActivated = !userMenu.hidden;
  });
  document.addEventListener("click", (e) => {
    // The click that opened the menu also reaches here, retargeted to the top
    // bar (its button lives in shadow DOM), so top-bar clicks never close it.
    if (!userMenu.hidden && !userMenu.contains(e.target) && e.target !== topbar) {
      userMenu.hidden = true;
      topbar.userButtonActivated = false;
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !userMenu.hidden) { userMenu.hidden = true; topbar.userButtonActivated = false; }
  });
}

let initialView = location.hash.slice(1);
if (!VIEW_NAMES[initialView]) { try { initialView = localStorage.getItem("sentinel_view") || "monitor"; } catch (e) { initialView = "monitor"; } }
showView(initialView);
// Back/forward and #view links switch views without a reload.
window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
poll();
setInterval(poll, 3000);

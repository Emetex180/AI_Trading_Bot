/* Dashboard live controls.
 *
 * One polling loop drives every control surface:
 *   - the navbar live pill (all pages)
 *   - the "Live session" card on the dashboard (start/stop, counters)
 *   - the backtest form, progress bar and job badge on the backtests page
 *
 * All job state comes from GET /api/status, which reads in-memory state and
 * never touches MT5 -- so polling stays safe with the terminal closed.
 */
(function () {
  "use strict";

  var POLL_MS = 5000;
  var lastSignalId = null;
  var backtestWasActive = false;

  /* Labels only -- the coloured status dot is markup, set in base.html and
   * left alone here so it is never rewritten out of the pill. */
  var LIVE_TONE = {
    idle: ["secondary", "Live: idle"],
    starting: ["info", "Live: starting"],
    running: ["success", "Live: running"],
    stopping: ["warning", "Live: stopping"],
    stopped: ["secondary", "Live: stopped"],
    error: ["danger", "Live: error"]
  };

  var BACKTEST_TONE = {
    idle: "secondary", queued: "warning", starting: "info",
    running: "info", done: "success", error: "danger"
  };

  var ACTIVE_BACKTEST = ["queued", "starting", "running"];

  function byId(id) { return document.getElementById(id); }
  function text(id, value) { var el = byId(id); if (el) el.textContent = value; }
  function show(id, message, tone) {
    var el = byId(id);
    if (!el) return;
    el.innerHTML = message ? '<span class="text-' + tone + '">' + message + "</span>" : "";
  }
  function setTone(id, tone) {
    var el = byId(id);
    if (!el) return;
    el.className = "badge text-bg-" + tone;
  }

  /* ---------------------------------------------------------------- */
  /* Transport                                                        */
  /* ---------------------------------------------------------------- */
  function getStatus(afterId) {
    var url = "/api/status";
    if (afterId !== null) url += "?after_signal_id=" + encodeURIComponent(afterId);
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      });
  }

  function post(path, body) {
    var opts = { method: "POST", headers: { Accept: "application/json" } };
    if (body) {
      opts.headers["Content-Type"] = "application/x-www-form-urlencoded";
      opts.body = new URLSearchParams(body).toString();
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; })
        .then(function (data) { return { ok: r.ok, data: data }; });
    });
  }

  /* ---------------------------------------------------------------- */
  /* Rendering                                                        */
  /* ---------------------------------------------------------------- */
  function renderPill(live) {
    var el = byId("live-pill");
    if (!el) return;
    var tone = LIVE_TONE[live.state] || LIVE_TONE.idle;
    el.className = "badge text-bg-" + tone[0] + " ms-2";
    el.setAttribute("data-state", live.state);
    text("live-pill-text", tone[1] +
      (live.signals_session ? " - " + live.signals_session + " signal(s)" : ""));
  }

  function renderLive(live) {
    var badge = byId("live-state-badge");
    if (!badge) return;

    badge.textContent = live.state;
    setTone("live-state-badge", (LIVE_TONE[live.state] || LIVE_TONE.idle)[0]);

    text("live-started", live.started_at_ny || "—");
    text("live-assets", (live.assets && live.assets.length) ? live.assets.join(", ") : "—");
    text("live-last-candle", live.last_candle_ny || "—");
    text("live-signals", String(live.signals_session));

    var active = ["starting", "running", "stopping"].indexOf(live.state) !== -1;
    var start = byId("live-start");
    var stop = byId("live-stop");
    if (start) start.disabled = active;
    if (stop) stop.disabled = !active;

    var err = byId("live-error");
    if (err) {
      err.textContent = live.last_error || "";
      err.classList.toggle("d-none", !live.last_error);
    }
  }

  function renderBacktestProgress(bt) {
    var box = byId("backtest-progress");
    if (!box) return;

    var active = ACTIVE_BACKTEST.indexOf(bt.state) !== -1;
    box.classList.toggle("d-none", !active && bt.state !== "done" && bt.state !== "error");

    var pct = bt.progress_total
      ? Math.round((bt.progress_done / bt.progress_total) * 100) : 0;
    if (bt.state === "done") pct = 100;
    if (bt.state === "error") pct = 100;

    var bar = byId("backtest-progress-bar");
    if (!bar) return;
    bar.style.width = pct + "%";
    bar.className = "progress-bar" +
      (bt.state === "error" ? " bg-danger" : (bt.state === "done" ? " bg-success" : ""));
  }

  function renderBacktest(bt) {
    var badge = byId("backtest-state-badge");
    if (!badge) return;
    badge.textContent = bt.state;
    setTone("backtest-state-badge", BACKTEST_TONE[bt.state] || "secondary");

    var active = ACTIVE_BACKTEST.indexOf(bt.state) !== -1;
    var run = byId("bt-run");
    if (run) run.disabled = active;

    renderBacktestProgress(bt);

    if (bt.state === "queued") {
      show("backtest-message", bt.queued_reason || "Queued.", "warning");
    } else if (bt.state === "running" || bt.state === "starting") {
      var progress = bt.progress_total
        ? " (" + bt.progress_done + "/" + bt.progress_total + " assets)" : "";
      show("backtest-message", "Running" + progress + "…", "info");
    } else if (bt.state === "done") {
      show("backtest-message", "Finished " + (bt.finished_at_ny || "") +
        (bt.batch_id ? " — opening the comparison…" : ""), "success");
    }

    var err = byId("backtest-error");
    if (err) {
      err.textContent = bt.last_error || "";
      err.classList.toggle("d-none", !bt.last_error);
    }

    // A finished run produced new rows. Go straight to the comparison for that
    // batch instead of reloading a list and leaving the user to find them.
    // Guarded on the run form so this only fires on the backtests page.
    if (backtestWasActive && !active && byId("backtest-form")) {
      var target = bt.batch_id
        ? "/backtests/batch/" + encodeURIComponent(bt.batch_id) : null;
      setTimeout(function () {
        if (target) { window.location.href = target; }
        else { window.location.reload(); }
      }, 1200);
    }
    backtestWasActive = active;
  }

  function renderNewSignals(items, lastId) {
    var host = byId("new-signals");
    var total = byId("stat-signals-total");
    if (total) total.textContent = String(lastId);
    if (!host || !items.length) return;

    var rows = items.map(function (s) {
      return '<li><a href="' + s.url + '">' + s.asset + " " +
        s.direction.toUpperCase() + " @ " + s.entry + " (RR " +
        (s.rr ? s.rr.toFixed(2) : "-") + ")</a> — " + s.entry_time_ny +
        " · " + s.session + "</li>";
    }).join("");

    host.innerHTML =
      '<div class="alert alert-info alert-dismissible fade show py-2 mb-0">' +
      "<strong>" + items.length + " new signal" + (items.length > 1 ? "s" : "") +
      "</strong> — check Telegram for the alert." +
      '<ul class="mb-0 mt-1 small">' + rows + "</ul>" +
      '<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>';
  }

  function renderTelegram(tg) {
    if (!tg || tg.configured) return;
    // Only warn on the dashboard, and only if the server-rendered banner is absent.
    var host = byId("live-message");
    if (host && !document.querySelector(".alert-warning")) {
      show("live-message", "Telegram is not configured — no alerts will be sent.", "warning");
    }
  }

  /* ---------------------------------------------------------------- */
  /* Loop                                                             */
  /* ---------------------------------------------------------------- */
  function tick() {
    getStatus(lastSignalId)
      .then(function (data) {
        renderPill(data.live);
        renderLive(data.live);
        renderBacktest(data.backtest);
        renderTelegram(data.telegram);
        if (data.signals) {
          renderNewSignals(data.signals.new || [], data.signals.last_id);
          lastSignalId = data.signals.last_id;
        }
      })
      .catch(function () {
        text("live-pill-text", "Live: unreachable");
      });
  }

  /* ---------------------------------------------------------------- */
  /* Controls                                                         */
  /* ---------------------------------------------------------------- */
  var startBtn = byId("live-start");
  if (startBtn) {
    startBtn.addEventListener("click", function () {
      show("live-message", "Starting…", "info");
      post("/api/live/start").then(function (r) {
        show("live-message", r.data.message || "Started.",
          r.ok ? "success" : "danger");
        tick();
      });
    });
  }

  var stopBtn = byId("live-stop");
  if (stopBtn) {
    stopBtn.addEventListener("click", function () {
      show("live-message", "Stopping…", "info");
      post("/api/live/stop").then(function (r) {
        show("live-message", r.data.message || "Stopped.",
          r.ok ? "success" : "danger");
        tick();
      });
    });
  }

  var form = byId("backtest-form");
  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var payload = {
        asset: byId("bt-asset").value,
        bars: byId("bt-bars").value,
        max_hold_m1: byId("bt-hold").value
      };
      show("backtest-message", "Submitting…", "info");
      post("/api/backtest/run", payload).then(function (r) {
        show("backtest-message", r.data.message || "",
          r.ok ? (r.data.queued ? "warning" : "info") : "danger");
        backtestWasActive = r.ok;
        tick();
      });
    });
  }

  tick();
  setInterval(tick, POLL_MS);
})();

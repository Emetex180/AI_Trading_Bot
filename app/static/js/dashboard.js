/* Dashboard live controls.
 *
 * One polling loop drives every control surface:
 *   - the topbar live pill (all pages)
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
  var accountWasRunning = false;
  /* One-shot: the dashboard asks the server to read the account by itself on
   * first load, so the tile is not left on an em dash until the user finds the
   * Refresh button. See the guard in renderAccount(). */
  var accountAutoRequested = false;
  /* Last known position of the master trading switch, so the toggle handler can
   * ask for the opposite without re-reading the DOM it just wrote. */
  var autoTradingOn = false;

  /* ------------------------------------------------------------------ */
  /* Chart theming                                                       */
  /*                                                                     */
  /* Published on `window` so the per-page inline chart scripts inherit a */
  /* palette instead of each hard-coding a colour. Set before any chart   */
  /* is constructed: this file is loaded in <head> order, ahead of the    */
  /* inline scripts at the end of each page.                              */
  /* ------------------------------------------------------------------ */
  var CHART = {
    accent: "#818cf8",
    accentFill: "rgba(129, 140, 248, .12)",
    palette: ["#818cf8", "#34d399", "#fbbf24", "#fb7185", "#c084fc",
              "#38bdf8", "#fb923c", "#a3e635"]
  };
  window.ictChartTheme = CHART;

  if (window.Chart) {
    var C = window.Chart;
    C.defaults.color = "#97a5bd";
    C.defaults.borderColor = "rgba(36, 49, 74, .85)";
    C.defaults.font.family = getComputedStyle(document.body).fontFamily;
    C.defaults.font.size = 11;
    C.defaults.plugins.tooltip.backgroundColor = "#182131";
    C.defaults.plugins.tooltip.borderColor = "#24314a";
    C.defaults.plugins.tooltip.borderWidth = 1;
    C.defaults.plugins.tooltip.titleColor = "#e8edf7";
    C.defaults.plugins.tooltip.bodyColor = "#97a5bd";
    C.defaults.plugins.tooltip.padding = 10;
    C.defaults.plugins.tooltip.cornerRadius = 8;
    C.defaults.plugins.tooltip.displayColors = false;
    C.defaults.plugins.legend.labels.color = "#97a5bd";
  }

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

  var BROKER_TONE = {
    idle: "secondary", running: "info", done: "success", error: "danger"
  };

  function byId(id) { return document.getElementById(id); }
  function text(id, value) { var el = byId(id); if (el) el.textContent = value; }
  function show(id, message, tone) {
    var el = byId(id);
    if (!el) return;
    el.innerHTML = message ? '<span class="text-' + tone + '">' + message + "</span>" : "";
  }

  /* Broker symbol names come from the terminal, not from this codebase, so they
   * are escaped rather than interpolated raw into the rows built below. */
  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Contract numbers arrive as JSON floats; trim the trailing zeros a broker's
   * 0.010000 would otherwise show. */
  function num(value) {
    if (value === null || value === undefined) return "-";
    var n = Number(value);
    if (!isFinite(n)) return "-";
    return String(parseFloat(n.toFixed(4)));
  }

  /* Account figures. Null means "never read from the terminal", which must not
   * render as 0.00 — hence the em dash, matching the `money` Jinja filter that
   * paints the server-rendered first frame. */
  function money(value) {
    if (value === null || value === undefined) return "—";
    var n = Number(value);
    if (!isFinite(n)) return "—";
    return n.toLocaleString(undefined, { minimumFractionDigits: 2,
                                         maximumFractionDigits: 2 });
  }

  /* Tones are classes, not Bootstrap utilities, so `setTone` rebuilds the whole
   * class list -- hence the explicit base. */
  function setTone(id, tone, base) {
    var el = byId(id);
    if (!el) return;
    el.className = (base || "badge") + " tone-" + tone;
  }

  /* Price precision per asset, taken from the registry the server sends with
   * every poll. Without this a live signal would read "21480.25000001" in the
   * alert while the same price in the table reads "21480.25". */
  var DIGITS = {};

  function price(value, asset) {
    if (value === null || value === undefined) return "-";
    var digits = DIGITS[asset];
    if (digits === undefined) digits = 4;
    return Number(value).toFixed(digits);
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
  function renderAssets(list) {
    if (!list) return;
    DIGITS = {};
    list.forEach(function (a) {
      if (a && a.name) DIGITS[a.name] = a.digits || 0;
    });
  }

  function renderPill(live) {
    var el = byId("live-pill");
    if (!el) return;
    var tone = LIVE_TONE[live.state] || LIVE_TONE.idle;
    el.className = "pill tone-" + tone[0];
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

  function renderNewSignals(items, lastId, total) {
    var host = byId("new-signals");

    // The tile shows the signal *count*; `lastId` is only the polling cursor.
    var tile = byId("stat-signals-total");
    if (tile && total !== undefined && total !== null) {
      tile.textContent = String(total);
    }
    if (!host || !items.length) return;

    var rows = items.map(function (s) {
      return '<li><a href="' + s.url + '">' + s.asset + " " +
        s.direction.toUpperCase() + " @ " + price(s.entry, s.asset) + " (RR " +
        (s.rr ? s.rr.toFixed(2) : "-") + ")</a> — " + s.entry_time_ny +
        " · " + s.session + "</li>";
    }).join("");

    host.innerHTML =
      '<div class="alert alert-info alert-dismissible fade show mb-0">' +
      "<strong>" + items.length + " new signal" + (items.length > 1 ? "s" : "") +
      "</strong> — check Telegram for the alert." +
      '<ul class="mb-0 mt-2 small">' + rows + "</ul>" +
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
  /* Assets page                                                      */
  /*                                                                    */
  /* The catalogue is held in memory: the search box and the "hide      */
  /* already added" filter re-render locally rather than re-reading MT5, */
  /* which would cost a terminal connect per keystroke.                 */
  /* ---------------------------------------------------------------- */
  var catalog = [];
  var brokerWasRunning = false;

  function readEmbeddedCatalog() {
    var el = byId("broker-catalog");
    if (!el) return [];
    try { return JSON.parse(el.textContent) || []; } catch (e) { return []; }
  }

  /* Cap what reaches the DOM. A broker can list thousands of instruments and
   * only a screenful is ever visible; the search box is how the rest are found. */
  var BROKER_ROW_LIMIT = 500;

  function renderCatalog() {
    var host = byId("broker-rows");
    if (!host) return;

    var search = byId("broker-search");
    var term = (search && search.value ? search.value : "").trim().toUpperCase();
    var hide = byId("broker-hide-added");
    var hideAdded = !!(hide && hide.checked);

    var matched = catalog.filter(function (s) {
      if (hideAdded && s.in_registry) return false;
      return !term || String(s.name || "").toUpperCase().indexOf(term) !== -1;
    });

    if (!matched.length) {
      host.innerHTML = '<tr><td colspan="5" class="empty-state">' +
        (catalog.length
          ? "No symbol matches that search."
          : "No symbols loaded yet. Click <strong>Scan broker</strong> to read " +
            "the list from your terminal.") + "</td></tr>";
      return;
    }

    var shown = matched.slice(0, BROKER_ROW_LIMIT);
    host.innerHTML = shown.map(function (s) {
      return "<tr>" +
        '<td class="fw-semibold">' + esc(s.name) + "</td>" +
        '<td class="mono">' + num(s.digits) + "</td>" +
        '<td class="mono text-muted">' + num(s.trade_contract_size) + "</td>" +
        '<td class="mono text-muted">' + num(s.volume_min) + "</td>" +
        '<td class="text-end">' + (s.in_registry
          ? '<span class="badge tone-secondary">in registry</span>'
          : '<button type="button" class="btn btn-sm btn-outline-primary" ' +
            'data-broker-add="' + esc(s.name) + '">Add</button>') +
        "</td></tr>";
    }).join("") + (matched.length > shown.length
      ? '<tr><td colspan="5" class="empty-state">Showing the first ' +
        BROKER_ROW_LIMIT + " of " + matched.length + " matches — narrow the " +
        "search to see more.</td></tr>"
      : "");
  }

  function renderRegistry(list) {
    var host = byId("registry-rows");
    if (!host || !list) return;

    var enabled = 0;
    list.forEach(function (a) { if (a.enabled) enabled += 1; });
    text("assets-enabled-count", enabled + " / " + list.length);

    if (!list.length) {
      host.innerHTML = '<tr><td colspan="5" class="empty-state">' +
        "The registry is empty. Add a symbol from the broker list.</td></tr>";
      return;
    }

    host.innerHTML = list.map(function (a) {
      return "<tr>" +
        '<td class="fw-semibold">' + esc(a.name) + "</td>" +
        '<td class="mono text-muted">' + esc(a.broker_symbol) + "</td>" +
        '<td class="mono">' + num(a.digits) + "</td>" +
        '<td><span class="badge tone-' +
          (a.enabled ? "success" : "secondary") + '">' +
          (a.enabled ? "enabled" : "disabled") + "</span></td>" +
        '<td class="text-end">' +
          '<button type="button" class="btn btn-sm btn-outline-' +
            (a.enabled ? "secondary" : "success") +
            '" data-registry-toggle data-asset="' + esc(a.name) +
            '" data-enabled="' + (a.enabled ? "true" : "false") + '">' +
            (a.enabled ? "Disable" : "Enable") + "</button> " +
          '<button type="button" class="btn btn-sm btn-outline-danger" ' +
            'data-registry-remove data-asset="' + esc(a.name) +
            '">Remove</button>' +
        "</td></tr>";
    }).join("");
  }

  function loadCatalog() {
    return fetch("/api/assets/broker", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        catalog = data.symbols || [];
        renderCatalog();
      })
      .catch(function () { /* the poll keeps trying; nothing to report here */ });
  }

  function accountWho(account) {
    var parts = [account.name, account.login, account.server].filter(function (v) {
      return v !== null && v !== undefined && v !== "";
    });
    return parts.length ? parts.join(" · ")
      : "No account read yet — press Refresh to read it from the terminal.";
  }

  function renderAccount(account) {
    if (!account) return;
    var card = byId("account-card");
    if (!card) return;                       /* not the dashboard */

    /* MT5 owns the account and nothing reads it unprompted, so the dashboard
     * asks on the user's behalf: one read as soon as the first poll reports the
     * account has never been read. Without this the tile sits on an em dash
     * until the user finds the Refresh button, which reads as "not connected".
     *
     * Guarded three ways so the five-second poll cannot queue a read per tick:
     * the one-shot flag, `state` (which is "running" until the read settles),
     * and `fetched_at_utc` (set once a read has ever succeeded). A refusal is
     * harmless -- the route returns "live_running" and the live worker publishes
     * a fresher snapshot anyway. */
    if (!accountAutoRequested && account.state === "idle"
        && !account.fetched_at_utc) {
      accountAutoRequested = true;
      requestAccountRefresh();
    }

    var running = account.state === "running";
    var btn = byId("account-refresh");
    if (btn) btn.disabled = running;

    /* The figures survive a refresh in progress -- the server keeps the previous
     * snapshot rather than blanking it -- so they are only rewritten once the
     * read has settled. Rewriting mid-flight would flash the tile to a dash. */
    if (!running) {
      ["balance", "equity", "margin_free"].forEach(function (field) {
        var id = field.replace("_", "-");
        text("account-" + id, money(account[field]));
        text("account-" + id + "-currency", account.currency || "");
      });
      text("account-who", accountWho(account));
    }

    if (accountWasRunning && !running) {
      if (account.state === "error") {
        show("account-message",
             account.last_error || "Could not read the account.", "danger");
      } else {
        show("account-message", "Updated " + (account.fetched_at_ny || "") + ".",
             "success");
      }
    }
    accountWasRunning = running;

    text("account-fetched", account.fetched_at_ny
      ? "as of " + account.fetched_at_ny : "not read yet");

    var err = byId("account-error");
    if (err) {
      err.textContent = account.last_error || "";
      err.classList.toggle("d-none", !account.last_error);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Auto-trading switch                                              */
  /* ---------------------------------------------------------------- */

  /* Shown twice -- the topbar pill on every page, and the card on the
   * dashboard -- and both are rewritten on every poll. The pill is
   * server-rendered for the first paint, but it would go stale the instant the
   * override changed, and this is the one indicator in the UI that must never
   * lie about whether the bot can trade. */
  function renderAutoTrading(data) {
    var on = !!data.auto_trading;
    autoTradingOn = on;

    var pill = byId("auto-trading-pill");
    if (pill) pill.className = "pill tone-" + (on ? "danger" : "success");
    text("auto-trading-pill-text",
         on ? "AUTO-TRADING ENABLED" : "AUTO-TRADING OFF · ALERT ONLY");

    var badge = byId("auto-trading-state-badge");
    if (badge) {
      badge.textContent = on ? "enabled" : "off";
      setTone("auto-trading-state-badge", on ? "danger" : "success");
    }

    var toggle = byId("auto-trading-toggle");
    if (toggle) {
      toggle.textContent = on ? "Turn off" : "Turn on";
      toggle.className = "btn btn-sm btn-outline-" + (on ? "secondary" : "danger");
      toggle.disabled = false;
    }

    var override = data.auto_trading_override;
    var reset = byId("auto-trading-reset");
    if (reset) {
      reset.classList.toggle("d-none", override === null || override === undefined);
      reset.disabled = false;
    }

    text("auto-trading-baseline", data.auto_trading_baseline ? "true" : "false");
    text("auto-trading-override",
         (override === null || override === undefined) ? "follow .env"
           : (override ? "forced on" : "forced off"));
  }

  /* Whether MT5 itself would accept an order. Kept separate from the switch
   * above because it is a different failure: the switch can be on with the
   * terminal still rejecting everything, and that reads as a silent dead end
   * unless it is called out. */
  function renderTerminalGate(account) {
    var warn = byId("auto-trading-terminal-warning");
    if (!warn) return;
    /* Only `false` warns. `null` means the terminal has not been asked yet, and
     * rendering that as "blocked" would cry wolf on every fresh page load. */
    warn.classList.toggle("d-none", account.trade_allowed !== false);
  }

  function renderBroker(b) {
    if (!b) return;
    var badge = byId("broker-state-badge");
    if (badge) {
      badge.textContent = b.state;
      setTone("broker-state-badge", BROKER_TONE[b.state] || "secondary");
    }

    var running = b.state === "running";
    var scan = byId("broker-scan");
    if (scan) scan.disabled = running;
    if (running) show("broker-message", "Reading the terminal's symbol list…", "info");

    // A scan that has just stopped needs its result pulled in, and the button
    // released — one start per scan, so two jobs can never compete.
    if (brokerWasRunning && !running) {
      if (b.state === "error") {
        show("broker-message", b.last_error || "The scan failed.", "danger");
      } else {
        show("broker-message", "Loaded " + b.n_symbols + " symbol(s).", "success");
        loadCatalog();
      }
    }
    brokerWasRunning = running;

    text("assets-broker-count", String(b.n_symbols || 0));
    var err = byId("broker-error");
    if (err) {
      err.textContent = b.last_error || "";
      err.classList.toggle("d-none", !b.last_error);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Loop                                                             */
  /* ---------------------------------------------------------------- */
  function tick() {
    getStatus(lastSignalId)
      .then(function (data) {
        renderAssets(data.assets);
        renderPill(data.live);
        renderLive(data.live);
        renderAccount(data.account);
        renderAutoTrading(data);
        renderTerminalGate(data.account);
        renderBacktest(data.backtest);
        renderBroker(data.broker);
        renderTelegram(data.telegram);
        if (data.signals) {
          renderNewSignals(data.signals.new || [], data.signals.last_id,
                           data.signals.total);
          lastSignalId = data.signals.last_id;
        }
      })
      .catch(function () {
        var pill = byId("live-pill");
        if (pill) pill.className = "pill tone-danger";
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
      // The date range is optional markup; when both fields are filled the
      // server prefers them over the bar count.
      var start = byId("bt-start");
      var end = byId("bt-end");
      if (start && end) {
        payload.start = start.value.trim();
        payload.end = end.value.trim();
      }
      show("backtest-message", "Submitting…", "info");
      post("/api/backtest/run", payload).then(function (r) {
        show("backtest-message", r.data.message || "",
          r.ok ? (r.data.queued ? "warning" : "info") : "danger");
        backtestWasActive = r.ok;
        tick();
      });
    });
  }

  /* ---------------------------------------------------------------- */
  /* Dashboard controls                                               */
  /* ---------------------------------------------------------------- */

  /* The master trading switch. Turning it ON is confirmed; turning it off is
   * not -- the fail-safe direction should never have friction, or the natural
   * reflex under pressure is to click through the dialog anyway. */
  var autoTradingToggle = byId("auto-trading-toggle");
  if (autoTradingToggle) {
    autoTradingToggle.addEventListener("click", function () {
      var enabling = !autoTradingOn;
      if (enabling && !window.confirm(
            "Turn auto-trading ON?\n\nApproved signals will be sent to your " +
            "broker as real orders. This override lasts only until the app " +
            "restarts -- it is not written to .env.")) {
        return;
      }
      autoTradingToggle.disabled = true;
      show("auto-trading-message", enabling ? "Turning on…" : "Turning off…",
           "info");
      post("/api/auto-trading", { enabled: enabling ? "true" : "false" })
        .then(function (r) {
          show("auto-trading-message",
               r.ok ? (enabling ? "Auto-trading on." : "Auto-trading off.")
                    : "Could not change it.",
               r.ok ? "success" : "danger");
          tick();
        });
    });
  }

  /* Hands control back to .env without a restart, which is the only way to
   * reach "no override" once one has been set. */
  var autoTradingReset = byId("auto-trading-reset");
  if (autoTradingReset) {
    autoTradingReset.addEventListener("click", function () {
      autoTradingReset.disabled = true;
      show("auto-trading-message", "Reverting to .env…", "info");
      post("/api/auto-trading", {}).then(function (r) {
        show("auto-trading-message",
             r.ok ? "Following .env again." : "Could not revert.",
             r.ok ? "success" : "danger");
        tick();
      });
    });
  }

  var accountBtn = byId("account-refresh");

  /* Named rather than inline: the click handler and the first-load auto-request
   * in renderAccount() are the same request, and two copies would drift. */
  function requestAccountRefresh() {
    show("account-message", "Reading the account…", "info");
    // Left disabled until the poll reports the read settled, so two clicks
    // cannot fire competing jobs against the same terminal.
    if (accountBtn) accountBtn.disabled = true;
    post("/api/account/refresh").then(function (r) {
      /* A refusal is not always a failure: while a live session runs the
       * terminal is its to own, and it publishes a fresher figure of its own,
       * so that one is reported as information rather than an error. */
      var refused = r.data.reason === "live_running";
      show("account-message", r.data.message || "",
           r.ok ? "info" : (refused ? "info" : "danger"));
      if (!r.ok && accountBtn) accountBtn.disabled = false;
    });
  }

  if (accountBtn) {
    accountBtn.addEventListener("click", requestAccountRefresh);
  }

  /* ---------------------------------------------------------------- */
  /* Assets page controls                                             */
  /* ---------------------------------------------------------------- */
  var scanBtn = byId("broker-scan");

  /* Named for the same reason as requestAccountRefresh(): the click handler and
   * the first-load auto-scan below are one request, not two copies of it. */
  function requestBrokerScan() {
    show("broker-message", "Reading the terminal's symbol list…", "info");
    // Stay disabled until the poll reports the scan finished, so a second
    // click cannot queue a competing job against the same terminal.
    if (scanBtn) scanBtn.disabled = true;
    post("/api/assets/broker/scan").then(function (r) {
      if (r.ok) return;
      if (scanBtn) scanBtn.disabled = false;
      /* Being refused by a live session is not a failure -- the terminal is
       * legitimately busy -- so it is reported as information, matching how the
       * account refresh treats the same refusal. */
      var refused = r.data.reason === "live_running";
      show("broker-message", r.data.message || "Could not start the scan.",
           refused ? "info" : "danger");
    });
  }

  if (scanBtn) {
    scanBtn.addEventListener("click", requestBrokerScan);
  }

  var searchBox = byId("broker-search");
  if (searchBox) searchBox.addEventListener("input", renderCatalog);

  var hideAdded = byId("broker-hide-added");
  if (hideAdded) hideAdded.addEventListener("change", renderCatalog);

  var brokerRows = byId("broker-rows");
  if (brokerRows) {
    brokerRows.addEventListener("click", function (event) {
      var btn = event.target.closest("button[data-broker-add]");
      if (!btn) return;
      var symbol = btn.getAttribute("data-broker-add");
      btn.disabled = true;
      show("broker-message", "Adding " + symbol + "…", "info");
      post("/api/assets/add-broker", { symbol: symbol }).then(function (r) {
        show("broker-message", r.data.message || "", r.ok ? "success" : "danger");
        if (!r.ok) { btn.disabled = false; return; }
        // Marked locally rather than re-fetching the catalogue: the server has
        // already confirmed the write, so the row is known to be in the registry.
        catalog.forEach(function (s) { if (s.name === symbol) s.in_registry = true; });
        renderCatalog();
        renderRegistry(r.data.assets);
      });
    });
  }

  var registryRows = byId("registry-rows");
  if (registryRows) {
    registryRows.addEventListener("click", function (event) {
      var btn = event.target.closest("button[data-asset]");
      if (!btn) return;
      var name = btn.getAttribute("data-asset");

      if (btn.hasAttribute("data-registry-toggle")) {
        var enable = btn.getAttribute("data-enabled") !== "true";
        show("registry-message", (enable ? "Enabling " : "Disabling ") + name + "…",
          "info");
        post("/api/assets/toggle", { name: name, enabled: enable })
          .then(function (r) {
            show("registry-message", r.data.message || "", r.ok ? "success" : "danger");
            if (r.ok) renderRegistry(r.data.assets);
          });
        return;
      }

      if (btn.hasAttribute("data-registry-remove")) {
        if (!window.confirm("Remove " + name + " from the registry?")) return;
        post("/api/assets/remove", { name: name }).then(function (r) {
          show("registry-message", r.data.message || "", r.ok ? "success" : "danger");
          if (r.ok) renderRegistry(r.data.assets);
        });
      }
    });
  }

  /* Enabling the whole list multiplies alert volume by the number of assets --
   * the same warning assets.json carries -- so it asks first. */
  function bulkToggle(enabled) {
    var question = enabled
      ? "Enable every asset in the registry?\n\nThe scanner will watch all of " +
        "them at once, and every signal will raise an alert."
      : "Disable every asset in the registry?\n\nThe scanner will have nothing " +
        "to watch until you enable one again.";
    if (!window.confirm(question)) return;
    show("registry-message", "Applying…", "info");
    post("/api/assets/set-all", { enabled: enabled }).then(function (r) {
      show("registry-message", r.data.message || "", r.ok ? "success" : "danger");
      if (r.ok) renderRegistry(r.data.assets);
    });
  }

  var enableAll = byId("registry-enable-all");
  if (enableAll) enableAll.addEventListener("click", function () { bulkToggle(true); });

  var disableAll = byId("registry-disable-all");
  if (disableAll) disableAll.addEventListener("click", function () { bulkToggle(false); });

  // Server-rendered rows are already in the page; this renders the catalogue
  // the server embedded, so a refresh after a scan shows the list immediately.
  if (byId("broker-rows")) {
    catalog = readEmbeddedCatalog();
    renderCatalog();
    /* An empty table is indistinguishable from a broken MT5 link, so the page
     * asks for the list itself when it has nothing cached. Done here rather than
     * on the server because a scan owns the terminal: it must go through the
     * same MT5-serialising job queue as everything else (see runner.py) and can
     * legitimately be refused while a live session runs. */
    if (!catalog.length) requestBrokerScan();
  }

  tick();
  setInterval(tick, POLL_MS);
})();

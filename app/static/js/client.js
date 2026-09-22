/* Client platform live updates.
 *
 * One small poller for the whole client area. It has three jobs, in order of
 * how much they matter:
 *
 *   1. Tick the New York clock in the topbar.
 *   2. Refresh the chrome that is on every page — the session and scanner
 *      pills, which are the reader's answer to "is the engine awake".
 *   3. Refresh what the *current* page shows, but only what the page declares
 *      in window.clientPoll. A page that declares nothing gets 1 and 2.
 *
 * The design rule throughout: this file renders values the server sent, and it
 * never decides what a value means. State labels, tone classes and condition
 * text all arrive in the payload from app/client.py, which is the same code
 * that rendered the first paint — so a polled update cannot disagree with a
 * refreshed one. Nothing is recomputed here, and nothing is invented: an absent
 * price stays an em dash.
 *
 * Polling, not SSE/WebSocket: the app is served by Waitress with a fixed thread
 * pool (see config.serve_threads), and one held connection per browser would
 * starve it. This matches the console's existing 5s loop in dashboard.js.
 */
(function () {
  "use strict";

  var POLL_MS = 5000;
  var DASH = "—"; /* em dash: the platform's "no value recorded" mark */

  var cfg = window.ictClock || {};
  /* A page that declares nothing still polls — for the status chrome only. */
  var poll = window.clientPoll || { kind: "status" };

  /* The highest signal id already on the page, so "a setup confirmed while you
   * were reading" is something we can detect without re-rendering the grid. */
  var renderedMaxId = (function () {
    var ids = document.querySelectorAll("[data-signal-id]");
    var max = 0;
    for (var i = 0; i < ids.length; i++) {
      var n = parseInt(ids[i].getAttribute("data-signal-id"), 10);
      if (!isNaN(n) && n > max) { max = n; }
    }
    return max;
  })();

  /* ------------------------------------------------------------------ */
  /* New York clock                                                      */
  /* ------------------------------------------------------------------ */
  /* Resolved through the browser's own tz database with the same IANA zone
   * name the server used (window.ictClock.nyZone), so DST is handled on both
   * sides by the same rules and the header can never disagree with the
   * timestamps in the tables. No UTC-4 or UTC-5 literal appears here.
   *
   * If the engine has no timezone data (very old browser), we fall back to the
   * offset the server reported — correct today, and only wrong within a day of
   * a DST change, which is the best a zone-less engine can do. */
  var zoneOk = (function () {
    if (!cfg.nyZone) { return false; }
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: cfg.nyZone });
      return true;
    } catch (e) {
      return false;
    }
  })();

  function nyParts() {
    if (zoneOk) {
      var parts = new Intl.DateTimeFormat("en-US", {
        timeZone: cfg.nyZone, hour12: false, hour: "2-digit",
        minute: "2-digit", second: "2-digit", timeZoneName: "short"
      }).formatToParts(new Date());
      var out = { time: "", zone: "" };
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === "hour" || parts[i].type === "minute" ||
            parts[i].type === "second") {
          if (out.time) { out.time += ":"; }
          out.time += parts[i].value;
        } else if (parts[i].type === "timeZoneName") {
          out.zone = parts[i].value;
        }
      }
      /* "24" appears at midnight under hour12:false in some engines. */
      if (out.time.indexOf("24:") === 0) { out.time = "00:" + out.time.slice(3); }
      return out;
    }

    /* Offset path: shifting the epoch by the offset makes the UTC getters read
     * New York wall-clock fields. */
    var d = new Date(Date.now() + (cfg.nyOffsetHours || 0) * 3600000);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return {
      time: pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()),
      zone: ""
    };
  }

  function tickClock() {
    var el = document.getElementById("ny-clock");
    if (!el) { return; }
    var now = nyParts();
    el.textContent = now.time;
    var zone = document.getElementById("ny-zone");
    if (zone && now.zone) { zone.textContent = now.zone; }
  }

  /* ------------------------------------------------------------------ */
  /* Shared chrome                                                       */
  /* ------------------------------------------------------------------ */
  function setText(id, value) {
    var el = document.getElementById(id);
    if (el && value !== undefined && value !== null) { el.textContent = value; }
  }

  /* Replace a pill's whole contents. Only ever called with a string built from
   * a payload field, never with markup from anywhere else. */
  function setPill(id, tone, text) {
    var el = document.getElementById(id);
    if (!el) { return; }
    el.className = "pill " + tone;
    el.innerHTML = "";
    var dot = document.createElement("span");
    dot.className = "dot";
    el.appendChild(dot);
    el.appendChild(document.createTextNode(text));
  }

  /* The scanner/session pill is two facts, and the wording distinguishes them:
   * the live thread runs around the clock, so "idle" and "stopped" are
   * different states and must not be collapsed into one another.
   *
   * `engine_alive` is what "the bot is up" actually means, and it is true when
   * a scanner is running in *any* process — the engine publishes a heartbeat
   * the server reads. A running web server is not part of this decision. */
  function applyStatus(status) {
    if (!status) { return; }
    if (status.scanner_active) {
      setPill("nav-scanner", "tone-success", "Scanner on · " + status.session_label);
    } else if (status.scanner_running) {
      setPill("nav-scanner", "tone-secondary", "Scanner idle · " + status.session_label);
    } else {
      setPill("nav-scanner", "tone-secondary", "Scanner stopped");
    }

    var session = document.getElementById("ov-session");
    if (session) {
      session.className = "state-chip " + (status.scanner_active ? "is-confirmed"
                                        : status.scanner_running ? "is-working" : "is-idle");
      session.textContent = status.session_label;
    }
    setText("ov-scanner", status.scanner_running
      ? "Scanner " + (status.scanner_state || "running") : "Scanner stopped");

    /* The engine status card on the market page. Rewritten from the same
     * payload, so a polled card cannot disagree with a refreshed one: the words
     * arrive already decided by app/client.py, and this only puts them in the
     * DOM. "Web app" is deliberately not touched — it is true by construction
     * for as long as this script is running to update anything. */
    if (document.getElementById("mk-engine")) {
      setText("mk-engine", status.backend_running ? "RUNNING" : "OFFLINE");
      setText("mk-mt5", status.mt5_connected ? "CONNECTED"
                    : (status.mt5_connected === false ? "DISCONNECTED" : "UNKNOWN"));
      setText("mk-data", status.market_data_live ? "LIVE"
                    : (status.backend_running ? "WAITING" : "NONE"));
      setText("mk-scanner", status.scanner_status);
      setText("mk-setups", status.setup_scanner_status);
      setText("mk-session", status.session_open ? status.session_label : DASH);
      setText("mk-session-status", status.session_status);
      setText("mk-assets", status.assets_status);
      setText("mk-last-scan", status.last_scan_ny || DASH);
      setText("mk-heartbeat", status.engine_heartbeat_ny || DASH);
    }
  }

  /* A confirmed setup that is not on the page yet. We surface it rather than
   * injecting a card, because the card markup (conditions, meter, wording)
   * lives in one server-side template and a second implementation here is
   * exactly how a polled card starts disagreeing with a refreshed one. */
  function noticeNewSetups(containerId, maxId) {
    var box = document.getElementById(containerId);
    if (!box || !maxId || maxId <= renderedMaxId) { return; }
    var n = maxId - renderedMaxId;
    renderedMaxId = maxId;
    box.innerHTML = "";
    var note = document.createElement("div");
    note.className = "notice mb-3";
    var text = document.createElement("span");
    var strong = document.createElement("strong");
    strong.textContent = n === 1 ? "A new setup was confirmed."
                                : n + " new setups were confirmed.";
    text.appendChild(strong);
    text.appendChild(document.createTextNode(
      " Reload to see " + (n === 1 ? "it" : "them") + " with the full conditions."));
    note.appendChild(text);
    box.appendChild(note);
  }

  /* ------------------------------------------------------------------ */
  /* Page-specific rendering                                             */
  /* ------------------------------------------------------------------ */
  function stateChip(info) {
    var span = document.createElement("span");
    span.className = "state-chip " + (info.tone || "is-idle");
    if (info.tone === "is-working" || info.tone === "is-ready") {
      var dot = document.createElement("span");
      dot.className = "dot";
      span.appendChild(dot);
    }
    span.title = info.note || "";
    span.appendChild(document.createTextNode(info.label || ""));
    return span;
  }

  function replaceChip(cell, info) {
    if (!cell || !info) { return; }
    cell.innerHTML = "";
    cell.appendChild(stateChip(info));
  }

  function feedItem(entry) {
    var li = document.createElement("li");
    li.className = "feed-item";
    var time = document.createElement("span");
    time.className = "feed-time";
    time.textContent = entry.date_ny + " " + entry.time_short;
    var source = document.createElement("span");
    source.className = "feed-source";
    source.textContent = entry.source_label || "";
    var text = document.createElement("span");
    text.className = "feed-text";
    text.textContent = entry.message || "";
    li.appendChild(time);
    li.appendChild(source);
    li.appendChild(text);
    return li;
  }

  /* Market and analysis pages: rewrite each row's quote and state chips in
   * place, keyed on data-asset. Rows are never added or removed — the registry
   * is what decides which assets exist, and that only changes from the console.
   *
   * Every figure here comes from a `*_label` field the server already formatted
   * at the asset's own precision, so the browser never decides how many digits
   * a price has. An absent one arrives as "" and stays an em dash. */
  function applyRows(rows) {
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var tr = document.querySelector('tr[data-asset="' + r.name + '"]');
      if (!tr) { continue; }

      var cells = {
        bid: r.bid_label,
        ask: r.ask_label,
        spread: r.spread_label,
        price: r.price_label
      };
      for (var role in cells) {
        if (!Object.prototype.hasOwnProperty.call(cells, role)) { continue; }
        var cell = tr.querySelector('[data-role="' + role + '"]');
        if (cell) { cell.textContent = cells[role] || DASH; }
      }

      var timeCell = tr.querySelector('[data-role="price-time"]');
      if (timeCell) { timeCell.textContent = r.quote_time_ny || DASH; }

      replaceChip(tr.querySelector('[data-role="buy"]'), r.buy);
      replaceChip(tr.querySelector('[data-role="sell"]'), r.sell);
    }
  }

  function applyOverview(payload) {
    setText("ov-count-active", payload.signals ? payload.signals.length : undefined);
    if (payload.counts) {
      setText("ov-count-total", payload.counts.setups_total);
      setText("ov-count-approved", payload.counts.setups_approved);
      setText("ov-count-assets", payload.counts.assets_monitored);
    }
    noticeNewSetups("ov-new-notice", payload.last_signal_id);
  }

  /* ------------------------------------------------------------------ */
  /* Poll loop                                                           */
  /* ------------------------------------------------------------------ */
  function fetchJSON(url) {
    return fetch(url, {
      credentials: "same-origin",
      headers: { "Accept": "application/json" }
    }).then(function (resp) {
      /* A session that expired mid-page returns the login page. Reloading puts
         the reader back on the sign-in form rather than showing stale figures
         as though they were live. */
      if (resp.status === 401 || resp.status === 403) {
        window.location.reload();
        return null;
      }
      return resp.ok ? resp.json() : null;
    }).catch(function () {
      /* A dropped poll is not an error worth surfacing: the next tick retries,
       * and the server-rendered values are already correct as of page load. */
      return null;
    });
  }

  var pollers = {
    /* Pages that show nothing live still get the chrome refreshed, so the
     * session pill on a history page cannot go stale while the reader is
     * scrolled down a table. `/api/client/feed` is the cheapest status source
     * of the four and the response is used only for its `status` block. */
    status: { url: "/api/client/feed", apply: function (d) {
      applyStatus(d.status);
    } },
    overview: { url: "/api/client/overview", apply: function (d) {
      applyStatus(d.status);
      applyOverview(d);
    } },
    market: { url: "/api/client/market", apply: function (d) {
      applyStatus(d.status);
      applyRows(d.rows || []);
    } },
    setups: { url: "/api/client/setups", apply: function (d) {
      applyStatus(d.status);
      noticeNewSetups("setups-new-notice", d.last_signal_id);
    } },
    analysis: { url: "/api/client/feed", apply: function (d) {
      applyStatus(d.status);
      var box = document.getElementById("analysis-feed");
      if (!box || !d.feed || !d.feed.length) { return; }
      /* Newest first: rebuild only when the log actually moved, so steady state
         costs nothing and does not fight a reader who is scrolling. */
      if (box.getAttribute("data-top") === String(d.feed[0].id)) { return; }
      box.setAttribute("data-top", String(d.feed[0].id));
      var list = document.createElement("ul");
      list.className = "feed";
      for (var i = 0; i < d.feed.length; i++) {
        list.appendChild(feedItem(d.feed[i]));
      }
      box.innerHTML = "";
      box.appendChild(list);
      setText("analysis-count", d.feed.length + " recent entries");
    } }
  };

  function tick() {
    var p = pollers[poll.kind];
    if (!p) { return; }
    fetchJSON(p.url).then(function (data) {
      if (data) { p.apply(data); }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Start                                                               */
  /* ------------------------------------------------------------------ */
  tickClock();
  setInterval(tickClock, 1000);

  if (poll.kind) {
    setInterval(tick, POLL_MS);
    /* Also poll when the tab becomes visible again: a backgrounded tab has its
     * timers throttled, so without this the first thing a returning reader sees
     * is whatever was on screen when they left. */
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { tick(); }
    });
  }
})();

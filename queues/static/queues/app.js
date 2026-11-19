/* giliran: progressive enhancement. Pages work without JS; this adds live updates.
 *
 * Live data comes from a Server-Sent Events stream (data-stream-url) when the browser
 * has EventSource; the server closes each stream after about a minute with a "bye"
 * event and we reconnect at once. If the stream is missing or keeps failing we fall
 * back to polling the JSON endpoint (data-poll-url), which sends the same payload.
 */
(function () {
  "use strict";

  var TERMINAL = ["done", "no_show", "cancelled"];
  var MAX_STREAM_FAILURES = 3;

  document.addEventListener("change", function (e) {
    if (e.target.matches("select[data-autosubmit]")) e.target.form.submit();
  });

  function poll(root, handle) {
    var url = root.dataset.pollUrl;
    var ms = parseInt(root.dataset.pollMs, 10) || 5000;
    root.dataset.transport = "poll";
    function tick() {
      fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(function (data) { root.dataset.live = "ok"; handle(data); })
        .catch(function () { root.dataset.live = "offline"; })
        .finally(function () { if (!root.dataset.stopped) setTimeout(tick, ms); });
    }
    setTimeout(tick, ms);
  }

  /* Keep `root` up to date with `render(data)`; stops once root.dataset.stopped is set. */
  function live(root, render) {
    function handle(data) {
      if (root.dataset.stopped) return;
      root.dataset.live = "ok";
      render(data);
    }
    var url = root.dataset.streamUrl;
    if (!url || typeof window.EventSource !== "function") { poll(root, handle); return; }
    var failures = 0;
    function connect() {
      var es = new EventSource(url);
      root.dataset.transport = "sse";
      es.onopen = function () { failures = 0; };
      es.onmessage = function (e) {
        var data;
        try { data = JSON.parse(e.data); } catch (err) { return; }
        failures = 0;
        handle(data);
        if (root.dataset.stopped) es.close();
      };
      es.addEventListener("bye", function () { es.close(); if (!root.dataset.stopped) connect(); });
      es.addEventListener("end", function () { es.close(); root.dataset.stopped = "1"; });
      es.onerror = function () {
        if (root.dataset.stopped) { es.close(); return; }
        failures += 1;
        root.dataset.live = "offline";
        // CLOSED: the server refused the stream (e.g. 404 when streams are off). Otherwise
        // the browser retries by itself; give up after a few failures in a row.
        if (es.readyState === EventSource.CLOSED || failures >= MAX_STREAM_FAILURES) {
          es.close();
          poll(root, handle);
        }
      };
    }
    connect();
  }

  function setText(root, field, value) {
    var node = root.querySelector('[data-field="' + field + '"]');
    if (node && node.textContent !== value) node.textContent = value;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  /* --- "Almost your turn" alerts: chime (WebAudio), vibration, notification, aria-live. */

  function Alerts(ticket) {
    this.ticket = ticket;
    this.audio = null;
    this.enabled = false;
    this.fired = {};
    this.announcer = ticket.querySelector("[data-announcer]");
    var toggle = ticket.querySelector("#alerts-toggle");
    var self = this;
    if (toggle) {
      toggle.addEventListener("change", function () { self.setEnabled(toggle.checked); });
    }
  }

  Alerts.prototype.setEnabled = function (on) {
    this.enabled = on;
    this.ticket.dataset.sound = on ? "on" : "off";
    if (!on) return;
    // Browsers only allow audio after a user gesture: this change event is one.
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx && !this.audio) {
      try { this.audio = new Ctx(); } catch (err) { this.audio = null; }
    }
    if (this.audio && this.audio.resume) this.audio.resume();
    this.chime(1);
    if ("Notification" in window && Notification.permission === "default") {
      try {
        var asked = Notification.requestPermission();
        if (asked && asked.catch) asked.catch(function () {});
      } catch (err) { /* old Safari: callback API only */ }
    }
  };

  Alerts.prototype.chime = function (repeats) {
    var ctx = this.audio;
    if (!ctx) return;
    var t = ctx.currentTime + 0.05;
    var notes = [];
    for (var i = 0; i < repeats; i++) notes.push(659, 880);
    notes.forEach(function (freq, n) {
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      var at = t + n * 0.22;
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, at);
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.3, at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.2);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(at);
      osc.stop(at + 0.21);
    });
  };

  /* Fire once per key (e.g. "almost", "called:0", "called:1" after a recall). */
  Alerts.prototype.fire = function (key, message, title) {
    if (this.fired[key]) return;
    this.fired[key] = true;
    var t = this.ticket;
    t.dataset.alertCount = String((parseInt(t.dataset.alertCount, 10) || 0) + 1);
    t.dataset.lastAlert = key;
    var announcer = this.announcer;
    if (announcer) {
      // Clear first so a repeated text (e.g. "called again") is announced again.
      announcer.textContent = "";
      setTimeout(function () { announcer.textContent = message; }, 50);
    }
    if (navigator.vibrate) {
      try { navigator.vibrate(key === "almost" ? [200] : [300, 150, 300]); } catch (err) { /* ignore */ }
    }
    if (!this.enabled) return;
    this.chime(key === "almost" ? 1 : 2);
    if ("Notification" in window && Notification.permission === "granted" &&
        document.visibilityState !== "visible") {
      try { new Notification(title, { body: message, tag: "giliran-" + key }); } catch (err) {
        /* Android Chrome needs a service worker for notifications; the chime still plays. */
      }
    }
  };

  var ticket = document.getElementById("ticket");
  if (ticket) {
    var almostAt = parseInt(ticket.dataset.almostAhead, 10);
    if (isNaN(almostAt)) almostAt = 2;
    var alerts = new Alerts(ticket);
    // Start from what the server rendered so a reload does not alert again.
    if (ticket.dataset.isAlmost === "1") alerts.fired.almost = true;
    if (ticket.dataset.status === "called") alerts.fired["called:" + ticket.dataset.recallCount] = true;
    if (TERMINAL.indexOf(ticket.dataset.status) !== -1) ticket.dataset.stopped = "1";
    var toggleBox = ticket.querySelector("[data-alerts-toggle]");
    if (toggleBox) toggleBox.hidden = TERMINAL.indexOf(ticket.dataset.status) !== -1;

    var render = function (d) {
      if (d.code !== ticket.dataset.code) {
        // Staff moved the ticket to another service: new number, so alert again for it.
        ticket.dataset.code = d.code;
        alerts.fired = {};
        document.title = d.code + " · giliran";
      }
      setText(ticket, "code", d.code);
      setText(ticket, "service", d.service);
      ticket.className = ticket.className.replace(/status-\S+/, "status-" + d.status);
      ticket.dataset.status = d.status;
      setText(ticket, "status_label", d.status_label);
      setText(ticket, "counter", d.counter || "");
      setText(ticket, "ahead", d.ahead === null ? ticket.dataset.tNone : String(d.ahead));
      var eta = ticket.dataset.tNone;
      if (d.eta_minutes === 0) eta = ticket.dataset.tNext;
      else if (d.eta_minutes !== null) eta = ticket.dataset.tMinutes.replace("{n}", d.eta_minutes);
      setText(ticket, "eta", eta);
      ticket.querySelectorAll("[data-show-when]").forEach(function (n) {
        n.hidden = n.dataset.showWhen.split(" ").indexOf(d.status) === -1;
      });
      var almost = d.status === "waiting" && d.ahead !== null && d.ahead <= almostAt;
      ticket.querySelector("[data-almost-banner]").hidden = !almost;
      if (almost) {
        alerts.fire("almost", ticket.dataset.tAlmost.replace("{n}", d.ahead), "🔔 " + d.code + " · giliran");
      } else if (d.status === "called") {
        document.title = "🔔 " + d.code + " · giliran";
        alerts.fire(
          "called:" + d.recall_count,
          ticket.dataset.tCalled.replace("{code}", d.code).replace("{counter}", d.counter || ""),
          "🔔 " + d.code + " · giliran"
        );
      }
      if (TERMINAL.indexOf(d.status) !== -1) ticket.dataset.stopped = "1";
    };
    if (!ticket.dataset.stopped) live(ticket, render);
  }

  var board = document.getElementById("board");
  if (board) {
    live(board, function (d) {
      var serving = board.querySelector('[data-list="serving"]');
      var next = board.querySelector('[data-list="next"]');
      var services = board.querySelector('[data-list="services"]');
      serving.replaceChildren();
      if (!d.serving.length) serving.appendChild(el("li", "empty", "—"));
      d.serving.forEach(function (s) {
        var li = el("li");
        li.appendChild(el("span", "code", s.code));
        var arrow = el("span", "arrow", "→");
        arrow.setAttribute("aria-hidden", "true");
        li.appendChild(arrow);
        li.appendChild(el("span", "counter", s.counter));
        serving.appendChild(li);
      });
      var paused = board.querySelector('[data-list="paused"]');
      if (paused) {
        paused.hidden = !(d.paused && d.paused.length);
        paused.textContent = paused.hidden ? "" : (paused.dataset.label || "Paused") + ": " + d.paused.join(", ");
      }
      next.replaceChildren();
      if (!d.next.length) next.appendChild(el("li", "empty", "—"));
      d.next.forEach(function (code) { next.appendChild(el("li", "", code)); });
      services.replaceChildren();
      d.services.forEach(function (s) {
        var li = el("li");
        li.appendChild(el("span", "", s.prefix + " · " + s.name));
        li.appendChild(el("strong", "", String(s.waiting)));
        services.appendChild(li);
      });
    });
  }
})();

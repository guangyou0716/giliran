/* giliran: progressive enhancement. Pages work without JS; this adds live updates. */
(function () {
  "use strict";

  document.addEventListener("change", function (e) {
    if (e.target.matches("select[data-autosubmit]")) e.target.form.submit();
  });

  function poll(el, render) {
    var url = el.dataset.pollUrl;
    var ms = parseInt(el.dataset.pollMs, 10) || 5000;
    function tick() {
      fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(function (data) { el.dataset.live = "ok"; render(data); })
        .catch(function () { el.dataset.live = "offline"; })
        .finally(function () { if (!el.dataset.stopped) setTimeout(tick, ms); });
    }
    setTimeout(tick, ms);
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

  var ticket = document.getElementById("ticket");
  if (ticket) {
    var lastStatus = null;
    poll(ticket, function (d) {
      ticket.className = ticket.className.replace(/status-\S+/, "status-" + d.status);
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
      if (d.status === "called" && lastStatus !== null && lastStatus !== "called") {
        if (navigator.vibrate) navigator.vibrate([300, 150, 300]);
        document.title = "🔔 " + d.code + " · giliran";
      }
      lastStatus = d.status;
      if (["done", "no_show", "cancelled"].indexOf(d.status) !== -1) ticket.dataset.stopped = "1";
    });
  }

  var board = document.getElementById("board");
  if (board) {
    poll(board, function (d) {
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

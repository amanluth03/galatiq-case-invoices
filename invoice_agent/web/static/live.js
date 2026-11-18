/* Review screen behaviour (no libraries, no external assets; text is only ever set with textContent).
   - Invoice page: a running story (data-live-run) polls /invoices/<id>/status every second and redraws its 7 steps.
     When the run finishes, the pill is updated and the page reloads once so the finished page (decision panel,
     full details) is server-rendered.
   - Dashboard (/dashboard): choosing or dropping a file only selects it ("<name> ready"); "Process this invoice" starts it. While
     invoices run, the dashboard is refreshed from the server every 2 seconds. Each dashboard row opens its invoice.
   - Original document: hovering, focusing or tapping a highlight shows why the reader was unsure, in a tooltip.
   - Review brief: "Show in invoice" scrolls to that spot (opening anything collapsed around it) and lights it up. */
(function () {
  "use strict";

  var POLL_MS = 1000;
  var BOARD_MS = 2000;
  var STATE_WORDS = { pending: "not started", running: "in progress", done: "done",
                      warn: "done, with warnings", failed: "failed", skipped: "skipped" };

  /* Build one step exactly as the server template (_steps.html) does. */
  function stepItem(s) {
    var item = document.createElement("div");
    item.className = "st st-" + s.state;
    item.setAttribute("role", "listitem");
    var mark = document.createElement("span");
    mark.className = "st-mark";
    mark.setAttribute("aria-hidden", "true");
    var body = document.createElement("div");
    body.className = "st-body";
    var title = document.createElement("p");
    title.className = "st-title";
    title.textContent = s.number + ". " + s.title;
    var sr = document.createElement("span");
    sr.className = "sr-only";
    sr.textContent = " (" + (STATE_WORDS[s.state] || s.state) + ")";
    title.appendChild(sr);
    body.appendChild(title);
    if (s.lines.length) {
      var lines = document.createElement("div");
      lines.className = "st-lines";
      s.lines.forEach(function (l) {
        var p = document.createElement("p");
        p.className = "ln ln-" + l.kind;
        p.textContent = l.text;
        lines.appendChild(p);
      });
      body.appendChild(lines);
    }
    item.appendChild(mark);
    item.appendChild(body);
    return item;
  }

  /* Set a status pill's text and colour (pill-paid, pill-rejected, ...), so it never keeps saying "Running". */
  function setPill(pill, status, label) {
    if (!pill) return;
    pill.textContent = label;
    pill.className = "pill pill-" + String(status).toLowerCase();
  }

  /* Poll one running story until it finishes; stop for good if the page is left or the server can't be reached. */
  function watch(root) {
    var runId = root.getAttribute("data-live-run");
    var list = root.querySelector("[data-live-steps]");
    var pill = root.querySelector("[data-live-pill]");
    var last = null;
    var timer = null;
    var stopped = false;

    function stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
    }
    // Leaving the page (or it going into the back/forward cache) ends polling, so nothing reloads a page you left.
    window.addEventListener("pagehide", stop);

    function tick() {
      if (stopped) return;
      fetch("/invoices/" + encodeURIComponent(runId) + "/status", { cache: "no-store" })
        .then(function (resp) {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.json();
        })
        .then(function (s) {
          if (stopped) return;
          if (s.finished) {
            stop();
            // Show the outcome at once, then load the full finished page.
            setPill(pill, s.status, s.label);
            window.location.reload();
            return;
          }
          var json = JSON.stringify(s.steps);
          // Only redraw when something changed, so the live region announces each new step once.
          if (json !== last) {
            last = json;
            var fresh = document.createDocumentFragment();
            s.steps.forEach(function (step) { fresh.appendChild(stepItem(step)); });
            list.replaceChildren(fresh);
            setPill(pill, s.status, s.label);
          }
          timer = setTimeout(tick, POLL_MS);
        })
        .catch(function () {
          stop();
          // Say so instead of spinning forever (and drop the "Running" look).
          setPill(pill, "error", "Lost touch with the server: refresh to check");
        });
    }
    timer = setTimeout(tick, POLL_MS);
  }

  var live = document.querySelector("[data-live-run]");
  if (live) watch(live);

  /* Dashboard: while invoices run, swap in the server's fresh dashboard (parsed, never run as script). */
  var boardTimer = null;
  function refreshBoard() {
    var board = document.querySelector("[data-dashboard]");
    if (!board || !board.hasAttribute("data-live")) return;
    boardTimer = setTimeout(function () {
      fetch("/dashboard", { cache: "no-store" })
        .then(function (resp) {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.text();
        })
        .then(function (html) {
          var fresh = new DOMParser().parseFromString(html, "text/html").querySelector("[data-dashboard]");
          var current = document.querySelector("[data-dashboard]");
          if (fresh && current) current.replaceWith(document.importNode(fresh, true));
          refreshBoard();
        })
        .catch(function () { /* Server unreachable: keep the last dashboard; a refresh tries again. */ });
    }, BOARD_MS);
  }
  refreshBoard();
  window.addEventListener("pagehide", function () { if (boardTimer) clearTimeout(boardTimer); });

  // Coming back via the back button can restore a page from cache with polling stopped: reload it fresh.
  window.addEventListener("pageshow", function (e) {
    if (e.persisted && (live || document.querySelector("[data-dashboard][data-live]"))) window.location.reload();
  });

  /* A click anywhere on a dashboard row opens that invoice (the file name inside is the keyboard link). */
  document.addEventListener("click", function (e) {
    var row = e.target.closest && e.target.closest("tr[data-href]");
    if (!row || e.target.closest("a, button")) return;
    // Selecting text in a row isn't a click to open it.
    if (window.getSelection && String(window.getSelection())) return;
    window.location.href = row.getAttribute("data-href");
  });

  /* Upload form: a chosen or dropped file is only selected (never sent) and its name shown. */
  var zone = document.querySelector("[data-dropzone]");
  var fileInput = zone && zone.querySelector("input[type=file]");
  var chosen = document.querySelector("[data-chosen]");
  function showChosen() {
    if (!chosen) return;
    chosen.textContent = fileInput.files && fileInput.files.length ? fileInput.files[0].name + " ready" : "";
  }
  if (zone && fileInput) {
    fileInput.addEventListener("change", showChosen);
    // Handlers sit on the form only (never the whole page), so dragging can't block clicks elsewhere.
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (e) {
        e.preventDefault();
        zone.classList.add("dz-over");
      });
    });
    ["dragleave", "dragend", "drop"].forEach(function (type) {
      zone.addEventListener(type, function () { zone.classList.remove("dz-over"); });
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      if (!e.dataTransfer || !e.dataTransfer.files.length) return;
      fileInput.files = e.dataTransfer.files;
      showChosen();
    });
    // Pressing Process with nothing chosen: say so and open the chooser, instead of the browser pointing at the
    // hidden input.
    fileInput.addEventListener("invalid", function (e) {
      e.preventDefault();
      if (chosen) chosen.textContent = "Choose a file first.";
      fileInput.click();
    });
    // A file kept by the browser after going back still shows as chosen.
    showChosen();
  }

  /* Tooltips on the original document's highlights: one shared box, fixed to the viewport so the document's
     scroll pane can't clip it; above the highlight, or below when there is no room above. */
  var tip = null;
  var tipFor = null;
  function hideTip() {
    if (tip) tip.hidden = true;
    tipFor = null;
  }
  function showTip(mark) {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tip";
      tip.id = "doc-tip";
      tip.setAttribute("role", "tooltip");
      document.body.appendChild(tip);
    }
    tip.textContent = mark.getAttribute("data-reason");
    tip.hidden = false;
    mark.setAttribute("aria-describedby", "doc-tip");
    tipFor = mark;
    var r = mark.getBoundingClientRect();
    var w = tip.offsetWidth, h = tip.offsetHeight, gap = 8;
    var left = Math.max(12, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 12));
    var top = r.top - h - gap;
    if (top < 8) top = r.bottom + gap;  // near the top of the window: flip below
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function markOf(target) {
    return target && target.closest ? target.closest(".doc mark[data-reason]") : null;
  }
  document.addEventListener("mouseover", function (e) {
    var m = markOf(e.target);
    if (m) showTip(m); else if (tipFor && !tipFor.contains(document.activeElement)) hideTip();
  });
  document.addEventListener("focusin", function (e) {
    var m = markOf(e.target);
    if (m) showTip(m); else hideTip();
  });
  // A tap on a phone shows it; tapping anywhere else hides it.
  document.addEventListener("click", function (e) {
    var m = markOf(e.target);
    if (m) showTip(m); else hideTip();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hideTip(); });

  /* Review brief links ("Show in invoice", "Read the full analyst and VP exchange"): find the spot (a document mark
     by its data-anchor key, else the element the link names), open any collapsed <details> around or at it, scroll it
     into view, focus it and light it up for a few seconds. Without JavaScript the plain link still goes nearby. */
  var lit = null;
  var litTimer = null;
  function jumpTarget(link) {
    var key = link.getAttribute("data-anchor-key");
    if (key) {
      var marks = document.querySelectorAll(".doc mark[data-anchor]");
      for (var i = 0; i < marks.length; i++) {
        if ((" " + marks[i].getAttribute("data-anchor") + " ").indexOf(" " + key + " ") >= 0) return marks[i];
      }
    }
    var href = link.getAttribute("href") || "";
    return href.charAt(0) === "#" ? document.getElementById(href.slice(1)) : null;
  }
  document.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("a[data-jump]");
    if (!link) return;
    var target = jumpTarget(link);
    if (!target) return;
    e.preventDefault();
    for (var d = target; d; d = d.parentElement) {
      if (d.tagName === "DETAILS") d.open = true;
    }
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    var smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ block: "center", behavior: smooth ? "smooth" : "auto" });
    target.focus({ preventScroll: true });
    if (lit) lit.classList.remove("jump-hit");
    if (litTimer) clearTimeout(litTimer);
    lit = target;
    target.classList.add("jump-hit");
    litTimer = setTimeout(function () { target.classList.remove("jump-hit"); }, 4000);
  });
  // Scrolling the page or the document pane moves the highlight: keep the box with it.
  document.addEventListener("scroll", function () { if (tipFor) showTip(tipFor); }, true);
})();

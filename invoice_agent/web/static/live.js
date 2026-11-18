/* Live story for the review screen (no libraries, no external assets).
   - A story with data-live-run polls /invoices/<id>/status every second and redraws its 7 steps from the JSON
     (textContent only, never innerHTML, so nothing from an invoice can run as code). When the run finishes it
     stops polling and reloads once, so the finished page (decision panel, original document) is server-rendered.
   - The upload form gets drag-and-drop. Dropping or choosing a file only selects it (its name is shown);
     processing starts when the user presses "Process this invoice". */
(function () {
  "use strict";

  var POLL_MS = 1000;
  var STATE_WORDS = { pending: "not started", running: "in progress", done: "done",
                      warn: "done, with warnings", failed: "failed", skipped: "skipped" };
  var fileInput = document.querySelector("[data-dropzone] input[type=file]");

  /* Build one step's <li> exactly as the server template does. */
  function stepItem(s) {
    var li = document.createElement("li");
    li.className = "st st-" + s.state;
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
      var ul = document.createElement("ul");
      ul.className = "st-lines";
      s.lines.forEach(function (l) {
        var item = document.createElement("li");
        item.className = "ln ln-" + l.kind;
        item.textContent = l.text;
        ul.appendChild(item);
      });
      body.appendChild(ul);
    }
    li.appendChild(mark);
    li.appendChild(body);
    return li;
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
            // Reload once to the full page, unless the user has picked a file to upload (a reload would drop it).
            if (!(fileInput && fileInput.files && fileInput.files.length)) window.location.reload();
            return;
          }
          var json = JSON.stringify(s.steps);
          // Only redraw when something changed, so the live region announces each new step once.
          if (json !== last) {
            last = json;
            var fresh = document.createDocumentFragment();
            s.steps.forEach(function (step) { fresh.appendChild(stepItem(step)); });
            list.replaceChildren(fresh);
            if (pill) pill.textContent = s.label;
          }
          timer = setTimeout(tick, POLL_MS);
        })
        .catch(function () {
          stop();
          // Say so instead of spinning forever.
          if (pill) pill.textContent = "Lost touch with the server: refresh to check";
        });
    }
    timer = setTimeout(tick, POLL_MS);
  }

  var live = document.querySelector("[data-live-run]");
  if (live) watch(live);
  // Coming back via the back button can restore this page from cache with polling stopped: reload it fresh.
  window.addEventListener("pageshow", function (e) { if (e.persisted && live) window.location.reload(); });

  /* Drag-and-drop on the upload form: a dropped file is selected (not sent) and its name shown. */
  var zone = document.querySelector("[data-dropzone]");
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
    // A file kept by the browser after going back still shows as chosen.
    showChosen();
  }
})();

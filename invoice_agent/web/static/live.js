/* Live progress for the review screen (no libraries, no external assets).
   - Any element with data-live-run polls /invoices/<id>/status every second, updates its stage text
     (and, on the detail page, the step tracker), and reloads the page when the run leaves PROCESSING.
   - The queue's upload form gets drag-and-drop on top of its plain file input. */
(function () {
  "use strict";

  var POLL_MS = 1000;
  var fileInput = document.querySelector("[data-dropzone] input[type=file]");

  /* Reload to show the finished result, unless the user has picked a file to upload (a reload would drop it). */
  function reloadWhenIdle() {
    if (fileInput && fileInput.files && fileInput.files.length) {
      setTimeout(reloadWhenIdle, POLL_MS);
      return;
    }
    window.location.reload();
  }

  /* Mark each tracker step done / current / to come for the step now in progress. */
  function updateTracker(root, step) {
    var items = root.querySelectorAll(".step");
    for (var i = 0; i < items.length; i++) {
      var state = i < step ? "done" : i === step ? "current" : "todo";
      items[i].className = "step step-" + state;
      if (state === "current") items[i].setAttribute("aria-current", "step");
      else items[i].removeAttribute("aria-current");
      var sr = items[i].querySelector("[data-step-state]");
      if (sr) sr.textContent = state === "done" ? ", done" : state === "current" ? ", in progress" : "";
    }
  }

  /* Poll one run until it finishes or the server can't be reached. */
  function watch(root) {
    var runId = root.getAttribute("data-live-run");
    var label = root.querySelector("[data-live-label]");
    var isTracker = !!root.querySelector(".tracker");
    var lastStep = null;

    function tick() {
      fetch("/invoices/" + encodeURIComponent(runId) + "/status", { cache: "no-store" })
        .then(function (resp) {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.json();
        })
        .then(function (s) {
          if (s.status !== "PROCESSING") {
            reloadWhenIdle();
            return;
          }
          // Only touch the text when the step changes, so the live region announces each stage once.
          if (s.step !== lastStep) {
            lastStep = s.step;
            if (label) label.textContent = isTracker ? "Now: " + s.label : s.label;
            if (isTracker) updateTracker(root, s.step);
          }
          setTimeout(tick, POLL_MS);
        })
        .catch(function () {
          // Stop polling: the server is down or the run is gone. Say so instead of spinning forever.
          if (label) label.textContent = "Lost touch with the server — refresh the page to check progress.";
        });
    }
    setTimeout(tick, POLL_MS);
  }

  var live = document.querySelectorAll("[data-live-run]");
  for (var i = 0; i < live.length; i++) watch(live[i]);

  /* Drag-and-drop: dropping a file on the upload form fills the file input and submits it. */
  var zone = document.querySelector("[data-dropzone]");
  if (zone && fileInput) {
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (e) {
        e.preventDefault();
        zone.classList.add("dz-over");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      zone.addEventListener(type, function () { zone.classList.remove("dz-over"); });
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      if (!e.dataTransfer || !e.dataTransfer.files.length) return;
      fileInput.files = e.dataTransfer.files;
      zone.submit();
    });
  }
})();

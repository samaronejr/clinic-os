/* Document viewer controls for includes/components/document_viewer.html.
   Zoom steps 100/125/150 via data-zoom (CSS scales the page), the status
   line reads the page and zoom, and a region anchor outlines and focuses
   its region. Everything stays readable without this script. */
(function () {
  "use strict";

  var STEPS = [100, 125, 150];

  function update(viewer) {
    var status = viewer.querySelector("[data-docviewer-status]");
    var template = viewer.getAttribute("data-text-status") || "";
    if (status && template) {
      status.textContent = template
        .replace("__PAGE__", "1")
        .replace("__TOTAL__", viewer.getAttribute("data-pages") || "1") +
        " · " + (viewer.getAttribute("data-zoom") || "100") + "%";
    }
    var zoom = Number(viewer.getAttribute("data-zoom"));
    var out = viewer.querySelector('[data-docviewer-zoom="out"]');
    var into = viewer.querySelector('[data-docviewer-zoom="in"]');
    if (out && !viewer.hasAttribute("aria-busy")) {
      out.disabled = zoom <= STEPS[0];
    }
    if (into && !viewer.hasAttribute("aria-busy")) {
      into.disabled = zoom >= STEPS[STEPS.length - 1];
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-docviewer-zoom]");
    if (button) {
      var viewer = button.closest("[data-docviewer]");
      var index = STEPS.indexOf(Number(viewer.getAttribute("data-zoom")));
      var next = button.getAttribute("data-docviewer-zoom") === "in" ? index + 1 : index - 1;
      viewer.setAttribute("data-zoom", String(STEPS[Math.max(0, Math.min(STEPS.length - 1, next))]));
      update(viewer);
      return;
    }
    var anchor = event.target.closest("[data-docviewer-anchor]");
    if (anchor) {
      var region = document.getElementById(anchor.getAttribute("href").slice(1));
      if (region) {
        event.preventDefault();
        anchor.closest("[data-docviewer]").querySelectorAll(".docviewer-region.is-current").forEach(
          function (item) { item.classList.remove("is-current"); }
        );
        region.classList.add("is-current");
        region.focus();
      }
    }
  });
})();

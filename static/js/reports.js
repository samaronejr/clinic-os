(function () {
  "use strict";
  document.addEventListener("htmx:afterSettle", function (event) {
    var target = event.detail.target;
    if (!(target instanceof HTMLElement) || target.id !== "reports-content") return;
    var root = document.getElementById("reports-content");
    if (!(root instanceof HTMLElement)) return;
    var focus = root.querySelector("[data-focus-error], [data-report-focus]");
    if (focus instanceof HTMLElement) focus.focus();
  });
})();

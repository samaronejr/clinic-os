(function () {
  "use strict";
  var root = document.querySelector("[data-note-review]");
  if (!root) return;
  var form = document.getElementById("note-review-form");
  if (!form) return;
  var offline = root.querySelector("[data-review-offline]");
  var unconfirmed = root.querySelector("[data-review-unconfirmed]");
  var restored = false;
  var buttons = Array.from(root.querySelectorAll("[data-review-write]"));
  var disabled = buttons.map(function (button) { return button.disabled; });

  function connectionState() {
    var connected = navigator.onLine;
    if (offline) offline.hidden = connected;
    buttons.forEach(function (button, index) {
      button.disabled = disabled[index] || !connected || restored;
    });
  }

  form.addEventListener("submit", function (event) {
    if (!navigator.onLine || (restored && event.submitter &&
        event.submitter.hasAttribute("data-review-write"))) {
      event.preventDefault();
      connectionState();
    }
  });
  root.addEventListener("input", function (event) {
    if (!event.target.matches("#review-note textarea")) return;
    var status = root.querySelector("[data-review-note-state]");
    if (status) {
      status.dataset.reviewNoteState = "unsaved";
      status.textContent = status.dataset.textUnsaved;
    }
  });
  root.addEventListener("keydown", function (event) {
    if (!event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey) return;
    var selector = event.code === "KeyA" ? "[data-review-accept]" :
      event.code === "KeyR" ? "[data-review-reject]" : "";
    var suggestion = event.target.closest("[data-suggestion]");
    if (!selector || !suggestion) return;
    var button = suggestion.querySelector(selector);
    if (!button || button.disabled) return;
    event.preventDefault();
    form.requestSubmit(button);
  });
  window.addEventListener("offline", connectionState);
  window.addEventListener("online", connectionState);
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    restored = true;
    if (unconfirmed) unconfirmed.hidden = false;
    connectionState();
  });
  connectionState();
}());

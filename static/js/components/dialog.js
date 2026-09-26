/* Dialog behavior for includes/components/dialog.html.
   Opens a native <dialog> modally from [data-dialog-open="<id>"], lets the
   browser trap focus and handle Escape, and returns focus to the opener.
   Reports state only; the server decides the outcome of the form. */
(function () {
  "use strict";

  var openers = new WeakMap();

  function open(trigger) {
    var dialog = document.getElementById(trigger.getAttribute("data-dialog-open"));
    if (!dialog || typeof dialog.showModal !== "function" || dialog.open) {
      return;
    }
    openers.set(dialog, trigger);
    dialog.showModal();
    var first = dialog.querySelector("[autofocus], input, textarea, select, button");
    if (first) {
      first.focus();
    }
  }

  function restoreFocus(dialog) {
    var trigger = openers.get(dialog);
    if (trigger && document.contains(trigger)) {
      trigger.focus();
    }
    openers.delete(dialog);
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-dialog-open]");
    if (trigger) {
      event.preventDefault();
      open(trigger);
    }
  });

  document.addEventListener("close", function (event) {
    if (event.target.matches && event.target.matches("dialog[data-dialog]")) {
      restoreFocus(event.target);
    }
  }, true);

  window.ClinicDialog = { open: open };
})();

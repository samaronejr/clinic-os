/* Command palette shell for includes/components/command_palette.html.
   Ctrl+K / Cmd+K (or a [data-command-open="<id>"] button) opens the first
   palette on the page as a modal dialog and focuses its combobox; Escape
   closes it and focus returns where it was. Choosing an option follows its
   data-href. Requires combobox.js and dialog semantics from the browser. */
(function () {
  "use strict";

  var returnTo = null;

  function palette(id) {
    if (id) {
      return document.getElementById(id);
    }
    return document.querySelector("dialog[data-command-palette]:not(.command-palette--specimen)");
  }

  function open(dialog) {
    if (!dialog || dialog.open || typeof dialog.showModal !== "function") {
      return;
    }
    returnTo = document.activeElement;
    dialog.showModal();
    var input = dialog.querySelector("[role=combobox]");
    if (input) {
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    }
  }

  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
      var dialog = palette(null);
      if (dialog) {
        event.preventDefault();
        open(dialog);
      }
    }
  });

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-command-open]");
    if (trigger) {
      event.preventDefault();
      open(palette(trigger.getAttribute("data-command-open")));
    }
  });

  document.addEventListener("combobox:select", function (event) {
    var dialog = event.target.closest("dialog[data-command-palette]");
    if (dialog && event.detail && event.detail.href) {
      window.location.assign(event.detail.href);
    }
  });

  document.addEventListener("close", function (event) {
    if (event.target.matches && event.target.matches("dialog[data-command-palette]")) {
      if (returnTo && document.contains(returnTo)) {
        returnTo.focus();
      }
      returnTo = null;
    }
  }, true);
})();

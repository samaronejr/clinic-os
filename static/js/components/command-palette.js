/* Command palette shell for includes/components/command_palette.html.
   Ctrl+K / Cmd+K (or a [data-command-open="<id>"] button) opens the first
   palette on the page as a modal dialog and focuses its combobox; Escape
   closes it and focus returns where it was. Choosing an option follows its
   data-href; an option without one (a patient) carries only an opaque
   token, POSTed to the dialog's data-command-run URL through a form built
   on demand (a patient row's form is requestSubmit-ed with
   data-context-switch, so patient-context.js can hold it for unsaved work).
   Requires combobox.js and dialog semantics from the browser. */
(function () {
  "use strict";

  if (window.ClinicCommandPalette) {
    return;
  }
  window.ClinicCommandPalette = true;

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
    if (!dialog || !event.detail) {
      return;
    }
    if (event.detail.href) {
      window.location.assign(event.detail.href);
      return;
    }
    var action = dialog.getAttribute("data-command-run");
    if (!action || !event.detail.value) {
      return;
    }
    var option = Array.prototype.find.call(
      dialog.querySelectorAll("[role=option][data-kind]"),
      function (item) { return item.getAttribute("data-value") === event.detail.value; }
    );
    var switching = Boolean(option && option.getAttribute("data-kind") === "patient");
    var cookie = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    var form = document.createElement("form");
    form.method = "post";
    form.action = action;
    form.hidden = true;
    [["csrfmiddlewaretoken", cookie ? decodeURIComponent(cookie[1]) : ""], ["token", event.detail.value],
      ["next", dialog.getAttribute("data-command-next") || ""], ["unsaved", ""]]
      .forEach(function (pair) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = pair[0];
        input.value = pair[1];
        form.appendChild(input);
      });
    if (switching) {
      form.setAttribute("data-context-switch", "");
    }
    document.body.appendChild(form);
    if (switching && typeof form.requestSubmit === "function") {
      form.requestSubmit(); /* patient-context.js may hold it */
    } else {
      form.submit();
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

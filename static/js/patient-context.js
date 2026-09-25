/* Patient-context switch guard for forms marked data-context-switch (the
   command palette's patient rows, the banner's "Close patient", the palette
   page). Before such a form leaves, the page is asked whether it holds work
   bound to the current patient:
   - a cancelable "clinic:context-switch" event on document (todo 27 draft
     binding and todo 40 capture binding call preventDefault to hold it);
   - unsaved editor state (#save-state of the encounter, [data-editor]).
   If anything holds it, the #context-switch-dialog (rendered with the
   banner) asks keep or discard; without that dialog a held switch simply
   does not happen.
   Keep changes nothing; discard submits the original form with
   unsaved=discard. Nothing is stored and nothing is retargeted: the server
   runs its own switch guards again. */
(function () {
  "use strict";

  if (window.ClinicPatientContext) {
    return;
  }

  var pending = null;
  var UNSAVED = "#save-state[data-state=\"unsaved\"], #save-state[data-state=\"saving\"], "
    + "[data-editor][data-save-state=\"unsaved\"], [data-editor][data-save-state=\"saving\"], "
    + "[data-editor][data-save-state=\"error\"], [data-editor][data-save-state=\"offline\"], "
    + "[data-editor][data-save-state=\"conflict\"]";

  function held(form) {
    var event = new CustomEvent("clinic:context-switch", {
      bubbles: true,
      cancelable: true,
      detail: { form: form }
    });
    var allowed = document.dispatchEvent(event);
    return !allowed || Boolean(document.querySelector(UNSAVED));
  }

  function dialog() {
    return document.getElementById("context-switch-dialog");
  }

  function request(form) {
    if (!held(form)) {
      form.submit();
      return;
    }
    var confirm = dialog();
    if (!confirm || typeof confirm.showModal !== "function") {
      return; /* Without the dialog the switch never happens silently. */
    }
    pending = form;
    confirm.showModal();
    var keep = confirm.querySelector("[data-context-keep]");
    if (keep) {
      keep.focus();
    }
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form instanceof HTMLFormElement && form.matches("form[data-context-switch]")) {
      event.preventDefault();
      request(form);
    }
  }, true);

  document.addEventListener("click", function (event) {
    var confirm = dialog();
    if (!confirm || !confirm.contains(event.target)) {
      return;
    }
    if (event.target.closest("[data-context-keep]")) {
      confirm.close();
    } else if (event.target.closest("[data-context-discard]")) {
      var target = pending;
      pending = null;
      confirm.close();
      if (target) {
        var marker = target.querySelector("input[name=unsaved]");
        if (marker) {
          marker.value = "discard";
        }
        target.submit();
      }
    }
  });

  document.addEventListener("close", function (event) {
    if (event.target === dialog()) {
      pending = null;
    }
  }, true);

  window.ClinicPatientContext = { request: request };
})();

(function () {
  "use strict";

  function panelFor(event) {
    var element = event.detail && event.detail.elt;
    return element && element.closest ? element.closest("#tasks-panel") : null;
  }

  function release(panel) {
    panel.removeAttribute("aria-busy");
    var progress = panel.querySelector("[data-task-progress]");
    if (progress) progress.hidden = true;
    panel.querySelectorAll("[data-workflow-disabled]").forEach(function (button) {
      button.disabled = false;
      button.removeAttribute("data-workflow-disabled");
    });
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var panel = panelFor(event);
    if (!panel) return;
    panel.setAttribute("aria-busy", "true");
    panel.setAttribute("data-tasks-state", "loading");
    panel.querySelector("[data-task-progress]").hidden = false;
    panel.querySelector("[data-task-offline]").hidden = true;
    event.detail.elt.querySelectorAll("button[type=submit], button[name=action]").forEach(function (button) {
      if (!button.disabled) {
        button.setAttribute("data-workflow-disabled", "");
        button.disabled = true;
      }
    });
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    var panel = panelFor(event);
    if (!panel) return;
    var status = event.detail.xhr.status;
    if (status === 400 || status === 409) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
    if (status === 403) {
      release(panel);
      panel.setAttribute("data-tasks-state", "permission-denied");
      panel.querySelectorAll("form, .task-list, .task-create, .task-exceptions, .task-preview").forEach(function (element) {
        element.hidden = true;
      });
      var denied = panel.querySelector("[data-task-denied]");
      denied.hidden = false;
      denied.focus();
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var panel = panelFor(event);
    if (panel) release(panel);
  });

  document.addEventListener("htmx:sendError", function (event) {
    var panel = panelFor(event);
    if (!panel) return;
    release(panel);
    panel.setAttribute("data-tasks-state", "offline");
    var notice = panel.querySelector("[data-task-offline]");
    notice.hidden = false;
    notice.focus();
  });

  document.addEventListener("htmx:afterSwap", function () {
    var panel = document.getElementById("tasks-panel");
    if (!panel) return;
    var error = panel.querySelector("[data-focus-error]");
    if (error) error.focus();
  });
}());

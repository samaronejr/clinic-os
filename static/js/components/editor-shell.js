/* Save-state reporting for includes/components/editor.html.
   Reports state only; the server decides whether text was saved (autosave
   is server-side). Typing marks the section and the indicator "unsaved";
   htmx request events move it to saving/saved/error; the browser's offline
   and online events move it to offline/unsaved. A page restored from the
   back-forward cache is marked unsaved because the server state is unknown.
   Text never leaves the form and is never written to browser storage. */
(function () {
  "use strict";

  function text(editor, state) {
    return editor.getAttribute("data-text-" + state) || "";
  }

  function setState(editor, state) {
    editor.setAttribute("data-save-state", state);
    editor.setAttribute("aria-busy", state === "saving" ? "true" : "false");
    var label = editor.querySelector("[data-editor-indicator-text]");
    if (label && text(editor, state)) {
      label.textContent = text(editor, state);
    }
    if (state === "saved") {
      editor.querySelectorAll(".editor-section--dirty").forEach(function (section) {
        section.classList.remove("editor-section--dirty");
        var note = section.querySelector("[data-editor-section-state]");
        if (note) {
          note.textContent = text(editor, "section-saved");
        }
      });
    }
  }

  function editors() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-editor]"));
  }

  document.addEventListener("input", function (event) {
    var section = event.target.closest && event.target.closest("[data-editor-section]");
    var editor = section && section.closest("[data-editor]");
    if (!editor) {
      return;
    }
    section.classList.add("editor-section--dirty");
    var note = section.querySelector("[data-editor-section-state]");
    if (note) {
      note.textContent = text(editor, "section-unsaved");
    }
    if (editor.getAttribute("data-save-state") !== "offline") {
      setState(editor, "unsaved");
    }
  });

  document.addEventListener("htmx:beforeRequest", function (event) {
    var editor = event.target.closest && event.target.closest("[data-editor]");
    if (editor) {
      setState(editor, navigator.onLine === false ? "offline" : "saving");
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var editor = event.target.closest && event.target.closest("[data-editor]");
    if (!editor) {
      return;
    }
    var xhr = event.detail && event.detail.xhr;
    if (xhr && xhr.status === 409) {
      setState(editor, "conflict");
    } else {
      setState(editor, event.detail && event.detail.successful ? "saved" : "error");
    }
  });

  window.addEventListener("offline", function () {
    editors().forEach(function (editor) { setState(editor, "offline"); });
  });

  window.addEventListener("online", function () {
    editors().forEach(function (editor) {
      if (editor.getAttribute("data-save-state") === "offline") {
        setState(editor, "unsaved");
      }
    });
  });

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      editors().forEach(function (editor) { setState(editor, "unsaved"); });
    }
  });

  window.ClinicEditor = { setState: setState };
})();

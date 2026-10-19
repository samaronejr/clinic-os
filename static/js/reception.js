(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.querySelector(".reception");
    if (!root) return;
    var pending = null;
    var stopped = false;
    var again = false;
    var submitting = new WeakSet();
    var busyButtons = new WeakSet();
    var clockRefresh = null;
    var saving = root.querySelector("[data-reception-saving]");
    var offline = root.querySelector("[data-reception-offline]");
    var error = root.querySelector("[data-focus-error]");
    if (error) error.focus();

    function networkState() {
      if (offline) offline.hidden = navigator.onLine !== false;
    }

    function editing(board) {
      return board.querySelector("form[data-reception-dirty], details[open]") ||
        (board.contains(document.activeElement) &&
          document.activeElement.closest("form"));
    }

    function markDirty(event) {
      var field = event.target;
      if (field instanceof Element && field.closest("form")) {
        field.closest("form").setAttribute("data-reception-dirty", "true");
      }
    }
    root.addEventListener("input", markDirty);
    root.addEventListener("change", markDirty);

    function updateClock() {
      var board = document.getElementById("reception-board");
      if (board && root.dataset.realtimeState === "connected" &&
          !document.hidden && !editing(board)) refresh();
    }

    root.addEventListener("submit", function (event) {
      var form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (navigator.onLine === false || submitting.has(form)) {
        event.preventDefault();
        networkState();
        return;
      }
      submitting.add(form);
      form.setAttribute("aria-busy", "true");
      form.querySelectorAll("button[type=submit]").forEach(function (button) {
        if (!button.disabled) {
          busyButtons.add(button);
          button.disabled = true;
        }
      });
      if (saving) saving.hidden = false;
    });
    window.addEventListener("offline", networkState);
    window.addEventListener("online", networkState);
    networkState();

    function refresh() {
      var board = document.getElementById("reception-board");
      var notice = root.querySelector("[data-reception-stale]");
      if (!board || stopped) return;
      if (pending) {
        again = true;
        return;
      }
      if (document.getElementById("issued-display-token")) {
        if (notice) notice.hidden = false;
        return;
      }
      if (editing(board)) {
        if (notice) notice.hidden = false;
        return;
      }
      var csrf = root.querySelector("[name=csrfmiddlewaretoken]");
      var day = document.getElementById("reception-day");
      if (!csrf || !day) return;
      pending = new AbortController();
      fetch(window.location.pathname, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "HX-Request": "true",
          "X-CSRFToken": csrf.value,
          "Content-Type": "application/x-www-form-urlencoded"
        },
        body: new URLSearchParams({ intent: "view", day: day.value }).toString(),
        signal: pending.signal
      }).then(function (response) {
        if (response.status === 403 || response.status === 404) {
          stopped = true;
          window.location.reload();
          return null;
        }
        if (!response.ok) throw new Error("reception unavailable");
        return response.text();
      }).then(function (text) {
        if (text === null || stopped) return;
        if (editing(board)) {
          if (notice) notice.hidden = false;
          return;
        }
        var page = new DOMParser().parseFromString(text, "text/html");
        var next = page.getElementById("reception-board");
        if (!next) throw new Error("reception unavailable");
        var before = board.cloneNode(true);
        var after = next.cloneNode(true);
        [before, after].forEach(function (comparison) {
          comparison.querySelectorAll(
            "input[name=csrfmiddlewaretoken], input[name=command_id]"
          ).forEach(function (input) {
            input.removeAttribute("value");
          });
        });
        if (!before.isEqualNode(after)) board.replaceWith(next);
        if (notice) notice.hidden = true;
      }).catch(function (error) {
        if (error.name !== "AbortError" && notice) notice.hidden = false;
      }).finally(function () {
        pending = null;
        if (again && !stopped) {
          again = false;
          refresh();
        }
      });
    }

    document.body.addEventListener("rt:queue", refresh);
    document.body.addEventListener("rt:poll", refresh);
    clockRefresh = window.setInterval(updateClock, 30000);
    window.addEventListener("pagehide", function () {
      stopped = true;
      window.clearInterval(clockRefresh);
      clockRefresh = null;
      if (pending) pending.abort();
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        stopped = false;
        clockRefresh = window.setInterval(updateClock, 30000);
        root.querySelectorAll("form[aria-busy=true]").forEach(function (form) {
          submitting.delete(form);
          form.removeAttribute("aria-busy");
        });
        root.querySelectorAll("button[type=submit]").forEach(function (button) {
          if (busyButtons.has(button)) {
            busyButtons.delete(button);
            button.disabled = false;
          }
        });
        if (saving) saving.hidden = true;
        refresh();
      }
    });
  });
}());

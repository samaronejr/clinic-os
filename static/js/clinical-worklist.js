(function () {
  "use strict";

  var TYPING = "input, select, textarea, [contenteditable]";

  function boardOf(root) {
    return root.querySelector("#inbox-board, #today-board");
  }

  function items(root) {
    return Array.prototype.slice.call(root.querySelectorAll("[data-worklist-item]"));
  }

  function focusAnswer(board) {
    var answer = board.querySelector("[data-focus-error], [autofocus]");
    if (answer) answer.focus();
  }

  function enhance() {
    var root = document.querySelector("[data-worklist]");
    if (!root || root.dataset.worklistEnhanced) return;
    root.dataset.worklistEnhanced = "true";
    var pending = null;
    var again = false;
    var stopped = false;
    var stale = root.querySelector("[data-worklist-stale]");
    var saving = root.querySelector("[data-worklist-saving]");
    var offline = root.querySelector("[data-worklist-offline]");

    function networkState() {
      if (offline) offline.hidden = navigator.onLine !== false;
    }

    function editing(board) {
      return board.querySelector(
        "form[data-worklist-dirty], details[open], [data-bulk-preview], " +
        "input[data-worklist-select]:checked"
      ) || (board.contains(document.activeElement) &&
        document.activeElement.closest("form"));
    }

    function markDirty(event) {
      var form = event.target instanceof Element && event.target.closest("form");
      if (form && !form.hasAttribute("data-worklist-refresh")) {
        form.setAttribute("data-worklist-dirty", "true");
      }
    }

    function refreshable(board) {
      return Boolean(board.querySelector("form[data-worklist-refresh]"));
    }

    function body(board) {
      return new URLSearchParams(
        new FormData(board.querySelector("form[data-worklist-refresh]"))
      ).toString();
    }

    function comparable(board) {
      var copy = board.cloneNode(true);
      copy.querySelectorAll("input[name=csrfmiddlewaretoken]").forEach(function (input) {
        input.removeAttribute("value");
      });
      return copy;
    }

    function refresh() {
      var board = boardOf(root);
      if (!board || stopped) return;
      if (pending) {
        again = true;
        return;
      }
      if (editing(board)) {
        if (stale) stale.hidden = false;
        return;
      }
      var csrf = root.querySelector("[name=csrfmiddlewaretoken]");
      if (!csrf) return;
      pending = new AbortController();
      fetch(window.location.pathname, refreshable(board) ? {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "HX-Request": "true",
          "X-CSRFToken": csrf.value,
          "Content-Type": "application/x-www-form-urlencoded"
        },
        body: body(board),
        signal: pending.signal
      } : {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "HX-Request": "true" },
        signal: pending.signal
      }).then(function (response) {
        if (response.status === 403 || response.status === 404) {
          stopped = true;
          window.location.reload();
          return null;
        }
        if (!response.ok) throw new Error("worklist unavailable");
        return response.text();
      }).then(function (text) {
        var current = boardOf(root);
        var page, next, focused, key, restored;
        if (text === null || stopped || !current) return;
        if (editing(current)) {
          if (stale) stale.hidden = false;
          return;
        }
        page = new DOMParser().parseFromString(text, "text/html");
        next = page.getElementById(current.id);
        if (!next) throw new Error("worklist unavailable");
        if (!comparable(current).isEqualNode(comparable(next))) {
          focused = document.activeElement && document.activeElement.closest &&
            document.activeElement.closest("[data-inbox-key]");
          key = focused ? focused.getAttribute("data-inbox-key") : "";
          current.replaceWith(next);
          if (window.htmx) window.htmx.process(next);
          restored = key ? next.querySelector('[data-inbox-key="' + key + '"]') : null;
          if (restored) restored.focus();
        }
        if (stale) stale.hidden = true;
      }).catch(function (error) {
        if (error.name !== "AbortError" && stale) stale.hidden = false;
      }).finally(function () {
        pending = null;
        if (again && !stopped) {
          again = false;
          refresh();
        }
      });
    }

    function move(step) {
      var list = items(root);
      if (!list.length) return;
      var index = list.indexOf(document.activeElement &&
        document.activeElement.closest &&
        document.activeElement.closest("[data-worklist-item]"));
      var target = list[Math.max(0, Math.min(list.length - 1, index + step))];
      if (index < 0) target = list[step > 0 ? 0 : list.length - 1];
      list.forEach(function (item) { item.removeAttribute("aria-current"); });
      target.setAttribute("aria-current", "true");
      target.focus();
    }

    document.addEventListener("keydown", function (event) {
      var key, item, primary, select;
      if (event.altKey || event.ctrlKey || event.metaKey || event.defaultPrevented) return;
      if (event.target instanceof Element && event.target.closest(TYPING)) return;
      key = event.key.toLowerCase();
      item = event.target instanceof Element &&
        event.target.matches("[data-worklist-item]") ? event.target : null;
      primary = item && item.querySelector("[data-worklist-primary]:not([disabled])");
      select = item && item.querySelector("input[data-worklist-select]");
      if (key === "j" || key === "k") {
        event.preventDefault();
        move(key === "j" ? 1 : -1);
      } else if (key === "enter" && primary) {
        event.preventDefault();
        primary.click();
      } else if (key === "x" && select) {
        event.preventDefault();
        select.checked = !select.checked;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });

    root.addEventListener("input", markDirty);
    root.addEventListener("change", markDirty);

    document.addEventListener("htmx:beforeRequest", function (event) {
      if (!root.contains(event.detail.elt)) return;
      root.setAttribute("aria-busy", "true");
      if (saving) saving.hidden = false;
      if (offline) offline.hidden = true;
    });
    document.addEventListener("htmx:beforeSwap", function (event) {
      if (!root.contains(event.detail.elt)) return;
      var status = event.detail.xhr.status;
      if (status === 400 || status === 409) {
        event.detail.shouldSwap = true;
        event.detail.isError = false;
      } else if (status === 403 || status === 404) {
        stopped = true;
        window.location.reload();
      }
    });
    document.addEventListener("htmx:afterRequest", function () {
      root.removeAttribute("aria-busy");
      if (saving) saving.hidden = true;
    });
    document.addEventListener("htmx:afterSwap", function () {
      var board = boardOf(root);
      if (board) focusAnswer(board);
      if (stale) stale.hidden = true;
    });
    document.addEventListener("htmx:sendError", function () {
      root.removeAttribute("aria-busy");
      if (saving) saving.hidden = true;
      if (offline) {
        offline.hidden = false;
        offline.focus();
      }
    });

    window.addEventListener("offline", networkState);
    window.addEventListener("online", networkState);
    networkState();
    document.body.addEventListener("rt:inbox", refresh);
    document.body.addEventListener("rt:poll", refresh);
    window.addEventListener("pagehide", function () {
      stopped = true;
      if (pending) pending.abort();
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        stopped = false;
        refresh();
      }
    });
    var board = boardOf(root);
    if (board) focusAnswer(board);
  }

  document.addEventListener("DOMContentLoaded", enhance);
}());

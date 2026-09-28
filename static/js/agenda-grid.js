/* Drag-to-move and the M move dialog for templates/scheduling/partials/agenda_grid.html.
   Moving is always the dialog (click, Enter or M on an appointment); dragging
   an appointment (mouse or pen) onto a free time in its own practitioner's
   column is a shortcut that submits the same form. The server decides every move: the
   form carries the revision the grid rendered, a stale one answers 409 with
   the current grid, and this script only swaps it in and keeps focus. Reads
   only opaque ids, revisions and times from data-* attributes. */
(function () {
  "use strict";

  var DRAG_DISTANCE = 6;
  var drag = null;
  var suppressClick = false;
  var pendingFocus = null;
  var pendingPlace = null;

  function dialog() {
    return document.querySelector("[data-move-dialog]");
  }

  function cellOf(node) {
    return node && node.closest ? node.closest("[data-grid-cell]") : null;
  }

  function column(link) {
    return cellOf(link).getAttribute("data-column");
  }

  function freeTimes(link) {
    var grid = link.closest("[data-agenda-grid]");
    var own = cellOf(link);
    var times = [];
    grid.querySelectorAll("[data-grid-cell][data-column=\"" + column(link) + "\"]").forEach(function (cell) {
      if (cell === own || cell.hasAttribute("data-free")) {
        times.push(cell.getAttribute("data-slot"));
      }
    });
    return times;
  }

  function fill(link, start) {
    var box = dialog();
    var form = box.querySelector("[data-move-form]");
    form.elements.appointment_id.value = link.getAttribute("data-appointment");
    form.elements.expected_revision.value = link.getAttribute("data-revision");
    form.elements.duration.value = link.getAttribute("data-duration");
    var name = link.querySelector("strong");
    var meta = link.querySelector(".resource-slot-meta");
    box.querySelector("[data-move-summary]").textContent =
      (name ? name.textContent : "") + (meta ? " · " + meta.textContent : "");
    var select = form.elements.start;
    select.textContent = "";
    freeTimes(link).forEach(function (time) {
      var option = document.createElement("option");
      option.value = time;
      option.textContent = time;
      option.selected = time === (start || link.getAttribute("data-start"));
      select.appendChild(option);
    });
    pendingFocus = link.getAttribute("data-appointment");
    return form;
  }

  function openFor(link) {
    var box = dialog();
    if (!box || !window.ClinicDialog) {
      return false;
    }
    fill(link);
    link.setAttribute("data-dialog-open", box.id);
    window.ClinicDialog.open(link);
    return true;
  }

  function submitMove(link, start) {
    var form = fill(link, start);
    form.elements.start.value = start;
    form.requestSubmit();
  }

  function busy(on) {
    var box = dialog();
    if (!box) {
      return;
    }
    box.querySelector("[data-move-progress]").hidden = !on;
    box.querySelector("[data-move-confirm]").disabled = on;
    var grid = document.getElementById("agenda-grid");
    if (grid) {
      grid.setAttribute("aria-busy", on ? "true" : "false");
    }
  }

  function clearTargets() {
    document.querySelectorAll(".is-drop-target").forEach(function (cell) {
      cell.classList.remove("is-drop-target");
    });
  }

  function endDrag() {
    if (drag) {
      drag.link.classList.remove("is-dragging");
      clearTargets();
    }
    drag = null;
  }

  function dropCell(event) {
    var cell = cellOf(document.elementFromPoint(event.clientX, event.clientY));
    if (!cell || cell.getAttribute("data-column") !== drag.column) {
      return null;
    }
    return cell.hasAttribute("data-free") || cell === drag.origin ? cell : null;
  }

  // Capture: fill the dialog before components/dialog.js opens it, and
  // swallow the click that ends a drag.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("[data-move]");
    if (!link) {
      return;
    }
    event.preventDefault();
    if (suppressClick) {
      suppressClick = false;
      event.stopPropagation();
      return;
    }
    if (openFor(link)) {
      event.stopPropagation();
    }
  }, true);

  document.addEventListener("keydown", function (event) {
    if (event.key !== "m" && event.key !== "M") {
      if (event.key === "Escape" && drag) {
        endDrag();
      }
      return;
    }
    var cell = cellOf(event.target);
    var link = cell && cell.querySelector("[data-move]");
    if (link && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      openFor(link);
    }
  });

  document.addEventListener("pointerdown", function (event) {
    // A new gesture: the click that ended the previous drag (if any) is gone.
    suppressClick = false;
    var link = event.target.closest && event.target.closest("[data-move]");
    // Touch keeps native scrolling; a tap opens the dialog instead.
    if (!link || event.button !== 0 || event.pointerType === "touch") {
      return;
    }
    drag = {
      link: link,
      origin: cellOf(link),
      column: column(link),
      x: event.clientX,
      y: event.clientY,
      moving: false,
      target: null
    };
  });

  document.addEventListener("pointermove", function (event) {
    if (!drag) {
      return;
    }
    if (!drag.moving) {
      if (Math.abs(event.clientX - drag.x) + Math.abs(event.clientY - drag.y) < DRAG_DISTANCE) {
        return;
      }
      drag.moving = true;
      drag.link.classList.add("is-dragging");
    }
    event.preventDefault();
    var target = dropCell(event);
    if (target !== drag.target) {
      clearTargets();
      if (target) {
        target.classList.add("is-drop-target");
      }
      drag.target = target;
    }
  });

  document.addEventListener("pointerup", function (event) {
    if (!drag) {
      return;
    }
    var current = drag;
    var target = current.moving ? dropCell(event) : null;
    endDrag();
    if (!current.moving) {
      return;
    }
    suppressClick = true;
    if (target && target !== current.origin) {
      submitMove(current.link, target.getAttribute("data-slot"));
    }
  });

  document.addEventListener("pointercancel", endDrag);

  // A link is natively draggable; that drag would cancel the pointer stream.
  document.addEventListener("dragstart", function (event) {
    if (event.target.closest && event.target.closest("[data-move]")) {
      event.preventDefault();
    }
  });

  document.addEventListener("htmx:beforeRequest", function (event) {
    if (event.target.matches("[data-move-form]")) {
      busy(true);
    }
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (event.detail.target && event.detail.target.id === "agenda-grid-results") {
      endDrag();
      // Close the modal first: focus cannot reach the new grid behind it.
      var box = dialog();
      if (box && box.open && event.detail.requestConfig &&
          event.detail.requestConfig.elt && event.detail.requestConfig.elt.matches("[data-move-form]")) {
        box.close();
      }
      // A lost race or a taken time answers 409 with the current grid.
      if (event.detail.xhr.status === 409) {
        event.detail.shouldSwap = true;
        event.detail.isError = false;
      }
      // A realtime refetch replaces the grid under the user: keep their place.
      var cell = cellOf(document.activeElement);
      if (cell && cell.closest("#agenda-grid")) {
        var link = cell.querySelector("[data-appointment]");
        var row = cell.parentNode;
        pendingPlace = {
          slot: cell.getAttribute("data-slot"),
          index: Array.prototype.indexOf.call(row.children, cell)
        };
        if (!pendingFocus && link) {
          pendingFocus = link.getAttribute("data-appointment");
        }
      }
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    if (!event.target.matches("[data-move-form]")) {
      return;
    }
    busy(false);
    var box = dialog();
    if (event.detail.xhr && event.detail.xhr.status) {
      if (box.open) {
        box.close();
      }
    } else {
      // Nothing reached the server: keep the dialog and say so.
      box.querySelector("[data-move-progress]").hidden = false;
      box.querySelector("[data-move-progress]").textContent = box.getAttribute("data-text-offline") || "";
    }
  });

  document.addEventListener("htmx:afterSettle", function (event) {
    if (event.target.id !== "agenda-grid-results" || (!pendingFocus && !pendingPlace)) {
      return;
    }
    var link = pendingFocus &&
      event.target.querySelector("[data-appointment=\"" + pendingFocus + "\"]");
    var cell = cellOf(link);
    if (!cell && pendingPlace) {
      var slot = event.target.querySelector("[data-grid-cell][data-slot=\"" + pendingPlace.slot + "\"]");
      cell = slot ? slot.parentNode.children[pendingPlace.index] : null;
    }
    pendingFocus = null;
    pendingPlace = null;
    if (!cell || !cell.matches("[data-grid-cell]")) {
      return;
    }
    event.target.querySelectorAll("[role=gridcell]").forEach(function (item) {
      item.tabIndex = item === cell ? 0 : -1;
    });
    cell.focus();
  });
})();

/* Agenda board behavior for templates/scheduling/agenda_grid.html (variant A).
   An appointment opens the appointment dialog (click, Enter or M): the move
   form with the professional's free times, a server slot finder for other
   days, and only the lifecycle actions the board rendered (data-actions).
   Dragging an appointment (mouse or pen) onto a free time in its own column
   submits the same move form. A bookable free cell (data-book) opens the
   booking dialog (click or Enter). The server decides every outcome: forms
   carry the revision the board rendered, a stale or taken one answers 409
   with the current board, and this script only swaps it in, keeps focus and
   says when a realtime refetch shows someone else's change. Reads only opaque
   ids, revisions, times and action names from data-* attributes. */
(function () {
  "use strict";

  var DRAG_DISTANCE = 6;
  var drag = null;
  var suppressClick = false;
  var pendingFocus = null;
  var editing = null;
  var editingRevision = null;
  var moveInFlight = false;
  var pendingPlace = null;
  var bookCell = null;
  var swapOrigin = null;
  var previousSignature = null;

  function dialog() {
    return document.querySelector("[data-move-dialog]");
  }

  function bookDialog() {
    return document.querySelector("[data-book-dialog]");
  }

  function cellOf(node) {
    return node && node.closest ? node.closest("[data-grid-cell]") : null;
  }

  function column(link) {
    return cellOf(link).getAttribute("data-column");
  }

  function uuid4() {
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    var hex = Array.prototype.map.call(bytes, function (value) {
      return (value + 256).toString(16).slice(1);
    }).join("");
    return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) +
      "-" + hex.slice(16, 20) + "-" + hex.slice(20);
  }

  function civil(day) {
    var parts = (day || "").split("-");
    return parts.length === 3 ? parts[2] + "/" + parts[1] + "/" + parts[0] : "";
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

  function actionsOf(link) {
    var raw = link.getAttribute("data-actions") || "";
    return raw ? raw.split(" ") : [];
  }

  function fillLifecycle(box, link) {
    var form = box.querySelector("[data-move-lifecycle]");
    if (!form) {
      return 0;
    }
    var allowed = actionsOf(link);
    form.elements.appointment_id.value = link.getAttribute("data-appointment");
    form.elements.expected_revision.value = link.getAttribute("data-revision");
    form.elements.command_id.value = uuid4();
    form.elements.day.value = cellOf(link).getAttribute("data-day");
    form.querySelectorAll("[data-lifecycle]").forEach(function (button) {
      button.hidden = allowed.indexOf(button.getAttribute("data-lifecycle")) === -1;
      button.disabled = false;
    });
    form.hidden = !allowed.length;
    return allowed.length;
  }

  function fill(link, start) {
    var box = dialog();
    var movable = link.hasAttribute("data-move");
    var form = box.querySelector("[data-move-form]");
    var cell = cellOf(link);
    form.elements.appointment_id.value = link.getAttribute("data-appointment");
    form.elements.expected_revision.value = link.getAttribute("data-revision");
    form.elements.duration.value = link.getAttribute("data-duration");
    form.elements.day.value = cell.getAttribute("data-day");
    var name = link.querySelector("strong");
    var meta = link.querySelector(".resource-slot-meta");
    box.querySelector("[data-move-summary]").textContent =
      (name ? name.textContent : "") + (meta ? " · " + meta.textContent : "");
    var select = form.elements.start;
    select.textContent = "";
    if (movable) {
      freeTimes(link).forEach(function (time) {
        var option = document.createElement("option");
        option.value = time;
        option.textContent = time;
        option.selected = time === (start || link.getAttribute("data-start"));
        select.appendChild(option);
      });
    }
    var find = box.querySelector("[data-move-find]");
    if (find) {
      find.hidden = !movable;
      find.elements.appointment_id.value = link.getAttribute("data-appointment");
      find.elements.day.value = cell.getAttribute("data-day");
      box.querySelector("[data-move-find-status]").textContent = "";
    }
    var count = fillLifecycle(box, link);
    form.hidden = !movable;
    box.querySelector("[data-move-close-only]").hidden = movable || !count;
    box.querySelector("[data-move-confirm]").disabled = false;
    box.querySelector("[data-move-conflict]").hidden = true;
    box.querySelector("#agenda-move-title").textContent = box.getAttribute(
      movable ? "data-text-move-title" : "data-text-actions-title") || "";
    editing = link.getAttribute("data-appointment");
    editingRevision = link.getAttribute("data-revision");
    return form;
  }

  function openFor(link) {
    var box = dialog();
    if (!box || !window.ClinicDialog) {
      return false;
    }
    pendingFocus = null;
    pendingPlace = null;
    fill(link);
    link.setAttribute("data-dialog-open", box.id);
    window.ClinicDialog.open(link);
    return true;
  }

  function openBook(cell) {
    var box = bookDialog();
    if (!box || !window.ClinicDialog) {
      return false;
    }
    var form = box.querySelector("[data-book-form]");
    var lane = cell.getAttribute("data-lane");
    var kind = cell.getAttribute("data-lane-kind");
    var slot = cell.getAttribute("data-slot");
    form.elements.day.value = cell.getAttribute("data-day");
    form.elements.idempotency_key.value = uuid4();
    if (kind === "practitioner" && form.querySelector("[data-book-professional] option[value=\"" + lane + "\"]")) {
      form.elements.practitioner.value = lane;
    }
    box.querySelectorAll("[data-book-resource]").forEach(function (input) {
      input.checked = kind !== "practitioner" && input.value === lane;
    });
    var start = form.querySelector("[data-book-start]");
    if (!start.querySelector("option[value=\"" + slot + "\"]")) {
      var option = document.createElement("option");
      option.value = slot;
      option.textContent = slot;
      start.appendChild(option);
    }
    start.value = slot;
    box.querySelector("[data-book-summary]").textContent = civil(cell.getAttribute("data-day")) + " · " + slot;
    box.querySelector("[data-book-progress]").hidden = true;
    bookCell = cell;
    cell.setAttribute("data-dialog-open", box.id);
    window.ClinicDialog.open(cell);
    return true;
  }

  function submitMove(link, start) {
    var form = fill(link, start);
    form.elements.start.value = start;
    form.requestSubmit();
  }

  function busy(box, on) {
    if (!box) {
      return;
    }
    box.querySelectorAll("[data-move-progress], [data-book-progress]").forEach(function (node) {
      node.hidden = !on;
    });
    box.querySelectorAll("button[type=submit]").forEach(function (button) {
      button.disabled = on;
    });
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

  function dialogOf(node) {
    return node && node.closest ? node.closest("[data-move-dialog], [data-book-dialog]") : null;
  }

  // Capture: fill the dialog before components/dialog.js opens it, and
  // swallow the click that ends a drag.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("[data-move], .agenda-slot-actions");
    if (link) {
      event.preventDefault();
      if (suppressClick) {
        suppressClick = false;
        event.stopPropagation();
        return;
      }
      if (openFor(link)) {
        event.stopPropagation();
      }
      return;
    }
    var free = event.target.closest && event.target.closest("[data-grid-cell][data-book]");
    if (free && !event.target.closest("a, button") && openBook(free)) {
      event.stopPropagation();
    }
  }, true);

  document.addEventListener("keydown", function (event) {
    var cell = cellOf(event.target);
    if ((event.key === "Enter" || event.key === " ") && cell && event.target === cell &&
        cell.hasAttribute("data-book") && !cell.querySelector("a, button")) {
      event.preventDefault();
      openBook(cell);
      return;
    }
    if (event.key !== "m" && event.key !== "M") {
      if (event.key === "Escape" && drag) {
        endDrag();
      }
      return;
    }
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
    var elt = event.target;
    if (elt.matches("[data-move-form], [data-move-lifecycle], [data-book-form]")) {
      moveInFlight = true;
      pendingFocus = elt.matches("[data-book-form]") ? null : editing;
      if (elt.matches("[data-book-form]") && bookCell) {
        pendingPlace = { slot: bookCell.getAttribute("data-slot"), index: Array.prototype.indexOf.call(bookCell.parentNode.children, bookCell) };
      }
      busy(dialogOf(elt), true);
    }
  });

  // Return focus to the appointment's cell in the grid as it is now (a
  // realtime refetch may have replaced the opener), else the grid's tab stop.
  function focusCell(id) {
    var grid = document.getElementById("agenda-grid");
    if (!grid) {
      return;
    }
    var link = id && grid.querySelector("[data-appointment=\"" + id + "\"]");
    var cell = cellOf(link) ||
      grid.querySelector("[role=gridcell][tabindex=\"0\"]") ||
      grid.querySelector("[role=gridcell]");
    if (!cell) {
      return;
    }
    grid.querySelectorAll("[role=gridcell]").forEach(function (item) {
      item.tabIndex = item === cell ? 0 : -1;
    });
    cell.focus();
  }

  // Cancel is a plain button: the forms post through HTMX, which would
  // swallow a formmethod="dialog" submit, so the dialog is closed here.
  document.addEventListener("click", function (event) {
    var cancel = event.target.closest && event.target.closest("[data-dialog-close], [data-move-close]");
    var box = dialogOf(cancel);
    if (!cancel || !box) {
      return;
    }
    event.preventDefault();
    pendingFocus = null;
    pendingPlace = null;
    box.close();
    // The close event is a queued task; return focus now, and again there
    // (after components/dialog.js returns it to the opener).
    if (box.matches("[data-move-dialog]")) {
      focusCell(editing);
    }
  });

  document.addEventListener("click", function (event) {
    var dismiss = event.target.closest && event.target.closest("[data-remote-change] [data-state-action]");
    if (dismiss) {
      dismiss.closest("[data-remote-change]").textContent = "";
      focusCell(null);
    }
  });

  // Cancel, Escape or an abandoned offline retry: nothing moved, so focus goes
  // back to the invoking appointment. A move in flight places focus itself.
  document.addEventListener("close", function (event) {
    if (!event.target.matches || !event.target.matches("[data-move-dialog]") ||
        event.target.open || moveInFlight) {
      return;
    }
    focusCell(editing);
    editing = null;
  }, true);

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (!event.detail.target || event.detail.target.id !== "agenda-grid-results") {
      return;
    }
    endDrag();
    var elt = event.detail.requestConfig && event.detail.requestConfig.elt;
    swapOrigin = elt && elt.matches && elt.matches("[data-agenda-grid]") ? "refetch" : "own";
    previousSignature = event.detail.target.getAttribute("data-signature");
    // Close the modal first: focus cannot reach the new grid behind it.
    var box = dialogOf(elt);
    if (box && box.open) {
      box.close();
    }
    // A lost race or a taken time answers 409 with the current board.
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
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var elt = event.target;
    if (!elt.matches("[data-move-form], [data-move-lifecycle], [data-book-form]")) {
      return;
    }
    moveInFlight = false;
    var box = dialogOf(elt);
    busy(box, false);
    var status = event.detail.xhr ? event.detail.xhr.status : 0;
    if (status !== 200 && status !== 409) {
      // No board came back, so nothing will place focus after a swap.
      pendingFocus = null;
    }
    if (status) {
      if (box.open) {
        box.close();
      }
    } else {
      // Nothing reached the server: keep the dialog and say so.
      var progress = box.querySelector("[data-move-progress], [data-book-progress]");
      progress.hidden = false;
      progress.textContent = box.getAttribute("data-text-offline") || "";
    }
  });

  // The slot finder swapped another day's free times into the move select.
  document.addEventListener("htmx:afterSwap", function (event) {
    var select = event.detail.target;
    if (!select || !select.matches || !select.matches("[data-move-start]")) {
      return;
    }
    var box = dialog();
    var find = box.querySelector("[data-move-find]");
    var count = select.options.length;
    box.querySelector("[data-move-form]").elements.day.value = find.elements.day.value;
    box.querySelector("[data-move-confirm]").disabled = !count;
    box.querySelector("[data-move-find-status]").textContent = count
      ? (box.getAttribute("data-text-found") || "").replace("__N__", String(count))
      : box.getAttribute("data-text-none") || "";
    if (count) {
      select.focus();
    }
  });

  function showRemoteChange() {
    var region = document.querySelector("[data-remote-change]");
    var note = document.getElementById("agenda-remote-change-note");
    if (region && note && !region.firstElementChild) {
      region.appendChild(note.content.cloneNode(true));
    }
  }

  // Someone else changed the appointment the open dialog is about. Warn only:
  // the server still decides, and a stale submit answers 409 with the board.
  function flagConflict(results) {
    var box = dialog();
    if (!box || !box.open || !editing) {
      return;
    }
    var link = results.querySelector("[data-appointment=\"" + editing + "\"]");
    if (link && link.getAttribute("data-revision") === editingRevision) {
      return;
    }
    box.querySelector("[data-move-conflict]").hidden = false;
  }

  document.addEventListener("htmx:afterSettle", function (event) {
    if (event.target.id !== "agenda-grid-results") {
      return;
    }
    if (swapOrigin === "refetch" && previousSignature !== event.target.getAttribute("data-signature")) {
      showRemoteChange();
      flagConflict(event.target);
    }
    swapOrigin = null;
    if (!pendingFocus && !pendingPlace) {
      return;
    }
    // A refetch may finish while the user is still deciding. Never consume
    // a stale restoration target behind the modal or replace its tab stop.
    var box = dialog();
    var book = bookDialog();
    if ((box && box.open) || (book && book.open)) {
      pendingFocus = null;
      pendingPlace = null;
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

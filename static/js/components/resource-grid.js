/* Keyboard model for includes/components/resource-grid.html (ARIA grid).
   One tab stop per grid (roving tabindex on gridcells). Arrows move by
   cell, Home/End to the row edges, Ctrl+Home/Ctrl+End to the grid corners,
   Enter or Space follows the cell's link. Row headers are skipped. */
(function () {
  "use strict";

  function rows(grid) {
    return Array.prototype.slice.call(grid.querySelectorAll("[role=row]")).map(function (row) {
      return Array.prototype.slice.call(row.querySelectorAll("[role=gridcell]"));
    }).filter(function (cells) { return cells.length; });
  }

  function locate(matrix, cell) {
    for (var r = 0; r < matrix.length; r += 1) {
      var c = matrix[r].indexOf(cell);
      if (c !== -1) {
        return [r, c];
      }
    }
    return [0, 0];
  }

  function activate(matrix, cell) {
    matrix.forEach(function (cells) {
      cells.forEach(function (item) { item.tabIndex = item === cell ? 0 : -1; });
    });
    cell.focus();
  }

  function enhance(root) {
    root.querySelectorAll("[data-resource-grid]:not([data-enhanced])").forEach(function (grid) {
      grid.setAttribute("data-enhanced", "");
      if (grid.getAttribute("aria-disabled") === "true") {
        return;
      }
      var matrix = rows(grid);
      if (!matrix.length) {
        return;
      }
      matrix.forEach(function (cells) {
        cells.forEach(function (cell) {
          cell.tabIndex = -1;
          cell.querySelectorAll("a, button").forEach(function (control) {
            control.tabIndex = -1;
          });
        });
      });
      matrix[0][0].tabIndex = 0;
      grid.addEventListener("keydown", function (event) {
        var cell = event.target.closest("[role=gridcell]");
        if (!cell) {
          return;
        }
        var at = locate(matrix, cell);
        var r = at[0];
        var c = at[1];
        var last = matrix.length - 1;
        if (event.key === "ArrowRight") {
          c = Math.min(c + 1, matrix[r].length - 1);
        } else if (event.key === "ArrowLeft") {
          c = Math.max(c - 1, 0);
        } else if (event.key === "ArrowDown") {
          r = Math.min(r + 1, last);
        } else if (event.key === "ArrowUp") {
          r = Math.max(r - 1, 0);
        } else if (event.key === "Home") {
          r = event.ctrlKey ? 0 : r;
          c = 0;
        } else if (event.key === "End") {
          r = event.ctrlKey ? last : r;
          c = matrix[r].length - 1;
        } else if (event.key === "Enter" || event.key === " ") {
          var target = cell.querySelector("a, button");
          if (target && cell.getAttribute("aria-disabled") !== "true") {
            event.preventDefault();
            target.click();
          }
          return;
        } else {
          return;
        }
        event.preventDefault();
        c = Math.min(c, matrix[r].length - 1);
        activate(matrix, matrix[r][c]);
      });
      grid.addEventListener("click", function (event) {
        var cell = event.target.closest("[role=gridcell]");
        if (cell) {
          activate(matrix, cell);
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  document.addEventListener("htmx:load", function (event) { enhance(event.target); });
})();

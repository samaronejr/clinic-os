{
  let goToTab = false;

  const panelFor = (event) => {
    const detail = event.detail || {};
    const element = detail.requestConfig?.elt || detail.elt;
    return element instanceof Element
      ? element.closest("#patient-workspace")
      : null;
  };

  document.addEventListener("keydown", (event) => {
    const panel = document.getElementById("patient-workspace");
    const target = event.target;
    if (
      !panel || event.defaultPrevented || event.ctrlKey || event.altKey ||
      event.metaKey ||
      (target instanceof Element &&
        target.closest("input, select, textarea, [contenteditable], dialog"))
    ) {
      goToTab = false;
      return;
    }
    const key = event.key.toLowerCase();
    const activate = goToTab;
    goToTab = key === "g";
    if (key === "g") {
      event.preventDefault();
    } else if (activate && "otemrdpfcv".includes(key) && key.length === 1) {
      const button = panel.querySelector(
        `button[data-workspace-key="${key}"]:not(:disabled)`,
      );
      if (button instanceof HTMLButtonElement) {
        event.preventDefault();
        button.click();
      }
    }
  });
  document.addEventListener("pointerdown", () => { goToTab = false; });
  document.addEventListener("focusin", (event) => {
    if (
      event.target instanceof Element &&
      event.target.closest("input, select, textarea, [contenteditable]")
    ) goToTab = false;
  });

  document.addEventListener("htmx:beforeRequest", (event) => {
    const panel = panelFor(event);
    if (!panel) return;
    panel.setAttribute("aria-busy", "true");
    // Serialization is complete. Freeze edits until this request is acknowledged.
    panel.querySelectorAll("input, select, textarea, button").forEach((field) => {
      if (!field.disabled) {
        field.setAttribute("data-workspace-disabled", "");
        field.disabled = true;
      }
    });
  });

  document.addEventListener("htmx:beforeSwap", (event) => {
    if (!panelFor(event)) return;
    const status = event.detail.xhr.status;
    if (status === 400 || status === 409 || status === 403) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  document.addEventListener("htmx:afterRequest", (event) => {
    const panel = panelFor(event);
    if (!panel) return;
    panel.removeAttribute("aria-busy");
    panel.querySelectorAll("[data-workspace-disabled]").forEach((field) => {
      field.disabled = false;
      field.removeAttribute("data-workspace-disabled");
    });
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail.target;
    if (!(target instanceof Element) || target.id !== "patient-workspace") return;
    const panel = document.getElementById("patient-workspace");
    if (!panel) return;
    const focus = panel.querySelector(
      "[data-focus-error], [data-workspace-outcome], #patient-workspace-title",
    );
    if (focus instanceof HTMLElement) focus.focus();
  });
}

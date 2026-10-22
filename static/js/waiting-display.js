(() => {
  const pause = document.querySelector("[data-display-pause]");
  const offline = document.querySelector("[data-display-offline]");
  if (!pause || !offline) return;
  let paused = false;
  let pending = false;
  let active = true;
  let timer;
  let controller;
  const stop = () => {
    active = false;
    clearInterval(timer);
    controller?.abort();
  };
  const update = async () => {
    if (!active || paused || pending || document.hidden) return;
    pending = true;
    controller = new AbortController();
    try {
      const response = await fetch(location.pathname, {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (response.status !== 200 && response.status !== 403) {
        throw new Error("Display unavailable");
      }
      const next = new DOMParser().parseFromString(await response.text(), "text/html");
      if (response.status === 403) {
        document.querySelector("#waiting-display").replaceWith(
          next.querySelector("#waiting-display"),
        );
        stop();
      } else {
        document.querySelector(".waiting-tickets").replaceWith(
          next.querySelector(".waiting-tickets"),
        );
        offline.hidden = true;
      }
    } catch (error) {
      if (error.name !== "AbortError") offline.hidden = false;
    } finally {
      pending = false;
    }
  };
  const start = () => {
    active = true;
    timer = setInterval(update, 15000);
  };
  pause.hidden = false;
  pause.addEventListener("click", () => {
    paused = !paused;
    pause.setAttribute("aria-pressed", String(paused));
    if (paused) controller?.abort();
    else update();
  });
  document.addEventListener("visibilitychange", update);
  window.addEventListener("online", update);
  window.addEventListener("offline", () => { offline.hidden = false; });
  window.addEventListener("pagehide", stop);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      start();
      update();
    }
  });
  start();
})();

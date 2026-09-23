/* Shell enhancement: register the static-only worker for signed-in sessions.
   Everything the shell does works without this file; it only adds the
   installable cache of versioned static assets. */
(function () {
  "use strict";
  var workerUrl = document.documentElement.getAttribute("data-service-worker");
  if (!workerUrl || !("serviceWorker" in navigator)) {
    return;
  }
  window.addEventListener("load", function () {
    navigator.serviceWorker.register(workerUrl, { scope: "/" }).catch(function () {
      /* Registration is optional; the shell keeps working from the network. */
    });
  });
})();

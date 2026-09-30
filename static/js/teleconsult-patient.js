/* Patient teleconsult surfaces: device checks in the waiting room and the
   room's local media, call controls and honest connection state.

   Scope: only pages carrying `data-teleconsult="waiting"` or `"room"`. No
   provider SDK is loaded; media stays local (synthetic capability record).
   Nothing here touches storage, history state, the title or metadata, and
   every stream is stopped when the patient leaves, retries or hides the page. */
(function () {
  "use strict";

  var root = document.querySelector("[data-teleconsult]");
  if (!root || !window.fetch) {
    return;
  }

  var POLL_MS = 5000;
  var FAILURES = {
    unsupported: {
      title: "Este navegador não permite vídeo",
      text: "O navegador não oferece acesso à câmera e ao microfone.",
      help: "Atualize o navegador ou abra o link em Chrome, Firefox, Safari ou Edge.",
    },
    insecure: {
      title: "Endereço sem segurança",
      text: "A câmera e o microfone só funcionam em um endereço seguro (https).",
      help: "Abra exatamente o link enviado pela clínica.",
    },
    denied: {
      title: "Permissão negada",
      text: "O navegador bloqueou a câmera ou o microfone.",
      help: "Toque no ícone de cadeado ou de câmera ao lado do endereço, permita câmera e microfone e tente de novo. No celular, confira também as permissões do navegador nos ajustes do aparelho.",
    },
    missing: {
      title: "Nenhum dispositivo encontrado",
      text: "Não encontramos câmera nem microfone neste aparelho.",
      help: "Conecte um dispositivo ou abra o link no celular. Se não for possível, a clínica pode continuar por telefone.",
    },
    busy: {
      title: "Dispositivo em uso",
      text: "A câmera ou o microfone está sendo usado por outro aplicativo.",
      help: "Feche outros aplicativos de vídeo ou abas com chamadas e tente de novo.",
    },
    error: {
      title: "Não foi possível ligar câmera e microfone",
      text: "Ocorreu um erro inesperado ao acessar os dispositivos.",
      help: "Recarregue a página e tente de novo. Se persistir, fale com a clínica.",
    },
    lost: {
      title: "Câmera ou microfone desconectado",
      text: "Um dispositivo parou de responder durante a consulta.",
      help: "Verifique a conexão do dispositivo e tente ligá-lo de novo. Você continua na sala.",
    },
    offline: {
      tone: "error",
      connection: true,
      title: "Conexão perdida",
      text: "Sua internet caiu ou está instável. Nada foi gravado.",
      help: "Verifique o Wi-Fi ou os dados móveis e toque em Reconectar. Sua vaga na sala continua reservada.",
    },
    expired: {
      tone: "error",
      connection: true,
      title: "Sua sessão de acesso expirou",
      text: "Por segurança, o acesso ao portal tem duração limitada.",
      help: "Peça um novo código à clínica e entre de novo. Nada foi gravado.",
    },
    ended: {
      tone: "muted",
      connection: true,
      title: "A consulta foi encerrada",
      text: "O médico encerrou a sala. Câmera e microfone foram desligados.",
      help: "Esta sala não reabre. Se precisar de outra consulta, fale com a clínica.",
    },
    failed: {
      tone: "error",
      connection: true,
      title: "A consulta não pôde continuar",
      text: "A sala foi fechada pela clínica ou pelo provedor.",
      help: "Fale com a clínica para combinar um novo horário.",
    },
  };
  var DEVICE_LABELS = {
    camera: {
      idle: "Não testada",
      ready: "Pronta",
      denied: "Permissão negada",
      missing: "Não encontrada",
      busy: "Em uso por outro app",
      unsupported: "Sem suporte",
      insecure: "Endereço inseguro",
      error: "Com erro",
    },
    microphone: {
      idle: "Não testado",
      ready: "Pronto",
      denied: "Permissão negada",
      missing: "Não encontrado",
      busy: "Em uso por outro app",
      unsupported: "Sem suporte",
      insecure: "Endereço inseguro",
      error: "Com erro",
    },
  };

  function support() {
    if (!window.isSecureContext) {
      return "insecure";
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return "unsupported";
    }
    return "";
  }

  function classify(error) {
    var name = error && error.name;
    if (name === "NotAllowedError" || name === "PermissionDeniedError") {
      return "denied";
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError" || name === "OverconstrainedError") {
      return "missing";
    }
    if (name === "NotReadableError" || name === "TrackStartError" || name === "AbortError") {
      return "busy";
    }
    if (name === "SecurityError") {
      return "insecure";
    }
    if (name === "TypeError") {
      return "unsupported";
    }
    return "error";
  }

  function probe(constraints) {
    return navigator.mediaDevices.getUserMedia(constraints).then(
      function (stream) {
        return { stream: stream, state: "ready" };
      },
      function (error) {
        return { stream: null, state: classify(error) };
      }
    );
  }

  /* One prompt for both devices; on refusal, probe each so the patient learns
     exactly which device is missing and can still continue with the other. */
  function acquire() {
    var unsupported = support();
    if (unsupported) {
      return Promise.resolve({ stream: null, camera: unsupported, microphone: unsupported, reason: unsupported });
    }
    return navigator.mediaDevices.getUserMedia({ video: true, audio: true }).then(
      function (stream) {
        return { stream: stream, camera: "ready", microphone: "ready", reason: "" };
      },
      function (error) {
        var reason = classify(error);
        return probe({ video: true, audio: false }).then(function (camera) {
          return probe({ video: false, audio: true }).then(function (microphone) {
            var tracks = [];
            if (camera.stream) {
              tracks = tracks.concat(camera.stream.getTracks());
            }
            if (microphone.stream) {
              tracks = tracks.concat(microphone.stream.getTracks());
            }
            return {
              stream: tracks.length ? new MediaStream(tracks) : null,
              camera: camera.state,
              microphone: microphone.state,
              reason: camera.state !== "ready" ? camera.state : microphone.state !== "ready" ? microphone.state : reason,
            };
          });
        });
      }
    );
  }

  function stopStream(stream) {
    if (stream) {
      stream.getTracks().forEach(function (track) {
        track.stop();
      });
    }
  }

  function fillFailure(box, key) {
    var failure = FAILURES[key] || FAILURES.error;
    box.className = "feedback feedback--" + (failure.tone || "error");
    box.querySelector("[data-" + box.dataset.prefix + "-title]").textContent = failure.title;
    box.querySelector("[data-" + box.dataset.prefix + "-text]").textContent = failure.text;
    box.querySelector("[data-" + box.dataset.prefix + "-help]").textContent = failure.help;
    box.setAttribute("data-failure", key);
    box.hidden = false;
  }

  function setBadge(element, kind, state) {
    element.className = "badge " + (state === "ready" ? "badge--success" : state === "idle" ? "badge--pending" : "badge--error");
    element.textContent = DEVICE_LABELS[kind][state] || DEVICE_LABELS[kind].error;
    element.setAttribute("data-device-state", state);
  }

  /* ---------------------------------------------------------------- */
  /* Waiting room: explicit device test, never automatic.              */
  /* ---------------------------------------------------------------- */
  function waitingRoom() {
    var panel = root.querySelector("[data-device-check]");
    if (!panel) {
      return;
    }
    var testButton = panel.querySelector("[data-device-test]");
    var stopButton = panel.querySelector("[data-device-stop]");
    var preview = panel.querySelector("[data-preview]");
    var video = panel.querySelector("[data-preview-video]");
    var previewOff = panel.querySelector("[data-preview-off]");
    var list = panel.querySelector("[data-device-list]");
    var cameraBadge = panel.querySelector('[data-device="camera"]');
    var microphoneBadge = panel.querySelector('[data-device="microphone"]');
    var status = panel.querySelector("[data-device-status]");
    var errorBox = panel.querySelector("[data-device-error]");
    errorBox.dataset.prefix = "device-error";
    var stream = null;

    function setState(state) {
      panel.setAttribute("data-device-state", state);
    }

    function say(text, tone) {
      status.className = "feedback feedback--" + tone;
      status.textContent = text;
      status.hidden = false;
    }

    function release() {
      stopStream(stream);
      stream = null;
      video.srcObject = null;
    }

    function run() {
      release();
      errorBox.hidden = true;
      setState("checking");
      panel.setAttribute("aria-busy", "true");
      testButton.disabled = true;
      say("Testando câmera e microfone… o navegador pode pedir permissão agora.", "muted");
      acquire().then(function (result) {
        panel.removeAttribute("aria-busy");
        testButton.disabled = false;
        testButton.textContent = "Testar de novo";
        stream = result.stream;
        list.hidden = false;
        setBadge(cameraBadge, "camera", result.camera);
        setBadge(microphoneBadge, "microphone", result.microphone);
        preview.hidden = false;
        if (stream && result.camera === "ready") {
          video.srcObject = stream;
          previewOff.hidden = true;
        } else {
          previewOff.hidden = false;
        }
        if (result.camera === "ready" && result.microphone === "ready") {
          setState("ready");
          say("Câmera e microfone prontos. Você pode entrar na sala.", "success");
          stopButton.hidden = false;
          status.focus();
          return;
        }
        if (stream) {
          setState("partial");
          say("Parte dos dispositivos está pronta. Veja abaixo o que falta; você ainda pode entrar na sala.", "muted");
          stopButton.hidden = false;
        } else {
          setState(result.reason);
          status.hidden = true;
          stopButton.hidden = true;
        }
        fillFailure(errorBox, result.reason);
        errorBox.focus();
      });
    }

    function stop() {
      release();
      preview.hidden = true;
      stopButton.hidden = true;
      errorBox.hidden = true;
      setBadge(cameraBadge, "camera", "idle");
      setBadge(microphoneBadge, "microphone", "idle");
      setState("idle");
      testButton.textContent = "Testar câmera e microfone";
      say("Teste encerrado. Câmera e microfone foram desligados.", "muted");
      status.focus();
    }

    testButton.addEventListener("click", run);
    stopButton.addEventListener("click", stop);
    root.querySelectorAll("[data-join-form]").forEach(function (form) {
      form.addEventListener("submit", release);
    });
    window.addEventListener("pagehide", release);
  }

  /* ---------------------------------------------------------------- */
  /* Room: local media, toggles, status polling, recovery.             */
  /* ---------------------------------------------------------------- */
  function room() {
    var panel = root.querySelector("#room-panel");
    if (!panel) {
      return;
    }
    var statusUrl = panel.getAttribute("data-status-url");
    var statusForm = panel.querySelector("[data-status-form]");
    var self = panel.querySelector("[data-self]");
    var video = panel.querySelector("[data-self-video]");
    var selfOff = panel.querySelector("[data-self-off]");
    var remoteText = panel.querySelector("[data-remote-text]");
    var micButton = panel.querySelector('[data-toggle="microphone"]');
    var camButton = panel.querySelector('[data-toggle="camera"]');
    var leave = panel.querySelector("[data-leave]");
    var connection = root.querySelector("[data-connection-status]");
    var errorBox = root.querySelector("[data-room-error]");
    errorBox.dataset.prefix = "room-error";
    var actions = {
      retryMedia: errorBox.querySelector("[data-retry-media]"),
      reconnect: errorBox.querySelector("[data-reconnect]"),
      back: errorBox.querySelector("[data-back]"),
      portal: errorBox.querySelector("[data-portal]"),
    };
    var refresh = root.querySelector("[data-refresh-status]");
    var stateBadge = root.querySelector("[data-state-badge]");
    var physician = root.querySelector("[data-physician]");
    var stream = null;
    var timer = null;
    var terminal = false;
    /* Bumped on every release so an acquisition still pending when the room
       ends, expires or is left cannot install its stream afterwards. */
    var mediaToken = 0;
    /* The unresolved media failure, kept while a connection failure occupies
       the alert so the device guidance returns once the connection does. */
    var mediaFailure = "";

    function setConnection(state) {
      panel.setAttribute("data-connection", state);
    }

    function setMedia(state) {
      panel.setAttribute("data-media", state);
    }

    function say(text, tone) {
      connection.className = "feedback" + (tone ? " feedback--" + tone : "");
      connection.textContent = text;
    }

    /* A connection failure replaces the live status line (the alert already
       announces it); a media failure sits beside it, since the call goes on. */
    function showFailure(key, visible) {
      fillFailure(errorBox, key);
      Object.keys(actions).forEach(function (name) {
        actions[name].hidden = visible.indexOf(name) === -1;
      });
      connection.hidden = Boolean((FAILURES[key] || {}).connection);
      errorBox.focus();
    }

    function hideFailure() {
      errorBox.hidden = true;
      errorBox.removeAttribute("data-failure");
      connection.hidden = false;
    }

    function showMediaFailure(key) {
      mediaFailure = key;
      if (!(FAILURES[errorBox.getAttribute("data-failure")] || {}).connection) {
        showFailure(key, ["retryMedia"]);
      }
    }

    function restoreMediaFailure() {
      if (mediaFailure) {
        showFailure(mediaFailure, ["retryMedia"]);
      } else {
        hideFailure();
      }
    }

    function stopTimer() {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    }

    /* Terminal states: no more polling, no media, no further checks. */
    function finish(state) {
      terminal = true;
      stopTimer();
      releaseMedia();
      setMedia("off");
      self.hidden = true;
      refresh.disabled = true;
      setConnection(state);
    }

    function releaseMedia() {
      mediaToken += 1;
      stopStream(stream);
      stream = null;
      video.srcObject = null;
      setToggle(micButton, "microphone", false, false);
      setToggle(camButton, "camera", false, false);
      selfOff.hidden = false;
    }

    function tracks(kind) {
      if (!stream) {
        return [];
      }
      return kind === "microphone" ? stream.getAudioTracks() : stream.getVideoTracks();
    }

    function setToggle(button, kind, on, available) {
      var noun = kind === "microphone" ? "Microfone" : "Câmera";
      button.disabled = !available;
      button.setAttribute("aria-pressed", available && on ? "true" : "false");
      button.textContent = !available ? noun + " indisponível" : on ? noun + " ligad" + (kind === "microphone" ? "o" : "a") : noun + " desligad" + (kind === "microphone" ? "o" : "a");
      tracks(kind).forEach(function (track) {
        track.enabled = available && on;
      });
      if (kind === "camera") {
        selfOff.hidden = available && on;
        selfOff.textContent = available ? "Câmera desligada" : "Sem câmera";
      }
    }

    function watchTracks() {
      tracks("microphone").concat(tracks("camera")).forEach(function (track) {
        track.addEventListener("ended", function () {
          if (terminal) {
            return;
          }
          setMedia("lost");
          releaseMedia();
          showMediaFailure("lost");
        });
      });
    }

    function startMedia() {
      releaseMedia();
      var token = mediaToken;
      mediaFailure = "";
      hideFailure();
      setMedia("pending");
      self.hidden = false;
      say("Preparando câmera e microfone…");
      return acquire().then(function (result) {
        if (token !== mediaToken) {
          stopStream(result.stream);
          return;
        }
        stream = result.stream;
        if (stream && result.camera === "ready") {
          video.srcObject = stream;
        }
        setToggle(micButton, "microphone", true, result.microphone === "ready");
        setToggle(camButton, "camera", true, result.camera === "ready");
        watchTracks();
        if (result.camera === "ready" && result.microphone === "ready") {
          setMedia("ready");
        } else if (stream) {
          setMedia("partial");
          showMediaFailure(result.reason);
        } else {
          setMedia(result.reason);
          showMediaFailure(result.reason);
        }
      });
    }

    function applyState(status) {
      panel.setAttribute("data-state", status.state);
      stateBadge.className = "badge " + (status.state === "active" ? "badge--success" : status.state === "waiting" ? "badge--pending" : status.state === "failed" ? "badge--error" : "badge--muted");
      stateBadge.textContent = status.label;
      physician.textContent = status.physician_joined ? "Já está na sala" : "Ainda não entrou";
      if (status.state === "ended" || status.state === "failed") {
        finish(status.state);
        physician.closest(".tele-fact").hidden = true;
        remoteText.textContent = status.state === "ended" ? "Consulta encerrada" : "Consulta indisponível";
        say(status.state === "ended" ? "A consulta foi encerrada." : "A consulta não pôde continuar." + (status.reason ? " " + status.reason : ""), "muted");
        showFailure(status.state, ["back"]);
        return;
      }
      setConnection("connected");
      if (status.state === "active") {
        remoteText.textContent = "Consulta em andamento";
        say("Conectado. Consulta em andamento.", "success");
      } else {
        remoteText.textContent = status.physician_joined ? "O médico já está na sala" : "Aguardando o médico entrar";
        say(status.physician_joined ? "Conectado. O médico já está na sala; a consulta começa em instantes." : "Conectado. Aguardando o médico entrar. Mantenha esta página aberta.");
      }
    }

    function poll() {
      if (!navigator.onLine) {
        return Promise.reject(new TypeError("offline"));
      }
      return fetch(statusUrl, {
        method: "POST",
        body: new FormData(statusForm),
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      }).then(function (response) {
        if (response.status === 403) {
          return { expired: true };
        }
        if (!response.ok) {
          throw new Error("status " + response.status);
        }
        return response.json();
      });
    }

    function schedule() {
      stopTimer();
      if (!terminal) {
        timer = window.setTimeout(connect, POLL_MS);
      }
    }

    function connect() {
      if (terminal) {
        return;
      }
      stopTimer();
      if (panel.getAttribute("data-connection") !== "connected") {
        setConnection("joining");
        say("Conectando à sala…");
      }
      poll().then(
        function (status) {
          // A reply already in flight cannot undo an ended/expired/left room.
          if (terminal) {
            return;
          }
          if (status.expired) {
            finish("expired");
            remoteText.textContent = "Acesso expirado";
            say("Sua sessão de acesso expirou.", "error");
            showFailure("expired", ["portal"]);
            return;
          }
          if (errorBox.getAttribute("data-failure") === "offline") {
            restoreMediaFailure();
          }
          applyState(status);
          schedule();
        },
        function () {
          if (terminal) {
            return;
          }
          setConnection("offline");
          say("Conexão perdida. Nada foi gravado.", "error");
          showFailure("offline", ["reconnect"]);
        }
      );
    }

    micButton.addEventListener("click", function () {
      setToggle(micButton, "microphone", micButton.getAttribute("aria-pressed") !== "true", true);
    });
    camButton.addEventListener("click", function () {
      setToggle(camButton, "camera", camButton.getAttribute("aria-pressed") !== "true", true);
    });
    actions.retryMedia.addEventListener("click", startMedia);
    actions.reconnect.addEventListener("click", connect);
    refresh.addEventListener("click", connect);
    leave.addEventListener("click", function () {
      finish("left");
    });
    window.addEventListener("offline", function () {
      if (terminal) {
        return;
      }
      stopTimer();
      setConnection("offline");
      say("Conexão perdida. Nada foi gravado.", "error");
      showFailure("offline", ["reconnect"]);
    });
    window.addEventListener("online", function () {
      if (panel.getAttribute("data-connection") === "offline") {
        connect();
      }
    });
    window.addEventListener("pagehide", function () {
      stopTimer();
      releaseMedia();
    });

    /* The room follows the session from the start: an unanswered camera or
       microphone prompt (getUserMedia can stay pending indefinitely) must not
       hold back the connection, the session state or an expiry notice. */
    setConnection("requesting");
    startMedia();
    connect();
  }

  if (root.getAttribute("data-teleconsult") === "room") {
    room();
  } else {
    waitingRoom();
  }
})();

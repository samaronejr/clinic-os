/* Clinician workspace: the patient's video beside the encounter notes.

   Scope: only the page carrying `data-teleconsult="clinician"`. Three jobs:
   a panel switch below 64rem so a phone shows one accessible panel at a
   time without unmounting the other; unsaved-note tracking around the
   shell's htmx saves (the server decides what was stored, this only says
   so); and the physician's local media plus the session state. The state is
   refetched on realtime hints (`rt:teleconsult`, and `rt:poll` from
   realtime.js's fallback), never on a timer of its own; a dropped connection
   resumes with the server's stored room state. No provider SDK, no
   recording, no storage, no history state. Every stream is stopped on
   leave, on a terminal session state and when the page hides. */
(function () {
  "use strict";

  var root = document.querySelector('[data-teleconsult="clinician"]');
  if (!root || !window.fetch) {
    return;
  }

  var WIDE = window.matchMedia("(min-width: 64rem)");
  var SWAPPABLE = { 400: true, 409: true, 503: true };
  var LABELS = {
    waiting: "Aguardando",
    active: "Em andamento",
    ended: "Encerrada",
    failed: "Falhou",
  };
  var FAILURES = {
    unsupported: {
      title: "Este navegador não permite vídeo",
      text: "O navegador não oferece acesso à câmera e ao microfone.",
      help: "Abra a sala em Chrome, Firefox, Safari ou Edge atualizado. As anotações continuam disponíveis aqui.",
    },
    insecure: {
      title: "Endereço sem segurança",
      text: "A câmera e o microfone só funcionam em um endereço seguro (https).",
      help: "Abra o endereço seguro da clínica. As anotações continuam disponíveis aqui.",
    },
    denied: {
      title: "Permissão negada",
      text: "O navegador bloqueou a câmera ou o microfone.",
      help: "Clique no ícone de cadeado ou de câmera ao lado do endereço, permita câmera e microfone e tente de novo.",
    },
    missing: {
      title: "Nenhum dispositivo encontrado",
      text: "Não encontramos câmera nem microfone neste computador.",
      help: "Conecte um dispositivo e tente de novo. Se não for possível, continue por telefone; as anotações seguem aqui.",
    },
    busy: {
      title: "Dispositivo em uso",
      text: "A câmera ou o microfone está sendo usado por outro aplicativo.",
      help: "Feche outros aplicativos de vídeo ou abas com chamadas e tente de novo.",
    },
    error: {
      title: "Não foi possível ligar câmera e microfone",
      text: "Ocorreu um erro inesperado ao acessar os dispositivos.",
      help: "Tente de novo. Se persistir, continue por telefone; as anotações seguem aqui.",
    },
    lost: {
      title: "Câmera ou microfone desconectado",
      text: "Um dispositivo parou de responder durante a consulta.",
      help: "Verifique a conexão do dispositivo e tente ligá-lo de novo. A sala e as anotações continuam.",
    },
    offline: {
      tone: "error",
      connection: true,
      title: "Conexão perdida",
      text: "A internet caiu ou está instável. Nada foi gravado.",
      help: "Verifique a rede e toque em Reconectar. As anotações continuam nesta página; salve quando a conexão voltar.",
    },
    expired: {
      tone: "error",
      connection: true,
      title: "Sua sessão de acesso expirou",
      text: "Por segurança, o acesso à clínica tem duração limitada.",
      help: "Copie as alterações não salvas antes de sair, entre de novo e reabra a sala pela lista de sessões.",
    },
    ended: {
      tone: "muted",
      connection: true,
      title: "Vídeo encerrado",
      text: "A sessão de vídeo foi encerrada e não reabre.",
      help: "O atendimento continua aberto e nada foi finalizado por isso. Salve ou finalize as anotações normalmente.",
    },
    failed: {
      tone: "error",
      connection: true,
      title: "Vídeo indisponível",
      text: "A sessão de vídeo não pôde continuar.",
      help: "O atendimento continua aberto e nada foi finalizado por isso. Salve ou finalize as anotações normalmente.",
    },
    left: {
      tone: "error",
      connection: true,
      title: "Você não está mais na sala",
      text: "Seu acesso a esta sala terminou.",
      help: "Volte à lista de sessões e entre de novo. As anotações continuam nesta página.",
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

  /* ---------------------------------------------------------------- */
  /* Panels: one at a time below 64rem, both from 64rem.               */
  /* ---------------------------------------------------------------- */
  var switcher = root.querySelector("[data-switch]");
  var tabs = Array.prototype.slice.call(switcher.querySelectorAll("[data-tab]"));
  var unsavedBadge = switcher.querySelector("[data-tab-unsaved]");

  function panelFor(name) {
    return root.querySelector('[data-panel="' + name + '"]');
  }

  function applyRoles(narrow) {
    tabs.forEach(function (tab) {
      var panel = panelFor(tab.getAttribute("data-tab"));
      if (!panel) {
        return;
      }
      if (narrow) {
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", tab.id);
      } else {
        panel.removeAttribute("role");
        panel.setAttribute("aria-labelledby", panel.getAttribute("data-title"));
      }
    });
  }

  function showPanel(name, focus) {
    root.setAttribute("data-active", name);
    tabs.forEach(function (tab) {
      var selected = tab.getAttribute("data-tab") === name;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) {
        tab.focus();
      }
    });
  }

  function layout() {
    if (WIDE.matches) {
      switcher.hidden = true;
      root.removeAttribute("data-active");
      applyRoles(false);
      return;
    }
    switcher.hidden = false;
    applyRoles(true);
    showPanel(root.getAttribute("data-active") || "video", false);
  }

  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () {
      showPanel(tab.getAttribute("data-tab"), false);
    });
    tab.addEventListener("keydown", function (event) {
      var next = null;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        next = (index + 1) % tabs.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        next = (index - 1 + tabs.length) % tabs.length;
      } else if (event.key === "Home") {
        next = 0;
      } else if (event.key === "End") {
        next = tabs.length - 1;
      }
      if (next === null) {
        return;
      }
      event.preventDefault();
      showPanel(tabs[next].getAttribute("data-tab"), true);
    });
  });
  WIDE.addEventListener("change", layout);

  /* ---------------------------------------------------------------- */
  /* Notes: explicit saves; the server says what was stored.           */
  /* ---------------------------------------------------------------- */
  var unsaved = false;
  var leaving = false;
  var pendingLeave = null;
  var guard = root.querySelector("[data-leave-guard]");
  /* Every keystroke in the notes advances the generation; a save remembers
     the generation it serialized, so a response can tell whether the fields
     changed while it was in flight. */
  var editGeneration = 0;
  var pendingSave = null;
  var lateEdits = null;
  var UNSAVED_TEXT = "Alterações não salvas.";
  var SAVING_TEXT = "Salvando… Aguarde a confirmação. As alterações ainda não estão confirmadas.";
  var LATE_TEXT = "O texto mudou durante o salvamento; as alterações atuais não foram salvas. Salve de novo.";
  var OFFLINE_TEXT = "Sem conexão com a clínica. Suas alterações não foram salvas e continuam nesta página; tente de novo quando a conexão voltar.";
  var REFUSED_TEXT = "A clínica recusou esta gravação. Suas alterações não foram salvas; copie o texto e reabra a sala pela lista de sessões.";

  function saveLine() {
    return root.querySelector("#save-state");
  }

  function hideOutcome() {
    /* The last action's outcome no longer describes the field contents. */
    root.querySelectorAll("[data-notes-message]").forEach(function (message) {
      message.hidden = true;
    });
  }

  function snapshotEdits() {
    var form = root.querySelector("#soap-form");
    if (!form) {
      return null;
    }
    var values = {};
    form.querySelectorAll("textarea[name]").forEach(function (field) {
      values[field.name] = field.value;
    });
    return values;
  }

  /* The server's panel shows what was stored; edits typed after the request
     was serialized are put back on top of it and stay marked unsaved. */
  function restoreLateEdits() {
    var values = lateEdits;
    lateEdits = null;
    var form = root.querySelector("#soap-form");
    if (!values || !form) {
      return;
    }
    Object.keys(values).forEach(function (name) {
      var field = form.elements[name];
      if (field) {
        field.value = values[name];
      }
    });
    setUnsaved(true);
    setLine("unsaved", LATE_TEXT, "error");
    saveLine().setAttribute("data-late-edits", "true");
    hideOutcome();
  }

  function setLine(state, text, tone) {
    var line = saveLine();
    if (!line) {
      return;
    }
    line.setAttribute("data-state", state);
    line.className = "feedback" + (tone ? " feedback--" + tone : "");
    line.textContent = text;
  }

  function setFinalizeGate() {
    var finalize = root.querySelector("[data-finalize]");
    if (finalize) {
      finalize.disabled = unsaved;
    }
    root.querySelectorAll("[data-finalize-hint]").forEach(function (hint) {
      hint.hidden = (hint.getAttribute("data-finalize-hint") === "unsaved") !== unsaved;
    });
  }

  function setUnsaved(flag) {
    unsaved = flag;
    unsavedBadge.hidden = !flag;
    setFinalizeGate();
  }

  function syncNotes() {
    var line = saveLine();
    setUnsaved(Boolean(line && line.getAttribute("data-state") === "unsaved"));
  }

  /* htmx settles the class attribute of same-id elements shortly after a
     swap, reverting it to the server's; the tone of a line marked unsaved
     at swap time is re-applied once that has happened. */
  function settleLine() {
    var line = saveLine();
    if (line && line.getAttribute("data-state") === "unsaved") {
      line.className = "feedback feedback--error";
    }
  }

  root.addEventListener("input", function (event) {
    if (event.target.closest("#soap-form")) {
      editGeneration += 1;
      setUnsaved(true);
      setLine("unsaved", UNSAVED_TEXT, "error");
      hideOutcome();
    }
  });

  root.addEventListener("htmx:beforeRequest", function (event) {
    if (event.detail.elt.id === "soap-form") {
      /* The body was serialized synchronously just before this event. */
      pendingSave = { xhr: event.detail.xhr, generation: editGeneration };
      lateEdits = null;
      setLine("saving", SAVING_TEXT, "");
    }
  });

  /* Conflict and failure responses carry the retained edits; swap them. */
  root.addEventListener("htmx:beforeSwap", function (event) {
    if (SWAPPABLE[event.detail.xhr.status]) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
    if (pendingSave && event.detail.xhr === pendingSave.xhr) {
      /* Fields typed into after serialization must survive the coming swap. */
      lateEdits =
        event.detail.shouldSwap && pendingSave.generation !== editGeneration ? snapshotEdits() : null;
      pendingSave = null;
    }
  });

  root.addEventListener("htmx:afterRequest", function (event) {
    if (pendingSave && event.detail.xhr === pendingSave.xhr) {
      pendingSave = null;
    }
  });

  function noteFailure(event, text) {
    if (event.detail.elt.id !== "soap-form") {
      return;
    }
    setUnsaved(true);
    setLine("unsaved", text, "error");
    saveLine().focus();
  }

  root.addEventListener("htmx:sendError", function (event) {
    noteFailure(event, OFFLINE_TEXT);
  });
  root.addEventListener("htmx:responseError", function (event) {
    noteFailure(event, REFUSED_TEXT);
  });

  /* Leaving (a link) or opening the record (a form) with unsaved text asks
     first; the chosen exit waits until the physician decides. */
  function askLeave(proceed) {
    pendingLeave = proceed;
    guard.hidden = false;
    guard.focus();
  }

  root.addEventListener("click", function (event) {
    var link = event.target.closest("[data-leave]");
    if (!link) {
      return;
    }
    if (!unsaved) {
      leaving = true;
      return;
    }
    event.preventDefault();
    askLeave(function () {
      window.location.assign(link.href);
    });
  });
  root.addEventListener("submit", function (event) {
    var form = event.target.closest("[data-leave-form]");
    if (!form) {
      return;
    }
    if (!unsaved || leaving) {
      leaving = true;
      return;
    }
    event.preventDefault();
    /* The confirmed exit resubmits through the same button, so the action it
       names travels with the form; a plain form.submit() would drop it. */
    var submitter = event.submitter;
    askLeave(function () {
      form.requestSubmit(submitter || undefined);
    });
  });
  guard.querySelector("[data-leave-stay]").addEventListener("click", function () {
    pendingLeave = null;
    guard.hidden = true;
    var field = root.querySelector("#soap-form textarea");
    if (field) {
      field.focus();
    }
  });
  guard.querySelector("[data-leave-discard]").addEventListener("click", function () {
    var proceed = pendingLeave;
    pendingLeave = null;
    guard.hidden = true;
    if (proceed) {
      leaving = true;
      proceed();
    }
  });
  window.addEventListener("beforeunload", function (event) {
    if (unsaved && !leaving) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  /* ---------------------------------------------------------------- */
  /* Video: local media, session state, polling and recovery.          */
  /* ---------------------------------------------------------------- */
  var video = panelFor("video");
  var statusUrl = video.getAttribute("data-status-url");
  var statusForm = video.querySelector("[data-status-form]");
  var self = video.querySelector("[data-self]");
  var selfVideo = video.querySelector("[data-self-video]");
  var selfOff = video.querySelector("[data-self-off]");
  var remoteText = video.querySelector("[data-remote-text]");
  var micButton = video.querySelector('[data-toggle="microphone"]');
  var camButton = video.querySelector('[data-toggle="camera"]');
  var connection = video.querySelector("[data-connection-status]");
  var errorBox = video.querySelector("[data-room-error]");
  var retryMedia = errorBox.querySelector("[data-retry-media]");
  var reconnect = errorBox.querySelector("[data-reconnect]");
  var refresh = video.querySelector("[data-refresh-status]");
  var badge = root.querySelector("[data-session-badge]");
  var audioButton = video.querySelector("[data-audio-only]");
  var devicePanel = video.querySelector("[data-device-check]");
  var stream = null;
  var terminal = false;
  /* Only the newest status reply may paint the room. */
  var statusSeq = 0;
  var audioOnly = audioButton.getAttribute("aria-pressed") === "true";
  var mediaToken = 0;
  var mediaFailure = "";

  function setConnection(state) {
    video.setAttribute("data-connection", state);
  }

  function setMedia(state) {
    video.setAttribute("data-media", state);
  }

  function say(text, tone) {
    connection.className = "feedback" + (tone ? " feedback--" + tone : "");
    connection.textContent = text;
  }

  function showFailure(key, visible, detail) {
    var failure = FAILURES[key] || FAILURES.error;
    errorBox.className = "feedback feedback--" + (failure.tone || "error");
    errorBox.querySelector("[data-room-error-title]").textContent = failure.title;
    errorBox.querySelector("[data-room-error-text]").textContent = failure.text + (detail ? " " + detail : "");
    errorBox.querySelector("[data-room-error-help]").textContent = failure.help;
    errorBox.setAttribute("data-failure", key);
    errorBox.hidden = false;
    retryMedia.hidden = visible.indexOf("retryMedia") === -1;
    reconnect.hidden = visible.indexOf("reconnect") === -1;
    connection.hidden = Boolean(failure.connection);
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

  function tracks(kind) {
    if (!stream) {
      return [];
    }
    return kind === "microphone" ? stream.getAudioTracks() : stream.getVideoTracks();
  }

  function setToggle(button, kind, on, available) {
    var noun = kind === "microphone" ? "Microfone" : "Câmera";
    var suffix = kind === "microphone" ? "o" : "a";
    button.disabled = !available;
    button.setAttribute("aria-pressed", available && on ? "true" : "false");
    button.textContent = !available ? noun + " indisponível" : on ? noun + " ligad" + suffix : noun + " desligad" + suffix;
    tracks(kind).forEach(function (track) {
      track.enabled = available && on && !(kind === "camera" && audioOnly);
    });
    if (kind === "camera") {
      selfOff.hidden = available && on && !audioOnly;
      selfOff.textContent = audioOnly ? "Somente áudio" : available ? "Câmera desligada" : "Sem câmera";
    }
  }

  /* Audio only: the camera track stops sending; the server records the mode
     so the patient sees it and a reconnect restores it. */
  function paintAudioOnly(enabled) {
    audioOnly = enabled;
    video.setAttribute("data-media-mode", enabled ? "audio" : "video");
    audioButton.setAttribute("aria-pressed", enabled ? "true" : "false");
    audioButton.textContent = audioButton.getAttribute(enabled ? "data-text-on" : "data-text-off");
    setToggle(camButton, "camera", !enabled, tracks("camera").length > 0 && !enabled);
  }

  function postAction(action, extra) {
    var body = new FormData(statusForm);
    body.set("action", action);
    Object.keys(extra || {}).forEach(function (name) {
      body.set(name, extra[name]);
    });
    return fetch(statusUrl, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  }

  function requestAudioOnly(enabled, automatic) {
    if (terminal) {
      return Promise.resolve();
    }
    audioButton.disabled = true;
    audioButton.setAttribute("aria-busy", "true");
    return postAction("audio_only", { enabled: enabled ? "true" : "false" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("audio_only " + response.status);
        }
        return response.json();
      })
      .then(
        function (value) {
          paintAudioOnly(Boolean(value.audio_only));
          if (automatic) {
            say(audioButton.getAttribute("data-text-auto"), "muted");
          }
        },
        function () {
          say(audioButton.getAttribute("data-text-failed"), "error");
        }
      )
      .then(function () {
        audioButton.removeAttribute("aria-busy");
        audioButton.disabled = terminal;
      });
  }

  function releaseMedia() {
    mediaToken += 1;
    stopStream(stream);
    stream = null;
    selfVideo.srcObject = null;
    setToggle(micButton, "microphone", false, false);
    setToggle(camButton, "camera", false, false);
    selfOff.hidden = false;
  }

  function finish(state) {
    terminal = true;
    releaseMedia();
    audioButton.disabled = true;
    setMedia("off");
    self.hidden = true;
    refresh.disabled = true;
    setConnection(state);
    var actions = root.querySelector("[data-session-actions]");
    if (actions) {
      actions.hidden = true;
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
        selfVideo.srcObject = stream;
      }
      setToggle(micButton, "microphone", true, result.microphone === "ready");
      setToggle(camButton, "camera", !audioOnly, result.camera === "ready" && !audioOnly);
      watchTracks();
      if (devicePanel) {
        devicePanel.dispatchEvent(
          new CustomEvent("tc:devices", { detail: { camera: result.camera, microphone: result.microphone } })
        );
      }
      if (result.camera === "ready" && result.microphone === "ready") {
        setMedia("ready");
      } else if (stream) {
        setMedia("partial");
        showMediaFailure(result.reason);
        /* Camera gone but microphone working: continue with audio only. */
        if (result.microphone === "ready" && !audioOnly) {
          requestAudioOnly(true, true);
        }
      } else {
        setMedia(result.reason);
        showMediaFailure(result.reason);
      }
    });
  }

  function paintPatient(status) {
    var remove = root.querySelector("[data-remove-form]");
    if (remove) {
      remove.hidden = !(status.patient_joined && !status.patient_removed);
    }
    var media = root.querySelector("[data-patient-media]");
    if (media && typeof status.patient_audio_only === "boolean") {
      media.textContent = media.getAttribute(status.patient_audio_only ? "data-text-audio" : "data-text-video");
    }
    var devices = root.querySelector("[data-patient-devices]");
    if (devices && Array.isArray(status.patient_devices) && status.patient_devices.length) {
      var list = document.createElement("ul");
      list.className = "tele-device-summary";
      list.setAttribute("data-device-summary", "");
      status.patient_devices.forEach(function (item) {
        var line = document.createElement("li");
        line.setAttribute("data-summary", item.field);
        line.setAttribute("data-summary-state", item.code);
        line.textContent = item.label + ": " + item.value;
        list.appendChild(line);
      });
      devices.replaceChildren(list);
    }
  }

  function applyState(state, patientJoined, reason, removed) {
    video.setAttribute("data-state", state);
    badge.className = "badge " + (state === "active" ? "badge--success" : state === "waiting" ? "badge--pending" : state === "failed" ? "badge--error" : "badge--muted");
    badge.textContent = "Sessão: " + (LABELS[state] || state);
    var presence = root.querySelector("[data-patient-presence]");
    if (state === "ended" || state === "failed") {
      finish(state);
      if (presence) {
        presence.textContent = "—";
      }
      remoteText.textContent = state === "ended" ? "Vídeo encerrado" : "Vídeo indisponível";
      say(
        (state === "ended" ? "O vídeo foi encerrado." : "O vídeo não pôde continuar." + (reason ? " " + reason : "")) +
          " O atendimento continua aberto e nada foi finalizado; as anotações seguem disponíveis.",
        "muted"
      );
      /* A server-rendered notice (after start/end) already explains the
         state; the local alert covers a change discovered by polling. */
      var notice = root.querySelector("[data-session-notice]");
      if (notice) {
        hideFailure();
        connection.hidden = true;
        notice.focus();
      } else {
        showFailure(state, [], reason);
      }
      return;
    }
    setConnection("connected");
    if (presence) {
      presence.textContent = removed ? "Retirado da sala" : patientJoined ? "Na sala" : "Ainda não entrou";
    }
    if (state === "active") {
      remoteText.textContent = "Consulta em andamento";
      say("Conectado. Consulta em andamento.", "success");
    } else {
      remoteText.textContent = patientJoined ? "O paciente está na sala" : "Aguardando o paciente entrar";
      say(patientJoined ? "Conectado. O paciente já está na sala; inicie a consulta quando estiver pronto." : "Conectado. Aguardando o paciente entrar.");
    }
  }

  /* After a server swap of the session facts, read the stored state. */
  function syncSession() {
    var facts = root.querySelector("#video-session");
    if (!facts || terminal) {
      return;
    }
    applyState(
      facts.getAttribute("data-state"),
      facts.getAttribute("data-patient-joined") === "true",
      facts.getAttribute("data-failure") || "",
      facts.getAttribute("data-patient-removed") === "true"
    );
  }

  function poll() {
    if (!navigator.onLine) {
      return Promise.reject(new TypeError("offline"));
    }
    var body = new FormData(statusForm);
    body.set("action", "status");
    return fetch(statusUrl, {
      method: "POST",
      body: body,
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

  function connect() {
    if (terminal) {
      return;
    }
    var mine = ++statusSeq;
    if (video.getAttribute("data-connection") !== "connected") {
      setConnection("joining");
      say("Conectando à sala…");
    }
    poll().then(
      function (status) {
        // A reply already in flight cannot undo an ended/expired room, and an
        // older reply never paints over a newer one.
        if (terminal || mine !== statusSeq) {
          return;
        }
        if (status.expired) {
          finish("expired");
          remoteText.textContent = "Acesso expirado";
          say("Sua sessão de acesso expirou.", "error");
          showFailure("expired", []);
          return;
        }
        if (errorBox.getAttribute("data-failure") === "offline") {
          restoreMediaFailure();
        }
        if (typeof status.physician_audio_only === "boolean" && status.physician_audio_only !== audioOnly) {
          paintAudioOnly(status.physician_audio_only);
        }
        paintPatient(status);
        applyState(status.state, status.patient_joined, status.reason, status.patient_removed);
      },
      function () {
        if (terminal || mine !== statusSeq) {
          return;
        }
        setConnection("offline");
        say("Conexão perdida. Nada foi gravado; as anotações continuam nesta página.", "error");
        showFailure("offline", ["reconnect"]);
      }
    );
  }

  /* Reconnect with state recovery: the server re-validates the physician,
     renews an expired room credential and returns the stored media modes;
     the notes stay untouched on this page. */
  function resume() {
    if (terminal) {
      return;
    }
    if (!navigator.onLine) {
      connect();
      return;
    }
    statusSeq += 1;
    setConnection("reconnecting");
    say("Reconectando à sala…");
    postAction("resume").then(
      function (response) {
        if (terminal) {
          return null;
        }
        if (response.status === 409) {
          return response.json().then(function (value) {
            if (value.code === "not_in_room") {
              finish("left");
              remoteText.textContent = "Fora da sala";
              showFailure("left", []);
            } else {
              connect();
            }
          });
        }
        if (!response.ok) {
          return connect();
        }
        return response.json().then(function (recovery) {
          if (!terminal && recovery.audio_only !== audioOnly) {
            paintAudioOnly(recovery.audio_only);
          }
          connect();
        });
      },
      function () {
        if (terminal) {
          return;
        }
        setConnection("offline");
        say("Conexão perdida. Nada foi gravado; as anotações continuam nesta página.", "error");
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
  retryMedia.addEventListener("click", startMedia);
  reconnect.addEventListener("click", resume);
  refresh.addEventListener("click", connect);
  refresh.hidden = false;
  audioButton.addEventListener("click", function () {
    requestAudioOnly(!audioOnly, false);
  });
  document.body.addEventListener("rt:teleconsult", connect);
  document.body.addEventListener("rt:poll", connect);
  window.addEventListener("offline", function () {
    if (terminal) {
      return;
    }
    statusSeq += 1;
    setConnection("offline");
    say("Conexão perdida. Nada foi gravado; as anotações continuam nesta página.", "error");
    showFailure("offline", ["reconnect"]);
  });
  window.addEventListener("online", function () {
    if (video.getAttribute("data-connection") === "offline") {
      resume();
    }
  });
  window.addEventListener("pagehide", function () {
    releaseMedia();
  });

  root.addEventListener("htmx:afterSwap", function (event) {
    if (event.target.id === "notes-panel") {
      applyRoles(!WIDE.matches);
      syncNotes();
      restoreLateEdits();
    } else if (event.target.id === "video-session") {
      syncSession();
    }
  });
  root.addEventListener("htmx:afterSettle", function (event) {
    if (event.target.id === "notes-panel") {
      settleLine();
    }
  });

  layout();
  syncNotes();
  /* The room follows the session from the start: an unanswered camera or
     microphone prompt (getUserMedia can stay pending indefinitely) must not
     hold back the connection, the session state or the patient's presence. */
  setConnection("requesting");
  startMedia();
  connect();
})();

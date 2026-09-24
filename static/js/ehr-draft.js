"use strict";

// No autosave and no browser storage of clinical content.
const soapForm = document.getElementById("soap-form");
const saveState = document.getElementById("save-state");
if (soapForm && saveState) {
  soapForm.addEventListener("input", () => {
    saveState.dataset.state = "unsaved";
    saveState.textContent = "Alterações não salvas.";
  });
  soapForm.addEventListener("submit", () => {
    saveState.dataset.state = "saving";
    saveState.textContent = "Salvando… Aguarde a confirmação. As alterações ainda não estão confirmadas.";
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      saveState.dataset.state = "unsaved";
      saveState.textContent = "Alterações não confirmadas. Reabra a versão salva para conferir.";
    }
  });
}

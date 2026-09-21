// Comportamiento del panel de registro; los datos y formularios siguen en HTMX.
(function () {
  "use strict";
  var dialog = document.getElementById("trade-dialog");
  var opener = null;
  var busy = false;
  function feedback(message) {
    var node = document.getElementById("ui-feedback");
    if (node) { node.textContent = message; node.hidden = false; }
  }
  function error(message) {
    var node = document.getElementById("dialog-error");
    if (dialog && dialog.open && node) { node.textContent = message; node.hidden = false; }
    else { feedback(message); }
  }
  function close() {
    if (!dialog || busy) return;
    dialog.close();
  }
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-close-dialog]")) close();
    var trigger = event.target.closest('[hx-target="#trade-form"]');
    if (trigger) opener = trigger;
  });
  if (dialog) {
    dialog.addEventListener("keydown", function (event) {
      if (event.key !== "Tab") return;
      var controls = Array.from(dialog.querySelectorAll('button, input:not([type="hidden"]), select, textarea, a[href], [tabindex="0"]'))
        .filter(function (node) { return !node.disabled && node.getClientRects().length > 0; });
      var first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    dialog.addEventListener("cancel", function (event) { if (busy) event.preventDefault(); });
    dialog.addEventListener("close", function () {
      document.getElementById("trade-form").replaceChildren();
      document.getElementById("dialog-error").hidden = true;
      var target = opener && opener.isConnected ? opener : document.getElementById("opportunities-heading");
      if (target) target.focus();
    });
  }
  document.addEventListener("htmx:afterSwap", function (event) {
    if (dialog && event.detail.target.id === "trade-form") {
      document.getElementById("dialog-error").hidden = true;
      if (!dialog.open) dialog.showModal();
      var first = dialog.querySelector('input:not([type="hidden"]), select, textarea');
      if (first) first.focus();
    }
  });
  document.addEventListener("htmx:beforeRequest", function (event) {
    if (event.detail.elt.matches("form.trade-form")) {
      if (busy) { event.preventDefault(); return; }
      busy = true;
      document.getElementById("dialog-error").hidden = true;
    }
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.elt.matches("form.trade-form")) {
      busy = false;
      if (event.detail.successful) { close(); feedback("Operación guardada correctamente."); }
      else error("No se pudo guardar. Tus datos siguen aquí; revisa la conexión e inténtalo de nuevo.");
    }
  });
  document.addEventListener("htmx:responseError", function (event) {
    error(event.detail.xhr.status === 401 ? "Tu sesión ha caducado. Inicia sesión para continuar; conserva tus datos antes de salir." : "No se pudo completar la solicitud. Inténtalo de nuevo.");
  });
  document.addEventListener("htmx:sendError", function () { error("No hay conexión con el servidor. Inténtalo de nuevo."); });
})();

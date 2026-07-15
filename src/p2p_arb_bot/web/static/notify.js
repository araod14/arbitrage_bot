// Avisos del navegador cuando aparece una oportunidad nueva.
//
// El bot ya detecta y persiste; esto solo mira la tabla que HTMX repinta cada 8 s
// y avisa si hay una fila con id mayor al último visto. Todo el estado vive en
// localStorage: es una preferencia del navegador, no del bot, y por eso no toca
// el .env ni el servidor.
//
// Requiere contexto seguro (HTTPS o localhost); por IP de LAN el navegador
// bloquea la API y aquí se degrada dejando el botón deshabilitado.

(function () {
  "use strict";

  var SEEN_KEY = "arb.lastSeenOppId";
  var ON_KEY = "arb.notifyEnabled";
  var supported = "Notification" in window && window.isSecureContext;

  function lastSeen() {
    return parseInt(localStorage.getItem(SEEN_KEY) || "0", 10) || 0;
  }

  function enabled() {
    return localStorage.getItem(ON_KEY) === "1";
  }

  // Mayor id presente en la tabla. Las filas vienen ordenadas por detected_at
  // DESC, id DESC, pero no dependemos de eso: tomamos el máximo explícitamente.
  function scan() {
    var rows = document.querySelectorAll("#opportunities tr[data-opp-id]");
    var top = null;
    for (var i = 0; i < rows.length; i++) {
      var id = parseInt(rows[i].getAttribute("data-opp-id"), 10);
      if (!isNaN(id) && (top === null || id > top.id)) {
        top = { id: id, row: rows[i] };
      }
    }
    return top;
  }

  function show(top) {
    var d = top.row.dataset;
    var body = d.net + "% neto";
    if (d.amount) {
      body += " · usa " + d.amount;
    }
    var n = new Notification("Oportunidad " + d.pair, {
      body: body,
      tag: "arb-opp-" + top.id, // evita apilar avisos duplicados del mismo id
    });
    n.onclick = function () {
      window.open(d.buyUrl, "_blank", "noopener");
      n.close();
    };
  }

  // Se llama en cada swap de HTMX y una vez al cargar. En la primera carga solo
  // siembra el id: sin esto, abrir el dashboard dispararía una ráfaga de avisos
  // por oportunidades viejas ya vistas.
  function check(seed) {
    var top = scan();
    if (top === null) {
      return;
    }
    if (seed || top.id <= lastSeen()) {
      localStorage.setItem(SEEN_KEY, String(Math.max(top.id, lastSeen())));
      return;
    }
    if (enabled() && Notification.permission === "granted") {
      show(top);
    }
    localStorage.setItem(SEEN_KEY, String(top.id));
  }

  function render(btn) {
    if (!supported) {
      btn.disabled = true;
      btn.textContent = "Avisos no disponibles";
      btn.title =
        "El navegador solo permite notificaciones en HTTPS o localhost.";
      return;
    }
    if (Notification.permission === "denied") {
      btn.disabled = true;
      btn.textContent = "Avisos bloqueados";
      btn.title = "Has bloqueado las notificaciones para este sitio.";
      return;
    }
    btn.disabled = false;
    btn.textContent = enabled() ? "Avisos activados" : "Activar avisos";
    btn.classList.toggle("on", enabled());
  }

  function wire() {
    var btn = document.getElementById("notify-toggle");
    if (btn === null) {
      return; // no hay sección de oportunidades en esta página
    }
    render(btn);

    btn.addEventListener("click", function () {
      if (enabled()) {
        localStorage.setItem(ON_KEY, "0");
        render(btn);
        return;
      }
      // requestPermission necesita un gesto del usuario: por eso va aquí y no al
      // cargar la página.
      Notification.requestPermission().then(function (perm) {
        localStorage.setItem(ON_KEY, perm === "granted" ? "1" : "0");
        render(btn);
      });
    });

    check(true); // siembra sin notificar
    document.body.addEventListener("htmx:afterSwap", function (e) {
      if (e.target && e.target.id === "opportunities") {
        check(false);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();

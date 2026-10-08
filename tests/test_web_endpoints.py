"""Tests de los endpoints FastAPI del dashboard, con TestClient.

Complementan ``test_web.py`` (que solo ejercita las funciones puras de soporte):
aquí se monta la app de verdad y se piden las rutas. Sin red — TestClient habla
con la app en proceso — pero sí con disco: SQLite y plantillas reales sobre
``tmp_path``, que es donde viven los fallos que los tests de funciones no ven
(plantilla rota, ruta sin proteger, contexto que no llega al parcial).

Nada de esto arranca el bot: no se toca ``/bot/start``, que lanzaría un
subproceso real.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from p2p_arb_bot.domain.models import Opportunity
from p2p_arb_bot.infrastructure.sqlite_repo import SQLiteRepository
from p2p_arb_bot.web import env_store, trade_store
from p2p_arb_bot.web.app import create_app

PASSWORD = "secreto-de-test"


def _seed_opportunity(db_path: str) -> None:
    repo = SQLiteRepository(db_path)
    repo.save(
        Opportunity(
            detected_at=datetime.now(timezone.utc),
            fiat="VES", asset="USDT",
            buy_pay_method="PagoMovil", sell_pay_method="Banesco",
            buy_price=Decimal("36"), sell_price=Decimal("37"),
            spread_pct=Decimal("2.9"), net_pct=Decimal("2.777"),
            max_usdt=Decimal("100"),
            buy_adv_no="b1", sell_adv_no="s1",
            buy_advertiser="vend1", sell_advertiser="comp1",
            buy_url="https://p2p.binance.com/es/trade/buy/USDT?fiat=VES&payment=PagoMovil",
            sell_url="https://p2p.binance.com/es/trade/sell/USDT?fiat=VES&payment=Banesco",
            est_profit_usdt=Decimal("2.77"),
            buy_min_amount=Decimal("100"), buy_max_amount=Decimal("100000"),
            sell_min_amount=Decimal("100"), sell_max_amount=Decimal("100000"),
        )
    )
    repo.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """App aislada en tmp_path.

    ENV_PATH apunta a un .env vacío a propósito: ``create_app`` llama a
    ``load_dotenv``, que sin ruta buscaría el .env REAL del repo y colaría la
    contraseña y las rutas de producción en los tests.
    """
    (tmp_path / "empty.env").write_text("")
    monkeypatch.setenv("ENV_PATH", str(tmp_path / "empty.env"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "opps.db"))
    monkeypatch.setenv("TRADES_DB_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setenv("STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "bot.log"))
    monkeypatch.setenv("BOT_PIDFILE", str(tmp_path / "bot.pid"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("MARKET_FRESHNESS_S", "60")
    monkeypatch.setenv("REVALIDATION_COOLDOWN_S", "0")
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_SECRET", "secreto-de-firma-para-tests")
    return tmp_path


@pytest.fixture
def client(env):
    return TestClient(create_app())


@pytest.fixture
def authed(client):
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 200
    return client


# --- supervisión pública -----------------------------------------------------

@pytest.mark.parametrize(
    "route",
    ["/", "/partials/status", "/partials/opportunities", "/partials/trade-amount", "/partials/log"],
)
def test_public_routes_need_no_login(client, route):
    assert client.get(route).status_code == 200


def test_dashboard_renders_with_empty_db(client):
    # Sin DB todavía: el dashboard debe pintar, no reventar.
    r = client.get("/")
    assert r.status_code == 200
    assert "Aún no se han registrado oportunidades" in r.text


@pytest.mark.parametrize("route", ["/", "/partials/status"])
def test_resumen_publico_muestra_configuracion_con_bot_detenido(client, monkeypatch, route):
    monkeypatch.setenv("ASSETS", "USDT,BTC")
    monkeypatch.setenv("FIAT", "VES")
    monkeypatch.setenv("PAY_METHODS", "Banesco,PagoMovil")
    monkeypatch.setenv("MAX_FIAT", "12345.67")
    body = client.get(route).text
    assert "Configuración actual" in body
    assert "USDT/VES</span>" in body and "BTC/VES</span>" in body
    assert "Banesco</span>" in body and "PagoMovil</span>" in body
    assert '12345.67 <span class="config-currency">VES</span>' in body
    assert "Se evalúa completo para cada oportunidad." in body
    assert "Editar configuración" not in body


@pytest.mark.parametrize("route", ["/", "/partials/status"])
def test_resumen_sin_filtro_de_bancos_y_enlace_privado(authed, monkeypatch, route):
    monkeypatch.setenv("PAY_METHODS", "")
    body = authed.get(route).text
    assert "Todos los métodos de pago" in body
    assert 'href="/config">Editar configuración</a>' in body


def test_dashboard_explica_funcion_del_bot(client):
    body = client.get("/").text
    assert "Monitorea precios de compra y venta en Binance P2P" in body
    assert "No ejecuta operaciones." in body


# --- rutas protegidas --------------------------------------------------------

PROTECTED_GET = ["/partials/trades", "/trades/new?opp_id=1", "/trades/fail?opp_id=1", "/config", "/funds"]


@pytest.mark.parametrize("route", PROTECTED_GET)
def test_protected_get_returns_401_to_htmx(client, route):
    # A HTMX se le responde 401 para que no swapee un HTML de login dentro del panel.
    assert client.get(route, headers={"HX-Request": "true"}).status_code == 401


@pytest.mark.parametrize("route", PROTECTED_GET)
def test_protected_get_redirects_browser_to_login(client, route):
    r = client.get(route, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_post_trades_requires_login(client):
    r = client.post(
        "/trades",
        data={"status": "completed", "opportunity_id": 1},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 401


def test_trades_are_not_leaked_to_anonymous_dashboard(client, env):
    """El dashboard es público: el registro de operaciones NO puede asomar en él."""
    trade_store.save_trade(
        str(env / "trades.db"),
        {"status": "completed", "notes": "nota-privada", "buy_advertiser": "vend-privado"},
    )
    body = client.get("/").text
    assert "Mis operaciones" not in body
    assert "nota-privada" not in body
    assert "vend-privado" not in body


# --- configuración -----------------------------------------------------------

@pytest.fixture
def sin_red(monkeypatch):
    """Evita que /config sondee Binance de verdad al descubrir métodos de pago."""
    async def _vacio(asset, fiat, **_kw):
        return []

    monkeypatch.setattr(env_store, "discover_methods", _vacio)


@pytest.fixture
def env_limpio(monkeypatch):
    """Aísla las claves que write_config vuelca en os.environ.

    ``write_config`` hace ``os.environ.update`` a propósito (el bot hereda el
    entorno del dashboard), así que sin esto un test filtraría su config a los
    siguientes. monkeypatch las restaura al terminar.
    """
    for key in ("ASSETS", "MAX_FIAT", "THRESHOLD_PCT", "PAY_METHODS", "POLL_INTERVAL_S"):
        monkeypatch.setenv(key, "")


def test_config_form_lista_las_monedas(authed, sin_red):
    body = authed.get("/config").text
    assert 'name="assets"' in body
    assert "BTC" in body
    assert "Monedas a monitorear" in body


def test_config_guarda_varias_monedas(authed, env, sin_red, env_limpio):
    before = authed.get("/partials/status").text
    assert "80000" not in before
    r = authed.post(
        "/config",
        data={
            "threshold_pct": "1.5",
            "max_fiat": "80000",
            "poll_interval_s": "30",
            "assets": ["USDT", "BTC"],
        },
    )
    assert r.status_code == 200
    assert "Configuración guardada" in r.text
    lineas = (env / "empty.env").read_text(encoding="utf-8").splitlines()
    assert "ASSETS=USDT,BTC" in lineas
    assert "MAX_FIAT=80000" in lineas
    body = authed.get("/partials/status").text
    assert "80000" in body
    assert "USDT/" in body and "BTC/" in body


def test_config_sin_monedas_muestra_error(authed, env, sin_red, env_limpio):
    r = authed.post(
        "/config",
        data={"threshold_pct": "1.5", "max_fiat": "80000", "poll_interval_s": "30"},
    )
    assert r.status_code == 200
    assert "No se pudo guardar" in r.text
    assert "al menos una moneda" in r.text
    # Y no escribió nada en el .env.
    assert "ASSETS=" not in (env / "empty.env").read_text(encoding="utf-8")


# --- login -------------------------------------------------------------------

def test_login_with_wrong_password_is_rejected(client):
    r = client.post("/login", data={"password": "incorrecta"})
    assert r.status_code == 401
    assert client.get("/partials/trades", headers={"HX-Request": "true"}).status_code == 401


def test_login_grants_access(authed):
    assert authed.get("/partials/trades").status_code == 200


# --- oportunidades: contrato con notify.js -----------------------------------

def test_opportunities_expose_id_for_notifications(client, env):
    """notify.js compara data-opp-id con el último visto: sin id no hay aviso."""
    _seed_opportunity(str(env / "opps.db"))
    body = client.get("/partials/opportunities").text
    assert 'data-opp-id="1"' in body
    assert "data-buy-url=" in body
    assert 'data-pair="USDT/VES"' in body


def test_opportunities_partial_keeps_buttons_after_refresh(authed, env):
    """El parcial se repinta cada 8 s: si no recibe `authed`, los botones se caen."""
    _seed_opportunity(str(env / "opps.db"))
    body = authed.get("/partials/opportunities").text
    assert "/trades/new?opp_id=1" in body
    assert "/trades/fail?opp_id=1" in body


def test_opportunities_partial_hides_buttons_when_anonymous(client, env):
    _seed_opportunity(str(env / "opps.db"))
    body = client.get("/partials/opportunities").text
    assert "/trades/new" not in body


# --- formularios de registro -------------------------------------------------

def test_trade_form_carries_estimate_snapshot(authed, env):
    """El snapshot viaja en el form; el POST no relee la DB (puede estar bloqueada)."""
    _seed_opportunity(str(env / "opps.db"))
    body = authed.get("/trades/new?opp_id=1").text
    assert 'name="est_net_pct" value="2.777"' in body
    assert 'name="asset" value="USDT"' in body
    assert 'name="est_profit_usdt" value="2.77"' in body


def test_trade_form_survives_missing_opportunity(authed):
    """Si el usuario pulsó Limpiar entre medias, el form sale vacío, no revienta."""
    r = authed.get("/trades/new?opp_id=999")
    assert r.status_code == 200
    assert 'name="est_net_pct" value=""' in r.text


def test_fail_form_lists_reasons(authed, env):
    _seed_opportunity(str(env / "opps.db"))
    body = authed.get("/trades/fail?opp_id=1").text
    for key in trade_store.FAILURE_REASONS:
        assert f'value="{key}"' in body


# --- guardar y renderizar ----------------------------------------------------

def _post_completed(client, **over):
    data = {
        "status": "completed", "opportunity_id": 1,
        "detected_at": "2026-07-15T10:00:00+00:00", "asset": "USDT", "fiat": "VES",
        "est_net_pct": "2.777", "est_profit_usdt": "2.77",
        "real_buy_price": "36", "real_sell_price": "37", "real_usdt": "100",
        "real_fiat_buy": "3600", "real_fiat_sell": "3650",
        "buy_advertiser": "vend1", "sell_advertiser": "comp1",
    }
    data.update(over)
    return client.post("/trades", data=data)


def test_post_completed_saves_and_renders_comparison(authed):
    r = _post_completed(authed)
    assert r.status_code == 200
    # Real 1.389% frente al 2.777% estimado: el hueco es lo que se quiere ver.
    assert "1.389%" in r.text
    assert "2.777%" in r.text
    assert "-1.388" in r.text


def test_post_failed_renders_without_crashing(authed):
    """Regresión: una operación fallida no tiene PnL.

    Las claves de PnL deben existir a None; si faltan, Jinja las ve como Undefined,
    ``Undefined is not none`` da True, y la plantilla revienta con un 500 al
    formatear el delta. La suite de funciones puras no detectaba esto.
    """
    r = authed.post(
        "/trades",
        data={
            "status": "failed", "opportunity_id": 1,
            "detected_at": "2026-07-15T10:00:00+00:00", "asset": "USDT", "fiat": "VES",
            "est_net_pct": "4.0", "failure_reason": "too_late", "notes": "el anuncio voló",
        },
    )
    assert r.status_code == 200
    assert "no pude" in r.text
    assert "No llegué a tiempo / el anuncio expiró" in r.text


def test_mixed_history_reports_hit_rate(authed):
    _post_completed(authed)
    authed.post("/trades", data={"status": "failed", "opportunity_id": 1, "failure_reason": "no_funds"})
    body = authed.get("/partials/trades").text
    assert "(50%)" in body  # 1 cerrada de 2 registradas


def test_unknown_status_is_not_trusted(authed):
    """El status llega del cliente: cualquier cosa que no sea 'failed' es 'completed'."""
    r = _post_completed(authed, status="cualquier-cosa")
    assert r.status_code == 200
    assert "cerrada" in r.text


def test_profit_btc_conserva_precision_y_unidad(client, env):
    import sqlite3

    _seed_opportunity(str(env / "opps.db"))
    with sqlite3.connect(env / "opps.db") as conn:
        conn.execute("UPDATE opportunities SET asset = 'BTC', est_profit_usdt = '0.000002'")
    response = client.get("/partials/opportunities")
    assert response.status_code == 200
    assert "~0.000002 BTC" in response.text
    assert "— USD" in response.text
    assert "/ — USD" in client.get("/partials/status").text


@pytest.mark.parametrize("con_sesion", [False, True])
def test_dashboard_muestra_ganancias_usd(client, env, con_sesion):
    _seed_opportunity(str(env / "opps.db"))
    if con_sesion:
        client.post("/login", data={"password": PASSWORD})
    for path in ("/", "/partials/opportunities"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'data-label="Ganancia est. (USD)"' in response.text
        assert "~2.77 USD" in response.text
        assert response.text.index('data-label="Ganancia est."') < response.text.index('data-label="Ganancia est. (USD)"')
    for path in ("/", "/partials/status"):
        response = client.get(path)
        assert response.status_code == 200
        assert "~99.72 VES / ~2.77 USD" in response.text


def test_ganancia_usd_vacia(client):
    response = client.get("/partials/status")
    assert response.status_code == 200
    assert "/ ~0.00 USD" in response.text


@pytest.mark.parametrize("falla_primera", [False, True])
def test_config_combina_metodos_de_todas_las_monedas(authed, monkeypatch, falla_primera):
    monkeypatch.setenv("ASSETS", "USDT,BTC")
    calls = []

    async def discover(asset, fiat):
        calls.append(asset)
        if asset == "USDT":
            if falla_primera:
                raise TimeoutError("sin respuesta")
            return [("PagoMovil", "PagoMovil"), ("Banesco", "Banesco")]
        return [("Banesco", "Banesco"), ("Mercantil", "Mercantil")]

    monkeypatch.setattr(env_store, "discover_methods", discover)
    response = authed.get("/config")
    assert response.status_code == 200
    assert calls == ["USDT", "BTC"]
    assert response.text.count('value="Banesco"') == 1
    assert 'value="Mercantil"' in response.text
    assert ('value="PagoMovil"' in response.text) == (not falla_primera)


def test_panel_registro_solo_con_sesion_y_fuera_del_refresco(client, authed):
    body = authed.get('/').text
    assert '<dialog id="trade-dialog"' in body
    assert 'id="trade-form"' in body
    assert 'aria-labelledby="dialog-title"' in body
    assert '<details class="card log-panel">' in body
    authed.post('/logout')
    public = client.get('/').text
    assert '<dialog' not in public
    assert 'id="trade-form"' not in public


def test_formularios_del_panel_conservan_snapshot_y_control_de_envio(authed, env):
    _seed_opportunity(str(env / 'opps.db'))
    for route in ('/trades/new?opp_id=1', '/trades/fail?opp_id=1'):
        response = authed.get(route)
        assert response.status_code == 200
        assert 'name="est_profit_usdt" value="2.77"' in response.text
        assert 'hx-disabled-elt="find button[type=submit]"' in response.text
        assert 'data-close-dialog' in response.text


def test_oportunidad_una_sola_fila_para_avisos_y_etiquetas_moviles(client, env):
    _seed_opportunity(str(env / 'opps.db'))
    body = client.get('/partials/opportunities').text
    assert body.count('data-opp-id="1"') == 1
    assert 'data-label="Par"' in body
    assert 'data-label="Ganancia est."' in body


def test_historial_muestra_ganancias_btc_sin_redondear_a_cero(authed):
    response = _post_completed(
        authed, asset='BTC', real_usdt='0.0001',
        real_fiat_buy='1000', real_fiat_sell='1020',
    )
    assert response.status_code == 200
    assert '~0.000002 BTC' in response.text
    assert 'Ganancia realizada' in response.text


def _seed_market(env):
    from p2p_arb_bot.domain.market import evaluate_routes
    from p2p_arb_bot.infrastructure.market_store import MarketStore
    from tests.test_monitor import make_ad, target
    buys, sells = [make_ad("b", "800", "BUY")], [make_ad("s", "820", "SELL")]
    t = target()
    store = MarketStore(str(env / "opps.db"))
    store.record(t, buys, sells, datetime.now(timezone.utc))
    store.close()
    return buys, sells, t, evaluate_routes(buys, sells, t)[0].key


def _seed_funds(env):
    from p2p_arb_bot.web.fund_store import FundStore
    store = FundStore(str(env / "trades.db"))
    aid = store.save(name="Banco privado", fiat="VES", balance="5000", methods="PagoMovil", can_receive=True)
    return store, aid


@pytest.mark.parametrize("route,data", [
    ("/funds/accounts", {"name": "Cuenta", "fiat": "VES", "balance": "100", "methods": "PagoMovil"}),
    ("/funds/accounts/1/archive", {"version": 1}),
    ("/funds/reservations", {"account_id": 1, "amount": "50", "version": 1}),
    ("/funds/reservations/1/release", {}),
    ("/market/revalidate", {"route_key": "ruta"}),
    ("/market/reserve", {"route_key": "ruta", "account_id": 1, "version": 1, "funds_revision": 1}),
])
def test_nuevas_mutaciones_exigen_login(client, route, data):
    assert client.post(route, data=data, headers={"HX-Request": "true"}).status_code == 401


def test_mercado_publico_no_filtra_fondos_ni_comprobaciones(client, env):
    from p2p_arb_bot.infrastructure.control_store import ControlStore
    _seed_market(env)
    _seed_funds(env)
    ControlStore(str(env / "control.db")).enqueue({"route_key": "privada", "funds": "123456.78"})
    for endpoint in ("/", "/partials/market", "/partials/opportunities", "/partials/status", "/partials/log"):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert "Banco privado" not in response.text and "123456.78" not in response.text
        assert "Comprobaciones solicitadas" not in response.text
    body = client.get("/partials/market").text
    assert "Observada" in body and "Oportunidades según tus fondos" not in body


def test_gestion_fondos_reserva_y_liberacion(authed, env):
    response = authed.post("/funds/accounts", data={"name": "Mi banco", "fiat": "VES", "balance": "5000",
                                                   "methods": "PagoMovil,Banesco", "can_receive": "true"})
    assert response.status_code == 200 and "Mi banco" in response.text
    assert authed.post("/funds/reservations", data={"account_id": 1, "amount": "1000", "version": 1}).status_code == 200
    body = authed.get("/funds").text
    assert "Disponible: 4000 VES" in body
    response = authed.post("/funds/accounts", data={"account_id": 1, "version": 2, "name": "Mi banco",
                                                   "fiat": "VES", "balance": "500", "methods": "PagoMovil"})
    assert response.status_code == 400 and "reservas activas" in response.text
    assert authed.post("/funds/reservations/1/release").status_code == 200
    assert "Disponible: 5000 VES" in authed.get("/funds").text
    assert authed.post("/funds/accounts/1/archive", data={"version": 3}).status_code == 200
    assert "Fondos sin configurar" in authed.get("/funds").text


def test_revalidacion_bot_detenido_y_reserva_sin_comprobacion(authed, env, monkeypatch):
    from p2p_arb_bot.web.bot_manager import BotManager
    monkeypatch.setattr(BotManager, "is_running", lambda self: False)
    _, _, _, key = _seed_market(env)
    _, aid = _seed_funds(env)
    response = authed.post("/market/revalidate", data={"route_key": key, "account_id": aid})
    assert response.status_code == 200 and "El bot está detenido" in response.text
    response = authed.post("/market/reserve", data={"route_key": key, "account_id": aid, "version": 1, "funds_revision": 1})
    assert "Revalida esta ruta" in response.text


def test_flujo_revalidacion_a_reserva_con_fuente_falsa(authed, env, monkeypatch):
    import asyncio
    from p2p_arb_bot.application.monitor import MonitorService
    from p2p_arb_bot.infrastructure.control_store import ControlStore
    from p2p_arb_bot.infrastructure.market_store import MarketStore
    from p2p_arb_bot.web.bot_manager import BotManager
    from tests.test_monitor import FakeSource
    monkeypatch.setattr(BotManager, "is_running", lambda self: True)
    buys, sells, t, key = _seed_market(env)
    store, aid = _seed_funds(env)
    data = {"route_key": key, "account_id": aid}
    response = authed.post("/market/revalidate", data=data)
    assert response.status_code == 200 and "Pendiente" in response.text
    # El segundo clic reutiliza la solicitud pendiente.
    authed.post("/market/revalidate", data=data)
    queue = ControlStore(str(env / "control.db"))
    assert len(queue.recent()) == 1
    market = MarketStore(str(env / "opps.db"))
    service = MonitorService(FakeSource(buys, sells), [], [], observer=market, revalidations=queue)
    asyncio.run(service.process_revalidation([t]))
    market.close()
    body = authed.get("/partials/market").text
    assert "Revalidada" in body and "Reservar fondos" in body
    form = authed.get(f"/trades/new?market_key={key}&account_id={aid}")
    assert form.status_code == 200
    assert f'name="market_route_key" value="{key}"' in form.text
    assert f'name="revalidation_id" value="{queue.recent()[0]["id"]}"' in form.text
    assert "Tiempo medio desde la solicitud" in body
    response = authed.post("/market/reserve", data={**data, "version": 1, "funds_revision": 1})
    assert response.status_code == 200 and "Fondos reservados" in response.text
    assert store.snapshot()[0][0].available == 0
    assert len(store.reservations()) == 1
    # La reserva no convierte el snapshot del intento en una operación de cero.
    form = authed.get(f"/trades/new?market_key={key}&account_id={aid}").text
    assert 'value="6.25"' in form and 'name="funds_revision" value="1"' in form


def test_registro_de_ruta_conserva_version_y_no_modifica_fondos(authed, env):
    _, _, _, key = _seed_market(env)
    funds, aid = _seed_funds(env)
    for endpoint in ("new", "fail"):
        body = authed.get(f"/trades/{endpoint}?market_key={key}&account_id={aid}").text
        assert f'name="market_route_key" value="{key}"' in body
        assert 'name="funds_revision" value="1"' in body
    response = authed.post("/trades", data={"status": "completed", "opportunity_id": 0,
                                           "asset": "USDT", "fiat": "VES", "real_fiat_buy": "5000",
                                           "real_fiat_sell": "5100", "real_usdt": "6.25",
                                           "market_route_key": key, "funds_revision": "1",
                                           "revalidation_id": "snapshot-privado",
                                           "revalidation_checked_at": "2026-10-08T12:00:00+00:00"})
    assert response.status_code == 200 and "Comprobación: 2026-10-08T12:00:00" in response.text
    saved = trade_store.recent_trades(str(env / "trades.db"))[0]
    assert saved["market_route_key"] == key and saved["funds_revision"] == "1"
    assert saved["revalidation_id"] == "snapshot-privado"
    assert funds.snapshot()[0][0].available == 5000


def test_mercado_con_sesion_no_se_puede_cachear(authed, env):
    _seed_market(env)
    _seed_funds(env)
    response = authed.get("/partials/market")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Cookie"


def test_consulta_normal_identica_mantiene_revalidacion_y_cambio_la_invalida(authed, env, monkeypatch):
    import asyncio
    from dataclasses import replace
    from datetime import timedelta
    from p2p_arb_bot.application.monitor import MonitorService
    from p2p_arb_bot.infrastructure.control_store import ControlStore
    from p2p_arb_bot.infrastructure.market_store import MarketStore
    from p2p_arb_bot.web.bot_manager import BotManager
    from tests.test_monitor import FakeSource
    monkeypatch.setattr(BotManager, "is_running", lambda self: True)
    buys, sells, t, key = _seed_market(env)
    _, aid = _seed_funds(env)
    authed.post("/market/revalidate", data={"route_key": key, "account_id": aid})
    market = MarketStore(str(env / "opps.db"))
    service = MonitorService(FakeSource(buys, sells), [], [], observer=market,
                             revalidations=ControlStore(str(env / "control.db")))
    asyncio.run(service.process_revalidation([t]))
    market.record(t, buys, sells, datetime.now(timezone.utc) + timedelta(seconds=1))
    assert "Reservar fondos" in authed.get("/partials/market").text
    market.record(t, buys, [replace(sells[0], price=Decimal("830"))], datetime.now(timezone.utc) + timedelta(seconds=2))
    assert "Reservar fondos" not in authed.get("/partials/market").text
    market.close()


def test_cambio_saldo_durante_revalidacion_impide_reserva(authed, env, monkeypatch):
    import asyncio
    from p2p_arb_bot.application.monitor import MonitorService
    from p2p_arb_bot.infrastructure.control_store import ControlStore
    from p2p_arb_bot.web.bot_manager import BotManager
    from tests.test_monitor import FakeSource
    monkeypatch.setattr(BotManager, "is_running", lambda self: True)
    buys, sells, t, key = _seed_market(env)
    funds, aid = _seed_funds(env)
    data = {"route_key": key, "account_id": aid}
    authed.post("/market/revalidate", data=data)
    funds.save(name="Banco privado", fiat="VES", balance="4000", methods="PagoMovil", can_receive=True,
               account_id=aid, expected_version=1)
    service = MonitorService(FakeSource(buys, sells), [], [], revalidations=ControlStore(str(env / "control.db")))
    asyncio.run(service.process_revalidation([t]))
    assert "Los fondos cambiaron" in authed.get("/partials/market").text
    response = authed.post("/market/reserve", data={**data, "version": 2, "funds_revision": 2})
    assert "Revalida esta ruta" in response.text and funds.reservations() == []


def test_datos_caducados_no_ofrecen_reserva_y_esquema_viejo_sigue_visible(authed, env):
    from datetime import timedelta
    from p2p_arb_bot.infrastructure.market_store import MarketStore
    buys, sells, t, _ = _seed_market(env)
    market = MarketStore(str(env / "opps.db"))
    market.record(t, buys, sells, datetime.now(timezone.utc) - timedelta(seconds=120))
    market.close()
    _seed_opportunity(str(env / "opps.db"))
    _seed_funds(env)
    body = authed.get("/").text
    assert "Sin datos recientes" in body and "Mercado desactualizado" in body
    assert "Historial de oportunidades" in body and "vend1" not in body  # vendedor está en el formulario, no tabla
    assert "Reservar fondos" not in body

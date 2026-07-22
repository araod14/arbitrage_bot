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


# --- rutas protegidas --------------------------------------------------------

PROTECTED_GET = ["/partials/trades", "/trades/new?opp_id=1", "/trades/fail?opp_id=1", "/config"]


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

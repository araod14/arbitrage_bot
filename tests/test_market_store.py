"""Vigencia por par y conservación del historial observado."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

from p2p_arb_bot.infrastructure.market_store import MarketStore
from p2p_arb_bot.infrastructure.sqlite_repo import SQLiteRepository
from p2p_arb_bot.web.market_reader import books, episodes
from tests.test_monitor import make_ad, target

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_actualiza_frescura_y_conserva_primera_observacion(tmp_path):
    path = str(tmp_path / "opps.db")
    store = MarketStore(path)
    buys, sells = [make_ad("b", "800", "BUY")], [make_ad("s", "820", "SELL")]
    store.record(target(), buys, sells, NOW)
    store.record(target(), buys, sells, NOW + timedelta(seconds=30))
    rows = episodes(path, 60, now=NOW + timedelta(seconds=40))
    assert len(rows) == 1 and rows[0]["age_s"] == 10
    assert rows[0]["first_seen"] == NOW.isoformat()
    assert not rows[0]["stale"]
    assert episodes(path, 60, now=NOW + timedelta(seconds=91))[0]["stale"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 1
    store.close()


def test_fallo_inicial_es_distinto_de_libro_vacio(tmp_path):
    path = str(tmp_path / "opps.db")
    store = MarketStore(path)
    store.failed(target(), NOW)
    row = books(path, 60, now=NOW)[0]
    assert row["error"] and row["stale"] and not row["checked_at"]
    store.close()


def test_limita_rutas_activas_y_sigue_comprobando_anterior(tmp_path):
    path = str(tmp_path / "opps.db")
    store = MarketStore(path, max_routes=1)
    buys, sells = [make_ad("b", "800", "BUY")], [make_ad("s", "820", "SELL")]
    store.record(target(), buys, sells, NOW)
    store.record(target(), buys + [make_ad("mejor", "790", "BUY")], sells, NOW + timedelta(seconds=30))
    assert len(episodes(path, 60, now=NOW + timedelta(seconds=31))) == 1
    store.record(target(), buys + [make_ad("mejor", "790", "BUY")], [replace(sells[0], price=Decimal("798"))], NOW + timedelta(seconds=60))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_episodes WHERE active=1").fetchone()[0] <= 1
    store.close()


def test_reducir_limite_pausa_seguimiento_sin_afirmar_desaparicion(tmp_path):
    path = str(tmp_path / "opps.db")
    buys, sells = [make_ad("b", "800", "BUY"), make_ad("b2", "790", "BUY")], [make_ad("s", "820", "SELL")]
    store = MarketStore(path, max_routes=2)
    store.record(target(), buys, sells, NOW)
    store.close()
    store = MarketStore(path, max_routes=1)
    store.record(target(), buys, sells, NOW + timedelta(seconds=30))
    states = {r["state"] for r in episodes(path, 60, now=NOW + timedelta(seconds=31))}
    assert states == {"compatible", "tracking_limited"}
    store.close()


def test_error_conserva_libro_sin_cerrar_episodio(tmp_path):
    path = str(tmp_path / "opps.db")
    store = MarketStore(path)
    store.record(target(), [make_ad("b", "800", "BUY")], [make_ad("s", "820", "SELL")], NOW)
    store.failed(target(), NOW + timedelta(seconds=20))
    assert books(path, 60, now=NOW + timedelta(seconds=25))[0]["checked_at"] == NOW.isoformat()
    row = episodes(path, 60, now=NOW + timedelta(seconds=25))[0]
    assert row["state"] == "compatible" and row["error"]
    store.close()


def test_libro_vacio_no_prueba_expiracion_y_reaparicion_abre_episodio(tmp_path):
    path = str(tmp_path / "opps.db")
    store = MarketStore(path)
    buys, sells = [make_ad("b", "800", "BUY")], [make_ad("s", "820", "SELL")]
    store.record(target(), buys, sells, NOW)
    store.record(target(), [], [], NOW + timedelta(seconds=30))
    assert episodes(path, 60, now=NOW + timedelta(seconds=31))[0]["state"] == "not_observed"
    store.record(target(), buys, sells, NOW + timedelta(seconds=60))
    assert len(episodes(path, 60, now=NOW + timedelta(seconds=61))) == 2
    store.close()


def test_cambiar_mejor_ruta_no_invalida_anterior_y_pars_independientes(tmp_path):
    path = str(tmp_path / "opps.db")
    store = MarketStore(path)
    buys, sells = [make_ad("b", "800", "BUY")], [make_ad("s", "820", "SELL")]
    store.record(target(), buys, sells, NOW)
    store.record(target(), buys + [make_ad("b2", "790", "BUY")], sells, NOW + timedelta(seconds=30))
    rows = episodes(path, 60, now=NOW + timedelta(seconds=31))
    assert len(rows) == 2 and all(r["state"] == "compatible" for r in rows)
    store.record(target(asset="BTC"), buys, sells, NOW + timedelta(seconds=100))
    rows = books(path, 60, now=NOW + timedelta(seconds=101))
    assert {r["asset"]: r["stale"] for r in rows} == {"USDT": True, "BTC": False}
    store.close()


def test_retencion_y_lectura_de_esquema_antiguo(tmp_path):
    path = str(tmp_path / "opps.db")
    repo = SQLiteRepository(path)
    repo.close()
    assert books(path, 60) == [] and episodes(path, 60) == []
    store = MarketStore(path, retention_days=1)
    store.record(target(), [make_ad("b", "800", "BUY")], [make_ad("s", "820", "SELL")], NOW)
    store.record(target(asset="BTC"), [], [], NOW + timedelta(days=2))
    assert episodes(path, 60, now=NOW + timedelta(days=2)) == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 0
    store.close()

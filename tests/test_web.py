"""Tests del dashboard: escritura de .env, status notifier y lectura de DB.

Sin red ni servidor: se ejercitan las funciones puras de soporte usando ficheros
temporales (tmp_path) y una DB SQLite construida a mano con el esquema del bot.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from p2p_arb_bot.domain.models import Opportunity
from p2p_arb_bot.infrastructure.sqlite_repo import SQLiteRepository
from p2p_arb_bot.infrastructure.status_notifier import StatusNotifier
from p2p_arb_bot.web import db_reader, env_store


# --- env_store.write_config -------------------------------------------------

def test_write_config_preserves_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comentario\n"
        "ASSET=USDT\n"
        "THRESHOLD_PCT=1.0\n"
        "FIAT=VES\n"
        "MAX_FIAT=5000\n",
        encoding="utf-8",
    )

    env_store.write_config(
        str(env),
        threshold_pct="2.5",
        max_fiat="250000",
        pay_methods=["PagoMovil", "Banesco"],
        poll_interval_s=45,
        assets=["USDT", "BTC"],
    )

    content = env.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert "# comentario" in lines
    assert "ASSET=USDT" in lines
    assert "FIAT=VES" in lines
    assert "THRESHOLD_PCT=2.5" in lines
    assert "MAX_FIAT=250000" in lines
    assert "ASSETS=USDT,BTC" in lines
    assert "PAY_METHODS=PagoMovil,Banesco" in lines
    assert "POLL_INTERVAL_S=45" in lines
    # No duplica claves existentes.
    assert sum(1 for ln in lines if ln.startswith("THRESHOLD_PCT=")) == 1


def test_write_config_appends_missing_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ASSET=USDT\n", encoding="utf-8")
    env_store.write_config(
        str(env),
        threshold_pct="1.0",
        max_fiat="80000",
        pay_methods=[],
        poll_interval_s=30,
        assets=["USDT"],
    )
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "PAY_METHODS=" in lines  # vacío = todos
    assert "POLL_INTERVAL_S=30" in lines


def test_write_config_rejects_bad_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    try:
        env_store.write_config(
            str(env),
            threshold_pct="1.0",
            max_fiat="0",  # inválido
            pay_methods=[],
            poll_interval_s=30,
            assets=["USDT"],
        )
        assert False, "debería lanzar ValueError"
    except ValueError:
        pass


def test_write_config_updates_os_environ(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.delenv("THRESHOLD_PCT", raising=False)
    env_store.write_config(
        str(env),
        threshold_pct="3.3",
        max_fiat="500000",
        pay_methods=["Mercantil"],
        poll_interval_s=20,
        assets=["USDT"],
    )
    assert os.environ["THRESHOLD_PCT"] == "3.3"
    assert os.environ["PAY_METHODS"] == "Mercantil"


def test_write_config_rejects_empty_assets(tmp_path):
    """Sin ninguna moneda marcada no habría targets y el bot no arrancaría."""
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        env_store.write_config(
            str(env),
            threshold_pct="1.0",
            max_fiat="80000",
            pay_methods=[],
            poll_interval_s=30,
            assets=[],
        )


def test_write_config_rejects_unknown_asset(tmp_path):
    """El POST podría venir manipulado: solo se aceptan monedas del catálogo."""
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        env_store.write_config(
            str(env),
            threshold_pct="1.0",
            max_fiat="80000",
            pay_methods=[],
            poll_interval_s=30,
            assets=["DOGE"],
        )


# --- StatusNotifier ---------------------------------------------------------

def test_status_notifier_writes_valid_json(tmp_path):
    path = tmp_path / "status.json"
    notifier = StatusNotifier(str(path))

    from p2p_arb_bot.domain.models import WatchTarget

    target = WatchTarget(
        asset="USDT", fiat="VES", pay_methods=(),
        max_fiat=Decimal("80000"), threshold_pct=Decimal("1"),
    )
    asyncio.run(notifier.notify_heartbeat(target, Decimal("2.5")))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["target"] == "USDT/VES"
    assert data["best_net_pct"] == 2.5
    assert "last_update" in data


def test_status_notifier_records_last_opportunity(tmp_path):
    path = tmp_path / "status.json"
    notifier = StatusNotifier(str(path))
    opp = Opportunity(
        detected_at=datetime.now(timezone.utc),
        fiat="VES", asset="USDT",
        buy_pay_method="A", sell_pay_method="B",
        buy_price=Decimal("36"), sell_price=Decimal("37"),
        spread_pct=Decimal("2.77"), net_pct=Decimal("2.5"),
        max_usdt=Decimal("100"),
        buy_adv_no="1", sell_adv_no="2",
        buy_advertiser="x", sell_advertiser="y",
        buy_url="http://b", sell_url="http://s",
        est_profit_usdt=Decimal("2.5"),
    )
    asyncio.run(notifier.notify_opportunity(opp))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["best_net_pct"] == 2.5
    assert data["last_opportunity"]["sell_price"] == "37"


# --- db_reader --------------------------------------------------------------

def _seed_db(path: str, *, net_pcts: list[float]) -> None:
    repo = SQLiteRepository(path)
    now = datetime.now(timezone.utc)
    for i, net in enumerate(net_pcts):
        repo.save(
            Opportunity(
                detected_at=now - timedelta(minutes=i),
                fiat="VES", asset="USDT",
                buy_pay_method="A", sell_pay_method="B",
                buy_price=Decimal("36"), sell_price=Decimal("37"),
                spread_pct=Decimal(str(net + 0.2)), net_pct=Decimal(str(net)),
                max_usdt=Decimal("100"),
                buy_adv_no=f"b{i}", sell_adv_no=f"s{i}",
                buy_advertiser="x", sell_advertiser="y",
                buy_url="http://b", sell_url="http://s",
                est_profit_usdt=Decimal("1.5"),
                buy_min_amount=Decimal("100"), buy_max_amount=Decimal("100000"),
                sell_min_amount=Decimal("100"), sell_max_amount=Decimal("100000"),
            )
        )
    repo.close()


def test_stats_24h(tmp_path):
    db = tmp_path / "opps.db"
    _seed_db(str(db), net_pcts=[1.0, 2.0, 3.0])
    stats = db_reader.stats_24h(str(db))
    assert stats["count_24h"] == 3
    assert stats["best_net_pct"] == 3.0
    # 1.5 unidades * 36 VES de precio de compra, por 3 filas.
    assert stats["total_profit_fiat"] == 162.0
    assert stats["fiat"] == "VES"
    assert stats["last_detection"] is not None


def test_stats_24h_missing_db(tmp_path):
    stats = db_reader.stats_24h(str(tmp_path / "nope.db"))
    assert stats["count_24h"] == 0
    assert stats["best_net_pct"] is None


def test_recent_opportunities(tmp_path):
    db = tmp_path / "opps.db"
    _seed_db(str(db), net_pcts=[1.0, 2.0])
    rows = db_reader.recent_opportunities(str(db), limit=10)
    assert len(rows) == 2
    assert rows[0]["asset"] == "USDT"


def test_recent_opportunities_includes_sizing(tmp_path):
    db = tmp_path / "opps.db"
    _seed_db(str(db), net_pcts=[2.0])
    row = db_reader.recent_opportunities(str(db), limit=1)[0]
    # Fondo 100 USDT dentro de los límites -> usable = fondo, factible.
    assert row["feasible"] is True
    assert row["usable_usdt"] == 100.0
    assert row["usable_fiat_buy"] == 3600.0   # 100 * 36
    assert row["usable_fiat_sell"] == 3700.0  # 100 * 37


def test_recent_opportunities_degrades_on_old_schema(tmp_path):
    """Una DB con esquema viejo (sin columnas de límites) no rompe y degrada al fondo."""
    db = tmp_path / "old.db"
    # Esquema mínimo previo, sin las columnas de límites de anuncio.
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL, fiat TEXT NOT NULL, asset TEXT NOT NULL,
            buy_pay_method TEXT NOT NULL, sell_pay_method TEXT NOT NULL,
            buy_price TEXT NOT NULL, sell_price TEXT NOT NULL,
            spread_pct REAL NOT NULL, net_pct REAL NOT NULL, max_usdt TEXT NOT NULL,
            buy_adv_no TEXT NOT NULL, sell_adv_no TEXT NOT NULL,
            buy_advertiser TEXT NOT NULL, sell_advertiser TEXT NOT NULL,
            buy_url TEXT NOT NULL, sell_url TEXT NOT NULL,
            est_profit_usdt TEXT NOT NULL DEFAULT '0'
        )
        """
    )
    conn.execute(
        "INSERT INTO opportunities (detected_at, fiat, asset, buy_pay_method, "
        "sell_pay_method, buy_price, sell_price, spread_pct, net_pct, max_usdt, "
        "buy_adv_no, sell_adv_no, buy_advertiser, sell_advertiser, buy_url, sell_url) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "VES", "USDT", "A", "B",
         "36", "37", 2.2, 2.0, "100", "b0", "s0", "x", "y", "http://b", "http://s"),
    )
    conn.commit()
    conn.close()
    # Sin migrar (lectura pura del dashboard): degrada al fondo, sin mínimos.
    row = db_reader.recent_opportunities(str(db), limit=1)[0]
    assert row["feasible"] is True
    assert row["usable_usdt"] == 100.0
    # Las claves de mínimos deben existir (0) aunque falten en el esquema, para
    # que la plantilla del dashboard no falle al renderizarlas.
    assert row["buy_min_amount"] == 0.0
    assert row["sell_min_amount"] == 0.0


def test_migration_adds_limit_columns(tmp_path):
    """Abrir una DB vieja con SQLiteRepository añade las columnas de límites."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE opportunities (id INTEGER PRIMARY KEY, detected_at TEXT)")
    conn.commit()
    conn.close()
    repo = SQLiteRepository(str(db))
    cols = {r[1] for r in repo._conn.execute("PRAGMA table_info(opportunities)")}
    repo.close()
    assert {"buy_min_amount", "buy_max_amount",
            "sell_min_amount", "sell_max_amount"} <= cols


def test_clear_opportunities(tmp_path):
    db = tmp_path / "opps.db"
    _seed_db(str(db), net_pcts=[1.0, 2.0, 3.0])
    removed = db_reader.clear_opportunities(str(db))
    assert removed == 3
    assert db_reader.recent_opportunities(str(db), limit=10) == []
    # Idempotente: volver a limpiar no falla y borra 0.
    assert db_reader.clear_opportunities(str(db)) == 0


def test_clear_opportunities_missing_db(tmp_path):
    assert db_reader.clear_opportunities(str(tmp_path / "nope.db")) == 0


def test_clear_status_removes_file(tmp_path):
    status = tmp_path / "status.json"
    status.write_text('{"best_net_pct": 1.2, "target": "USDT/VES"}', encoding="utf-8")
    assert db_reader.clear_status(str(status)) is True
    assert db_reader.read_status(str(status)) is None
    # Idempotente: si ya no existe, no falla y devuelve False.
    assert db_reader.clear_status(str(status)) is False


def test_read_status_missing(tmp_path):
    assert db_reader.read_status(str(tmp_path / "nope.json")) is None


def test_tail_log(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("\n".join(f"line {i}" for i in range(10)) + "\n", encoding="utf-8")
    tail = db_reader.tail_log(str(log), lines=3)
    assert tail == ["line 7", "line 8", "line 9"]


def test_tail_log_newest_first_reorders_heartbeats(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "[13:00:01] USDT/VES  spread 0.1%\n"
        "[13:00:02] USDT/VES  spread 0.2%\n"
        "[13:00:03] USDT/VES  spread 0.3%\n",
        encoding="utf-8",
    )
    lines = db_reader.tail_log(str(log), lines=10, newest_first=True)
    assert lines[0] == "[13:00:03] USDT/VES  spread 0.3%"
    assert lines[-1] == "[13:00:01] USDT/VES  spread 0.1%"


def test_tail_log_newest_first_keeps_panel_block_intact(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "[13:00:01] USDT/VES  spread 0.1%\n"
        "╭──── OPORTUNIDAD ────╮\n"
        "│ Ganancia NETA 0.7%  │\n"
        "╰── detectada 13:00:02 ─╯\n"
        "[13:00:03] USDT/VES  spread 0.3%\n",
        encoding="utf-8",
    )
    lines = db_reader.tail_log(str(log), lines=10, newest_first=True)
    # El heartbeat más reciente va primero.
    assert lines[0] == "[13:00:03] USDT/VES  spread 0.3%"
    # El panel va por encima del heartbeat viejo y conserva su orden interno.
    panel = [ln for ln in lines if ln.startswith(("╭", "│", "╰"))]
    assert panel == [
        "╭──── OPORTUNIDAD ────╮",
        "│ Ganancia NETA 0.7%  │",
        "╰── detectada 13:00:02 ─╯",
    ]
    i_panel_top = lines.index("╭──── OPORTUNIDAD ────╮")
    i_old_hb = lines.index("[13:00:01] USDT/VES  spread 0.1%")
    assert i_panel_top < i_old_hb

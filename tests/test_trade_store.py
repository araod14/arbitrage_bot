"""Tests del registro de operaciones ejecutadas a mano.

SQLite real sobre ``tmp_path`` (como ``test_web.py``), sin red ni servidor: el
almacén crea su propio esquema, así que no hace falta fake.
"""

from __future__ import annotations

import sqlite3

from p2p_arb_bot.web import trade_store


def _completed(**over) -> dict:
    """Operación cerrada: compró 100 USDT por 3600 y vendió por 3700 (+2.777%)."""
    base = {
        "status": trade_store.STATUS_COMPLETED,
        "opportunity_id": 7,
        "detected_at": "2026-07-15T10:00:00+00:00",
        "asset": "USDT",
        "fiat": "VES",
        "est_net_pct": 3.0,
        "est_profit_usdt": "3.0",
        "real_buy_price": "36",
        "real_sell_price": "37",
        "real_usdt": "100",
        "real_fiat_buy": "3600",
        "real_fiat_sell": "3700",
        "buy_advertiser": "vendedor1",
        "sell_advertiser": "comprador1",
        "buy_pay_method": "PagoMovil",
        "sell_pay_method": "Banesco",
        "notes": "sin incidencias",
    }
    base.update(over)
    return base


def test_save_and_read_completed(tmp_path):
    db = str(tmp_path / "trades.db")
    assert trade_store.save_trade(db, _completed()) is not None

    rows = trade_store.recent_trades(db)
    assert len(rows) == 1
    t = rows[0]
    assert t["status"] == "completed"
    assert t["buy_advertiser"] == "vendedor1"
    # 3700 - 3600 = 100 VES => 100/3600 = 2.777% sobre lo invertido
    assert t["profit_fiat"] == 100.0
    assert round(t["net_pct"], 3) == 2.778
    # El bot prometía 3.0%: la realidad entregó menos.
    assert round(t["net_pct_delta"], 3) == -0.222


def test_save_failed_keeps_reason_and_has_no_pnl(tmp_path):
    db = str(tmp_path / "trades.db")
    trade_store.save_trade(
        db,
        {
            "status": trade_store.STATUS_FAILED,
            "opportunity_id": 9,
            "detected_at": "2026-07-15T10:00:00+00:00",
            "asset": "USDT",
            "fiat": "VES",
            "est_net_pct": 4.0,
            "failure_reason": "too_late",
            "notes": "el anuncio voló",
        },
    )
    t = trade_store.recent_trades(db)[0]
    assert t["status"] == "failed"
    assert t["failure_label"] == "No llegué a tiempo / el anuncio expiró"
    # Una operación no cerrada no tiene ganancia real, pero las claves EXISTEN a
    # None: en Jinja una clave ausente es Undefined y `Undefined is not none` da
    # True, lo que hacía reventar la plantilla al formatear el delta.
    for key in ("net_pct", "net_pct_delta", "profit_fiat", "profit_usdt"):
        assert key in t, f"falta {key}"
        assert t[key] is None


def test_snapshot_survives_clearing_opportunities(tmp_path):
    """La razón de ser del snapshot: "Limpiar" borra las oportunidades.

    Si el registro solo guardara ``opportunity_id``, limpiar dejaría las
    operaciones sin base de comparación contra lo estimado.
    """
    db = str(tmp_path / "trades.db")
    opps = tmp_path / "opps.db"
    # Base del bot con la oportunidad 7 dentro...
    conn = sqlite3.connect(opps)
    conn.execute("CREATE TABLE opportunities (id INTEGER PRIMARY KEY, net_pct REAL)")
    conn.execute("INSERT INTO opportunities (id, net_pct) VALUES (7, 3.0)")
    conn.commit()
    trade_store.save_trade(db, _completed())
    # ...que el usuario borra con el botón Limpiar.
    conn.execute("DELETE FROM opportunities")
    conn.commit()
    conn.close()

    t = trade_store.recent_trades(db)[0]
    assert t["est_net_pct"] == 3.0
    assert t["detected_at"] == "2026-07-15T10:00:00+00:00"
    assert t["net_pct_delta"] is not None


def test_summary_mixes_completed_and_failed(tmp_path):
    db = str(tmp_path / "trades.db")
    trade_store.save_trade(db, _completed())
    trade_store.save_trade(db, _completed(real_fiat_sell="3500"))  # pérdida
    trade_store.save_trade(
        db, {"status": trade_store.STATUS_FAILED, "failure_reason": "no_funds"}
    )

    s = trade_store.summary(db)
    assert s["total"] == 3
    assert s["completed"] == 2
    assert s["failed"] == 1
    assert round(s["hit_rate"], 1) == 66.7
    # +100 VES y -100 VES sobre 3600 invertidos: se compensan.
    assert round(s["profit_usdt"], 6) == 0.0


def test_summary_empty(tmp_path):
    s = trade_store.summary(str(tmp_path / "trades.db"))
    assert s == {
        "total": 0,
        "completed": 0,
        "failed": 0,
        "hit_rate": None,
        "profit_usdt": 0,
        "avg_net_pct": None,
        "avg_net_pct_delta": None,
    }


def test_bad_numbers_do_not_raise(tmp_path):
    """El formulario es texto libre: basura => 0, nunca una excepción."""
    db = str(tmp_path / "trades.db")
    trade_store.save_trade(db, _completed(real_fiat_buy="", real_fiat_sell="abc"))
    t = trade_store.recent_trades(db)[0]
    # fiat_in = 0 => no se puede calcular % sobre una inversión desconocida
    assert t["net_pct"] == 0.0
    assert t["profit_fiat"] == 0.0


def test_decimal_comma_is_accepted(tmp_path):
    """Un usuario en es-VE escribe 3.600,50 o 3600,50: la coma no debe romper."""
    db = str(tmp_path / "trades.db")
    trade_store.save_trade(db, _completed(real_fiat_sell="3700,50"))
    t = trade_store.recent_trades(db)[0]
    assert t["profit_fiat"] == 100.5

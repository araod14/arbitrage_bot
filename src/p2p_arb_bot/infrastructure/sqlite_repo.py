"""Adaptador ``OpportunityRepository`` sobre SQLite (stdlib)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from ..domain.models import Opportunity

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT    NOT NULL,
    fiat            TEXT    NOT NULL,
    asset           TEXT    NOT NULL,
    buy_pay_method  TEXT    NOT NULL,
    sell_pay_method TEXT    NOT NULL,
    buy_price       TEXT    NOT NULL,
    sell_price      TEXT    NOT NULL,
    spread_pct      REAL    NOT NULL,
    net_pct         REAL    NOT NULL,
    max_usdt        TEXT    NOT NULL,
    buy_adv_no      TEXT    NOT NULL,
    sell_adv_no     TEXT    NOT NULL,
    buy_advertiser  TEXT    NOT NULL,
    sell_advertiser TEXT    NOT NULL,
    buy_url         TEXT    NOT NULL,
    sell_url        TEXT    NOT NULL,
    est_profit_usdt TEXT    NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_opportunities_detected_at
    ON opportunities (detected_at);
"""


class SQLiteRepository:
    """Persiste oportunidades en SQLite y detecta duplicados recientes."""

    def __init__(self, db_path: str = "opportunities.db") -> None:
        # check_same_thread=False: el bucle asyncio puede tocar la conexión desde
        # callbacks distintos; el acceso es secuencial, así que es seguro aquí.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Añade columnas nuevas a BDs creadas con un esquema anterior."""
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(opportunities)")
        }
        if "est_profit_usdt" not in existing:
            self._conn.execute(
                "ALTER TABLE opportunities "
                "ADD COLUMN est_profit_usdt TEXT NOT NULL DEFAULT '0'"
            )

    def save(self, opp: Opportunity) -> None:
        self._conn.execute(
            """
            INSERT INTO opportunities (
                detected_at, fiat, asset, buy_pay_method, sell_pay_method,
                buy_price, sell_price, spread_pct, net_pct, max_usdt,
                buy_adv_no, sell_adv_no, buy_advertiser, sell_advertiser,
                buy_url, sell_url, est_profit_usdt
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                opp.detected_at.isoformat(),
                opp.fiat,
                opp.asset,
                opp.buy_pay_method,
                opp.sell_pay_method,
                str(opp.buy_price),
                str(opp.sell_price),
                float(opp.spread_pct),
                float(opp.net_pct),
                str(opp.max_usdt),
                opp.buy_adv_no,
                opp.sell_adv_no,
                opp.buy_advertiser,
                opp.sell_advertiser,
                opp.buy_url,
                opp.sell_url,
                str(opp.est_profit_usdt),
            ),
        )
        self._conn.commit()

    def recent_duplicate(self, opp: Opportunity, window_s: int) -> bool:
        """¿Misma combinación advNo + precios registrada en la ventana reciente?"""
        cutoff = (opp.detected_at - timedelta(seconds=window_s)).isoformat()
        cur = self._conn.execute(
            """
            SELECT 1 FROM opportunities
            WHERE buy_adv_no = ? AND sell_adv_no = ?
              AND buy_price = ? AND sell_price = ?
              AND detected_at >= ?
            LIMIT 1
            """,
            (
                opp.buy_adv_no,
                opp.sell_adv_no,
                str(opp.buy_price),
                str(opp.sell_price),
                cutoff,
            ),
        )
        return cur.fetchone() is not None

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - cierre best-effort
            logger.debug("Error cerrando SQLite: %s", exc)

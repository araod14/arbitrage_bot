"""Lectura solo-lectura de la DB del bot, del status.json y del log.

El dashboard corre en un proceso distinto al del bot. Para no interferir con las
escrituras del bot, abre SQLite en modo ``ro`` (read-only) vía URI y nunca crea
ni migra la base. Si la DB aún no existe (bot nunca arrancado), degrada a vacío.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import deque
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _connect_ro(db_path: str) -> sqlite3.Connection | None:
    if not os.path.exists(db_path):
        return None
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def recent_opportunities(db_path: str, limit: int = 50) -> list[dict]:
    """Últimas oportunidades detectadas, más recientes primero."""
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        cur = conn.execute(
            """
            SELECT detected_at, asset, fiat, buy_pay_method, sell_pay_method,
                   buy_price, sell_price, spread_pct, net_pct, max_usdt,
                   buy_advertiser, sell_advertiser, buy_url, sell_url,
                   est_profit_usdt
            FROM opportunities
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        logger.warning("Error leyendo oportunidades: %s", exc)
        return []
    finally:
        conn.close()


def stats_24h(db_path: str) -> dict:
    """Resumen de las últimas 24 h: conteo, mejor net %, profit estimado total."""
    empty = {
        "count_24h": 0,
        "best_net_pct": None,
        "total_profit_usdt": 0.0,
        "last_detection": None,
    }
    conn = _connect_ro(db_path)
    if conn is None:
        return empty
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cur = conn.execute(
            """
            SELECT COUNT(*)                     AS count_24h,
                   MAX(net_pct)                 AS best_net_pct,
                   COALESCE(SUM(CAST(est_profit_usdt AS REAL)), 0) AS total_profit
            FROM opportunities
            WHERE detected_at >= ?
            """,
            (cutoff,),
        )
        row = cur.fetchone()
        last = conn.execute(
            "SELECT MAX(detected_at) AS last FROM opportunities"
        ).fetchone()
        return {
            "count_24h": row["count_24h"] or 0,
            "best_net_pct": row["best_net_pct"],
            "total_profit_usdt": round(row["total_profit"] or 0.0, 2),
            "last_detection": last["last"] if last else None,
        }
    except sqlite3.Error as exc:
        logger.warning("Error calculando estadísticas: %s", exc)
        return empty
    finally:
        conn.close()


def read_status(status_path: str) -> dict | None:
    """Lee el status.json escrito por el bot. ``None`` si no existe/ilegible."""
    if not os.path.exists(status_path):
        return None
    try:
        with open(status_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Error leyendo status %s: %s", status_path, exc)
        return None


def tail_log(log_path: str, lines: int = 100) -> list[str]:
    """Devuelve las últimas ``lines`` líneas del log (vacío si no existe)."""
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in deque(fh, maxlen=lines)]
    except OSError as exc:
        logger.warning("Error leyendo log %s: %s", log_path, exc)
        return []

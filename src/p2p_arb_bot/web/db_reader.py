"""Lectura solo-lectura de la DB del bot, del status.json y del log.

El dashboard corre en un proceso distinto al del bot. Para no interferir con las
escrituras del bot, abre SQLite en modo ``ro`` (read-only) vía URI y nunca crea
ni migra la base. Si la DB aún no existe (bot nunca arrancado), degrada a vacío.

Única excepción: ``clear_opportunities`` (acción manual del usuario) abre en
lectura-escritura para vaciar la tabla; no crea ni migra el esquema.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import deque
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def to_local(iso: str | None) -> str:
    """Convierte un timestamp ISO (UTC) a la hora local del servidor.

    El bot guarda ``detected_at`` en UTC (``datetime.now(timezone.utc)``). Sin
    convertir, el dashboard mostraría una hora adelantada respecto al reloj del
    usuario. Devuelve ``"YYYY-MM-DD HH:MM:SS"`` en zona local; si no se puede
    parsear, degrada a los primeros 19 caracteres.
    """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso[:19]
    if dt.tzinfo is None:  # asumir UTC si viene sin zona
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


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
        rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            row["detected_local"] = to_local(row.get("detected_at"))
        return rows
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
        "last_detection_local": "",
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
        last_iso = last["last"] if last else None
        return {
            "count_24h": row["count_24h"] or 0,
            "best_net_pct": row["best_net_pct"],
            "total_profit_usdt": round(row["total_profit"] or 0.0, 2),
            "last_detection": last_iso,
            "last_detection_local": to_local(last_iso),
        }
    except sqlite3.Error as exc:
        logger.warning("Error calculando estadísticas: %s", exc)
        return empty
    finally:
        conn.close()


def clear_opportunities(db_path: str) -> int:
    """Borra TODAS las filas de ``opportunities``. Acción manual del usuario.

    Es la única operación de escritura del dashboard sobre la DB del bot: abre la
    base en lectura-escritura (no ``ro``) el tiempo justo para el DELETE y cierra.
    Si la DB o la tabla no existen, no hace nada. Devuelve cuántas filas borró.
    """
    if not os.path.exists(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error as exc:
        logger.warning("No se pudo abrir la DB para limpiar: %s", exc)
        return 0
    try:
        cur = conn.execute("DELETE FROM opportunities")
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.Error as exc:
        logger.warning("Error limpiando oportunidades: %s", exc)
        return 0
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

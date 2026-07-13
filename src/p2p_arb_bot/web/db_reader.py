"""Lectura solo-lectura de la DB del bot, del status.json y del log.

El dashboard corre en un proceso distinto al del bot. Para no interferir con las
escrituras del bot, abre SQLite en modo ``ro`` (read-only) vía URI y nunca crea
ni migra la base. Si la DB aún no existe (bot nunca arrancado), degrada a vacío.

Únicas escrituras: ``clear_opportunities`` (vacía la tabla en lectura-escritura,
sin crear ni migrar el esquema) y ``clear_status`` (borra ``status.json``), ambas
como acción manual del usuario desde el botón "Limpiar".
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


def clear_status(status_path: str) -> bool:
    """Borra ``status.json`` para reiniciar los KPIs en vivo. Acción manual del usuario.

    Deja en blanco el "mejor spread neto actual" y la última oportunidad que el bot
    publica en ``status.json``. Si el bot sigue corriendo, lo reescribirá con el valor
    actual en su próximo ciclo (es una métrica en vivo); si está parado, queda vacío.
    Devuelve ``True`` si borró el fichero.
    """
    if not os.path.exists(status_path):
        return False
    try:
        os.remove(status_path)
        return True
    except OSError as exc:
        logger.warning("Error borrando status %s: %s", status_path, exc)
        return False


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


def _group_log_entries(lines: list[str]) -> list[list[str]]:
    """Agrupa líneas de log en entradas (bloques) preservando su orden interno.

    Cada heartbeat de una línea (``[HH:MM:SS] …``) es una entrada. Cada panel de
    oportunidad de ``rich`` (``╭…`` + ``│…`` + ``╰…``) es una única entrada con todas
    sus líneas juntas. Se abre una entrada nueva cuando la línea, tras quitarle
    espacios y control iniciales (p. ej. el BEEP ``\\x07``), empieza por ``[`` o ``╭``;
    el resto continúa la entrada actual. Las líneas previas al primer límite forman
    su propia entrada inicial.
    """
    entries: list[list[str]] = []
    for line in lines:
        head = line.lstrip("\x07 \t")
        if head.startswith("[") or head.startswith("╭") or not entries:
            entries.append([line])
        else:
            entries[-1].append(line)
    return entries


def tail_log(log_path: str, lines: int = 100, *, newest_first: bool = False) -> list[str]:
    """Devuelve las últimas ``lines`` líneas del log (vacío si no existe).

    Con ``newest_first=True`` se agrupan las líneas en entradas (ver
    ``_group_log_entries``) y se devuelven con la entrada más reciente primero,
    manteniendo intacto el orden interno de los paneles multilínea.
    """
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = [ln.rstrip("\n") for ln in deque(fh, maxlen=lines)]
    except OSError as exc:
        logger.warning("Error leyendo log %s: %s", log_path, exc)
        return []
    if not newest_first:
        return tail
    entries = _group_log_entries(tail)
    return [line for entry in reversed(entries) for line in entry]

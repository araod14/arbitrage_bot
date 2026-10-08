"""Lectura del mercado en modo ro; nunca crea ni migra la base del bot."""

from datetime import datetime, timezone
import json
import sqlite3

from .db_reader import _connect_ro
from ..infrastructure.market_codec import read_ads, read_target


def age_seconds(stamp: str, now: datetime | None = None) -> int:
    dt = datetime.fromisoformat(stamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int(((now or datetime.now(timezone.utc)) - dt).total_seconds()))


def books(path: str, freshness_s: int, now: datetime | None = None) -> list[dict]:
    conn = _connect_ro(path)
    if conn is None:
        return []
    try:
        result = []
        for row in conn.execute("SELECT * FROM market_books ORDER BY asset,fiat"):
            payload = json.loads(row["payload"])
            age = age_seconds(row["checked_at"], now) if row["checked_at"] else freshness_s
            result.append({**dict(row), "target": read_target(payload["target"]),
                           "buys": read_ads(payload["buys"]), "sells": read_ads(payload["sells"]),
                           "age_s": age, "stale": age >= freshness_s})
        return result
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return []
    finally:
        conn.close()


def episodes(path: str, freshness_s: int, limit: int = 50, now: datetime | None = None) -> list[dict]:
    conn = _connect_ro(path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT e.*,b.error FROM market_episodes e LEFT JOIN market_books b "
            "ON e.asset=b.asset AND e.fiat=b.fiat ORDER BY e.active DESC,e.last_checked DESC,e.id DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            age = age_seconds(row["last_checked"], now)
            state = row["state"]
            label = {"compatible": "Observada", "not_observed": "No observada en las filas consultadas",
                     "margin": "Dejó de cumplir el margen", "limits": "Límites incompatibles",
                     "funds": "Fondo configurado insuficiente",
                     "tracking_limited": "Seguimiento pausado por el límite de rutas"}.get(state, state)
            if state == "identity":
                label = "Identidad de ruta insuficiente o ambigua"
            result.append({**dict(row), "route": json.loads(row["payload"]),
                           "age_s": age, "stale": age >= freshness_s,
                           "label": "Sin datos recientes" if age >= freshness_s else label})
        return result
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return []
    finally:
        conn.close()

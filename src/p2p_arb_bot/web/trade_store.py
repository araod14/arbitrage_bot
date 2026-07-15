"""Registro de las operaciones que el usuario ejecutó (o intentó) a mano.

Vive en un fichero SQLite APARTE del que escribe el bot, y no por gusto: SQLite
bloquea el fichero entero, no la tabla, y el bot no activa WAL. Si el dashboard
escribiera en ``opportunities.db`` competiría con los INSERT del bot y saltaría
``database is locked``. Con dos ficheros, cada proceso escribe el suyo y no hay
contención.

A diferencia de ``db_reader`` —que tiene prohibido crear o migrar la base del
bot— este módulo ES el dueño de la suya y sí crea su esquema.

Cada fila guarda un SNAPSHOT de lo que el bot estimó, no solo el id de la
oportunidad: el botón Limpiar hace ``DELETE FROM opportunities``, así que apuntar
al id dejaría el registro sin base de comparación en cuanto el usuario limpiara.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from ..domain.arbitrage import realized_pnl
from .db_reader import to_local

logger = logging.getLogger(__name__)

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

#: Motivos de "no pude" que ofrece el formulario. La clave se persiste.
FAILURE_REASONS = {
    "too_late": "No llegué a tiempo / el anuncio expiró",
    "no_funds": "No tenía fondos disponibles",
    "no_response": "La contraparte no respondió",
    "bad_price": "Cancelé por precio",
    "other": "Otro",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status          TEXT    NOT NULL,
    recorded_at     TEXT    NOT NULL,
    opportunity_id  INTEGER,
    -- snapshot de lo que estimó el bot (sobrevive a "Limpiar")
    detected_at     TEXT,
    asset           TEXT,
    fiat            TEXT,
    est_net_pct     REAL,
    est_profit_usdt TEXT,
    -- lo que pasó de verdad
    real_buy_price  TEXT,
    real_sell_price TEXT,
    real_usdt       TEXT,
    real_fiat_buy   TEXT,
    real_fiat_sell  TEXT,
    -- contrapartes
    buy_advertiser  TEXT,
    sell_advertiser TEXT,
    buy_pay_method  TEXT,
    sell_pay_method TEXT,
    failure_reason  TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_recorded_at ON trades(recorded_at);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _dec(value: object) -> Decimal:
    """Texto del formulario → Decimal. Vacío/basura => 0, nunca revienta."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value).strip().replace(",", ".") or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def save_trade(db_path: str, data: dict) -> int | None:
    """Guarda una operación (completada o fallida). Devuelve el id, o None si falla.

    ``data`` viene del formulario ya normalizado por la ruta; los montos se
    persisten como texto de ``Decimal``, igual que hace el repositorio del bot.
    """
    row = {
        "status": data.get("status") or STATUS_COMPLETED,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "opportunity_id": data.get("opportunity_id"),
        "detected_at": data.get("detected_at"),
        "asset": data.get("asset"),
        "fiat": data.get("fiat"),
        "est_net_pct": data.get("est_net_pct"),
        "est_profit_usdt": data.get("est_profit_usdt"),
        "real_buy_price": str(_dec(data.get("real_buy_price"))),
        "real_sell_price": str(_dec(data.get("real_sell_price"))),
        "real_usdt": str(_dec(data.get("real_usdt"))),
        "real_fiat_buy": str(_dec(data.get("real_fiat_buy"))),
        "real_fiat_sell": str(_dec(data.get("real_fiat_sell"))),
        "buy_advertiser": data.get("buy_advertiser"),
        "sell_advertiser": data.get("sell_advertiser"),
        "buy_pay_method": data.get("buy_pay_method"),
        "sell_pay_method": data.get("sell_pay_method"),
        "failure_reason": data.get("failure_reason"),
        "notes": data.get("notes"),
    }
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        logger.warning("No se pudo abrir el registro de operaciones: %s", exc)
        return None
    try:
        cur = conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})", tuple(row.values())
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as exc:
        logger.warning("Error guardando la operación: %s", exc)
        return None
    finally:
        conn.close()


def _enrich(row: dict) -> dict:
    """Añade la ganancia real y la desviación contra lo estimado.

    Las claves de PnL se definen SIEMPRE (a ``None`` si la operación no se cerró):
    en Jinja una clave ausente es ``Undefined``, y ``Undefined is not none`` evalúa
    a ``True``, así que una plantilla que filtre por ``is not none`` entraría en la
    rama y reventaría al comparar. Definirlas siempre hace el contrato explícito.
    """
    row["failure_label"] = FAILURE_REASONS.get(row.get("failure_reason") or "")
    row["detected_local"] = to_local(row.get("detected_at")) if row.get("detected_at") else None
    if row.get("status") != STATUS_COMPLETED:
        row["profit_fiat"] = None
        row["profit_usdt"] = None
        row["net_pct"] = None
        row["net_pct_delta"] = None
        return row

    pnl = realized_pnl(
        fiat_in=_dec(row.get("real_fiat_buy")),
        fiat_out=_dec(row.get("real_fiat_sell")),
        usdt=_dec(row.get("real_usdt")),
    )
    row["profit_fiat"] = float(pnl.profit_fiat)
    row["profit_usdt"] = float(pnl.profit_usdt)
    row["net_pct"] = float(pnl.net_pct)
    est = row.get("est_net_pct")
    # Cuánto se desvió lo real de lo que prometía el bot: negativo = se evaporó.
    row["net_pct_delta"] = float(pnl.net_pct) - float(est) if est is not None else None
    return row


def recent_trades(db_path: str, limit: int = 50) -> list[dict]:
    """Últimas operaciones registradas, más recientes primero."""
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        logger.warning("No se pudo abrir el registro de operaciones: %s", exc)
        return []
    try:
        cur = conn.execute(
            "SELECT * FROM trades ORDER BY recorded_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [_enrich(dict(row)) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        logger.warning("Error leyendo operaciones: %s", exc)
        return []
    finally:
        conn.close()


def summary(db_path: str) -> dict:
    """Resumen estimado-vs-real sobre todo el histórico.

    Es la pregunta que justifica todo esto: de lo detectado, ¿cuánto llegaste a
    ejecutar y cuánto ganaste de verdad frente a lo que el bot prometía?
    """
    trades = recent_trades(db_path, limit=10_000)
    done = [t for t in trades if t.get("status") == STATUS_COMPLETED]
    failed = [t for t in trades if t.get("status") == STATUS_FAILED]
    total = len(trades)
    deltas = [t["net_pct_delta"] for t in done if t.get("net_pct_delta") is not None]
    return {
        "total": total,
        "completed": len(done),
        "failed": len(failed),
        # De lo que intentaste registrar, qué fracción cerraste.
        "hit_rate": (len(done) / total * 100) if total else None,
        "profit_usdt": sum(t.get("profit_usdt", 0.0) for t in done),
        "avg_net_pct": (sum(t["net_pct"] for t in done) / len(done)) if done else None,
        # Media de cuánto se desvió lo real de lo estimado (negativo = el bot
        # promete más de lo que la realidad entrega).
        "avg_net_pct_delta": (sum(deltas) / len(deltas)) if deltas else None,
    }

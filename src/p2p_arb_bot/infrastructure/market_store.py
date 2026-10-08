"""Observaciones de mercado. Solo el bot crea/migra y escribe estas tablas."""

from datetime import datetime, timedelta
import sqlite3

from ..domain.market import evaluate_routes
from ..domain.models import Ad, WatchTarget
from .market_codec import dumps, target_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_books (
    asset TEXT NOT NULL, fiat TEXT NOT NULL, checked_at TEXT NOT NULL,
    attempted_at TEXT NOT NULL, error TEXT, payload TEXT NOT NULL,
    PRIMARY KEY(asset, fiat)
);
CREATE TABLE IF NOT EXISTS market_episodes (
    id INTEGER PRIMARY KEY, route_key TEXT NOT NULL,
    asset TEXT NOT NULL, fiat TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_checked TEXT NOT NULL,
    last_favorable TEXT, state TEXT NOT NULL, payload TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_active
    ON market_episodes(route_key) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_market_episodes_time ON market_episodes(last_checked);
CREATE TABLE IF NOT EXISTS market_observations (
    id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL,
    checked_at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_observations_time ON market_observations(checked_at);
"""


class MarketStore:
    def __init__(self, path: str, *, retention_days: int = 7, max_routes: int = 50) -> None:
        self._conn = sqlite3.connect(path, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._retention_days = retention_days
        self._max_routes = max_routes

    def record(self, target: WatchTarget, buys: list[Ad], sells: list[Ad], now: datetime) -> None:
        stamp = now.isoformat()
        routes = evaluate_routes(buys, sells, target)
        by_key = {r.key: r for r in routes}
        previous = {r["route_key"]: r for r in self._conn.execute(
            "SELECT * FROM market_episodes WHERE asset=? AND fiat=? AND active=1",
            (target.asset, target.fiat))}
        # Reevaluar todas las rutas seguidas, aunque otra sea ahora la mejor.
        # Priorizar continuidad; las rutas nuevas ocupan los huecos disponibles.
        continuing = {r.key for r in [r for r in routes if r.key in previous and r.state == "compatible"][:self._max_routes]}
        slots = max(0, self._max_routes - len(continuing))
        selected = list(previous)
        selected += [r.key for r in routes if r.state == "compatible" and r.key not in previous][:slots]
        with self._conn:
            self._conn.execute(
                "INSERT INTO market_books VALUES(?,?,?,?,NULL,?) "
                "ON CONFLICT(asset,fiat) DO UPDATE SET checked_at=excluded.checked_at, "
                "attempted_at=excluded.attempted_at,error=NULL,payload=excluded.payload",
                (target.asset, target.fiat, stamp, stamp,
                 dumps({"target": target_dict(target), "buys": buys, "sells": sells})))
            for key in selected:
                route = by_key.get(key)
                old = previous.get(key)
                state = route.state if route else "not_observed"
                if old and state == "compatible" and key not in continuing:
                    state = "tracking_limited"
                if state != "compatible" and old is None:
                    continue
                payload = dumps(route) if route else old["payload"]
                if old is None:
                    cursor = self._conn.execute(
                        "INSERT INTO market_episodes(route_key,asset,fiat,first_seen,last_checked,"
                        "last_favorable,state,payload) VALUES(?,?,?,?,?,?,?,?)",
                        (key, target.asset, target.fiat, stamp, stamp, stamp, state, payload))
                    episode_id = cursor.lastrowid
                else:
                    episode_id = old["id"]
                    self._conn.execute(
                        "UPDATE market_episodes SET last_checked=?,last_favorable=?,state=?,"
                        "payload=?,active=? WHERE id=?",
                        (stamp, stamp if state == "compatible" else old["last_favorable"],
                         state, payload, int(state == "compatible"), episode_id))
                # Un libro estable refresca la vigencia sin multiplicar snapshots.
                if old is None or old["payload"] != payload or old["state"] != state:
                    self._conn.execute(
                        "INSERT INTO market_observations(episode_id,checked_at,state,payload) VALUES(?,?,?,?)",
                        (episode_id, stamp, state, payload))
            cutoff = (now - timedelta(days=self._retention_days)).isoformat()
            self._conn.execute("DELETE FROM market_observations WHERE checked_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM market_episodes WHERE last_checked < ?", (cutoff,))
            self._conn.execute("DELETE FROM market_books WHERE attempted_at < ?", (cutoff,))

    def failed(self, target: WatchTarget, now: datetime) -> None:
        # No sustituir el libro bueno ni interpretar un timeout como libro vacío.
        with self._conn:
            self._conn.execute(
                "INSERT INTO market_books VALUES(?,?,'',?,?,?) ON CONFLICT(asset,fiat) "
                "DO UPDATE SET attempted_at=excluded.attempted_at,error=excluded.error",
                (target.asset, target.fiat, now.isoformat(), "No se pudo consultar el mercado.",
                 dumps({"target": target_dict(target), "buys": [], "sells": []})))

    def close(self) -> None:
        self._conn.close()

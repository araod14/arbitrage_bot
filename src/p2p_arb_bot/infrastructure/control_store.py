"""Cola privada con leases y transacciones breves, compartida entre procesos."""

from contextlib import contextmanager
from collections.abc import Iterator
import json
import sqlite3
import time
from uuid import uuid4

from .market_codec import dumps

_SCHEMA = """
CREATE TABLE IF NOT EXISTS revalidations (
    id TEXT PRIMARY KEY, route_key TEXT NOT NULL, created REAL NOT NULL,
    expires REAL NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
    result TEXT, lease TEXT, lease_until REAL
);
CREATE INDEX IF NOT EXISTS idx_revalidation_created ON revalidations(created);
"""


class ControlStore:
    def __init__(self, path: str, *, ttl_s: int = 120, cooldown_s: int = 10,
                 retention_days: int = 7, max_pending: int = 10) -> None:
        self.path = path
        self.ttl_s = ttl_s
        self.cooldown_s = cooldown_s
        self.retention_days = retention_days
        self.max_pending = max_pending

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            yield conn
        finally:
            conn.close()

    def _expire(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute("UPDATE revalidations SET state='expired' WHERE expires<=? "
                     "AND state IN ('pending','processing')", (now,))
        conn.execute("DELETE FROM revalidations WHERE created < ?",
                     (now - self.retention_days * 86400,))

    def enqueue(self, payload: dict) -> str:
        now = time.time()
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, now)
            existing = conn.execute(
                "SELECT id,payload FROM revalidations WHERE route_key=? "
                "AND state IN ('pending','processing') ORDER BY created DESC LIMIT 1",
                (payload["route_key"],)).fetchone()
            if existing:
                if existing["payload"] == dumps(payload):
                    return existing["id"]
                raise ValueError("Ya hay una comprobación de esta ruta en curso.")
            count = conn.execute("SELECT COUNT(*) FROM revalidations WHERE state IN ('pending','processing')").fetchone()[0]
            latest = conn.execute("SELECT MAX(created) FROM revalidations").fetchone()[0]
            if count >= self.max_pending:
                raise ValueError("Hay demasiadas comprobaciones pendientes.")
            if latest is not None and now - latest < self.cooldown_s:
                raise ValueError("Espera unos segundos antes de solicitar otra comprobación.")
            request_id = uuid4().hex
            conn.execute("INSERT INTO revalidations(id,route_key,created,expires,state,payload) "
                         "VALUES(?,?,?,?, 'pending',?)",
                         (request_id, payload["route_key"], now, now + self.ttl_s, dumps(payload)))
            return request_id

    def claim(self) -> dict | None:
        now = time.time()
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, now)
            row = conn.execute(
                "SELECT * FROM revalidations WHERE state='pending' OR "
                "(state='processing' AND lease_until<=?) ORDER BY created LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            lease = uuid4().hex
            conn.execute("UPDATE revalidations SET state='processing',lease=?,lease_until=? WHERE id=?",
                         (lease, now + 30, row["id"]))
            return {"id": row["id"], "lease": lease, "payload": json.loads(row["payload"])}

    def finish(self, request: dict, result: dict, *, error: bool = False) -> None:
        with self._connect() as conn, conn:
            now = time.time()
            self._expire(conn, now)
            row = conn.execute("SELECT created FROM revalidations WHERE id=?", (request["id"],)).fetchone()
            result = {**result, "latency_s": max(0, now - row["created"])} if row else result
            conn.execute("UPDATE revalidations SET state=?,result=? WHERE id=? AND lease=? "
                         "AND state='processing'",
                         ("error" if error else "completed", dumps(result), request["id"], request["lease"]))

    def recent(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn, conn:
            self._expire(conn, time.time())
            rows = conn.execute("SELECT * FROM revalidations ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"]),
                     "result": json.loads(r["result"]) if r["result"] else None} for r in rows]

    def metrics(self) -> dict:
        """Resumen del periodo retenido, sin sumar ganancias de detecciones."""
        with self._connect() as conn, conn:
            self._expire(conn, time.time())
            counts = {r["state"]: r["n"] for r in conn.execute(
                "SELECT state,COUNT(*) AS n FROM revalidations GROUP BY state")}
            results = [json.loads(r[0]) for r in conn.execute(
                "SELECT result FROM revalidations WHERE state='completed' AND result IS NOT NULL")]
            favorable = sum(r.get("state") == "compatible" for r in results)
            reasons = {}
            for result in results:
                state = result.get("state", "unknown")
                reasons[state] = reasons.get(state, 0) + 1
            times = [r["latency_s"] for r in results if "latency_s" in r]
            return {"total": sum(counts.values()), "completed": len(results), "favorable": favorable,
                    "favorable_pct": favorable * 100 / len(results) if results else None,
                    "avg_latency_s": sum(times) / len(times) if times else None,
                    "reasons": reasons, "retention_days": self.retention_days}

    def get(self, request_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM revalidations WHERE id=?", (request_id,)).fetchone()
            if row is None:
                return None
            return {**dict(row), "payload": json.loads(row["payload"]),
                    "result": json.loads(row["result"]) if row["result"] else None}

"""Cuentas y reservas privadas del operador; SQLite propio del dashboard."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Iterator
import json
import sqlite3

from ..domain.market import FundAccount, decimal_amount

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_accounts (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, fiat TEXT NOT NULL,
    balance TEXT NOT NULL, methods TEXT NOT NULL, can_receive INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fund_reservations (
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES fund_accounts(id),
    amount TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
    released_at TEXT, source_request TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS fund_changes (
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
    changed_at TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fund_revision (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO fund_revision VALUES(1,0);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FundStore:
    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            yield conn
        finally:
            conn.close()

    def _reserved(self, conn: sqlite3.Connection, account_id: int) -> Decimal:
        return sum((decimal_amount(r[0]) for r in conn.execute(
            "SELECT amount FROM fund_reservations WHERE account_id=? AND released_at IS NULL",
            (account_id,))), decimal_amount("0"))

    def _account(self, conn: sqlite3.Connection, row: sqlite3.Row) -> FundAccount:
        return FundAccount(row["id"], row["name"], row["fiat"], decimal_amount(row["balance"]),
                           self._reserved(conn, row["id"]), tuple(json.loads(row["methods"])),
                           bool(row["can_receive"]), row["version"], row["updated_at"])

    def _check_revision(self, conn: sqlite3.Connection, expected: int | None) -> None:
        if expected is not None and conn.execute("SELECT version FROM fund_revision WHERE id=1").fetchone()[0] != expected:
            raise ValueError("Los fondos cambiaron. Actualiza la página e inténtalo de nuevo.")

    def _change(self, conn: sqlite3.Connection, account_id: int, action: str, payload: dict) -> None:
        conn.execute("UPDATE fund_revision SET version=version+1 WHERE id=1")
        conn.execute("INSERT INTO fund_changes(account_id,changed_at,action,payload) VALUES(?,?,?,?)",
                     (account_id, _now(), action, json.dumps(payload, ensure_ascii=False)))

    def snapshot(self) -> tuple[list[FundAccount], int]:
        with self._connect() as conn, conn:
            conn.execute("BEGIN")
            accounts = [self._account(conn, r) for r in conn.execute(
                "SELECT * FROM fund_accounts WHERE archived=0 ORDER BY name,id")]
            revision = conn.execute("SELECT version FROM fund_revision WHERE id=1").fetchone()[0]
            return accounts, revision

    def save(self, *, name: str, fiat: str, balance: str, methods: str,
             can_receive: bool, account_id: int = 0, expected_version: int = 0) -> int:
        amount = decimal_amount(balance)
        chosen = tuple(dict.fromkeys(m.strip() for m in methods.split(",") if m.strip()))
        if not name.strip() or not fiat.strip() or not chosen:
            raise ValueError("Nombre, moneda y al menos un método son obligatorios.")
        if len(name) > 100 or len(fiat) > 10 or len(methods) > 1000:
            raise ValueError("Los datos de la cuenta son demasiado largos.")
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if account_id:
                row = conn.execute("SELECT * FROM fund_accounts WHERE id=? AND archived=0", (account_id,)).fetchone()
                if row is None or row["version"] != expected_version:
                    raise ValueError("La cuenta cambió. Actualiza la página.")
                reserved = self._reserved(conn, account_id)
                if amount < reserved:
                    raise ValueError("El saldo no puede ser menor que las reservas activas.")
                if fiat.strip().upper() != row["fiat"]:
                    raise ValueError("La moneda de una cuenta existente no se puede cambiar.")
                conn.execute("UPDATE fund_accounts SET name=?,balance=?,methods=?,can_receive=?,"
                             "version=version+1,updated_at=? WHERE id=?",
                             (name.strip(), str(amount), json.dumps(chosen), int(can_receive), _now(), account_id))
            else:
                cursor = conn.execute("INSERT INTO fund_accounts(name,fiat,balance,methods,can_receive,updated_at) "
                                      "VALUES(?,?,?,?,?,?)",
                                      (name.strip(), fiat.strip().upper(), str(amount), json.dumps(chosen), int(can_receive), _now()))
                account_id = cursor.lastrowid
            self._change(conn, account_id, "saldo y métodos", {
                "balance": str(amount), "methods": chosen, "name": name.strip(), "can_receive": can_receive})
            return account_id

    def archive(self, account_id: int, expected_version: int) -> None:
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fund_accounts WHERE id=? AND archived=0", (account_id,)).fetchone()
            if row is None or row["version"] != expected_version:
                raise ValueError("La cuenta cambió. Actualiza la página.")
            if self._reserved(conn, account_id) > 0:
                raise ValueError("Libera las reservas antes de archivar la cuenta.")
            conn.execute("UPDATE fund_accounts SET archived=1,version=version+1 WHERE id=?", (account_id,))
            self._change(conn, account_id, "archivada", {})

    def reserve(self, account_id: int, amount: str, *, expected_version: int,
                expected_revision: int | None = None, note: str = "",
                source_request: str | None = None) -> int:
        value = decimal_amount(amount)
        if value <= 0:
            raise ValueError("La reserva debe ser mayor que cero.")
        if len(note) > 500:
            raise ValueError("La nota no puede superar 500 caracteres.")
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if source_request:
                existing = conn.execute("SELECT id,account_id,amount FROM fund_reservations WHERE source_request=?",
                                        (source_request,)).fetchone()
                if existing:
                    if existing["account_id"] != account_id or decimal_amount(existing["amount"]) != value:
                        raise ValueError("Esta comprobación ya se usó para otra reserva.")
                    return existing["id"]
            self._check_revision(conn, expected_revision)
            row = conn.execute("SELECT * FROM fund_accounts WHERE id=? AND archived=0", (account_id,)).fetchone()
            if row is None or row["version"] != expected_version:
                raise ValueError("La cuenta cambió. Actualiza la página.")
            account = self._account(conn, row)
            if value > account.available:
                raise ValueError("Saldo disponible insuficiente para esta reserva.")
            cursor = conn.execute("INSERT INTO fund_reservations(account_id,amount,note,created_at,source_request) "
                                  "VALUES(?,?,?,?,?)", (account_id, str(value), note, _now(), source_request))
            conn.execute("UPDATE fund_accounts SET version=version+1 WHERE id=?", (account_id,))
            self._change(conn, account_id, "reserva", {"amount": str(value), "reservation_id": cursor.lastrowid})
            return cursor.lastrowid

    def release(self, reservation_id: int) -> None:
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fund_reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise ValueError("La reserva no existe.")
            if row["released_at"] is not None:
                return
            conn.execute("UPDATE fund_reservations SET released_at=? WHERE id=?", (_now(), reservation_id))
            conn.execute("UPDATE fund_accounts SET version=version+1 WHERE id=?", (row["account_id"],))
            self._change(conn, row["account_id"], "liberación", {"reservation_id": reservation_id})

    def reservations(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT r.*,a.name,a.fiat FROM fund_reservations r JOIN fund_accounts a ON a.id=r.account_id "
                "WHERE released_at IS NULL ORDER BY r.id DESC")]

    def changes(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT c.*,a.name FROM fund_changes c JOIN fund_accounts a ON a.id=c.account_id ORDER BY c.id DESC LIMIT 30")]

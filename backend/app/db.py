"""SQLite-Speicher für Konfiguration, Bot-Zustand, Aufträge, Ereignisse und DEMO-Konto.

Alle Daten tragen eine Spalte ``mode`` (DEMO/PAPER), damit beide Betriebsarten
strikt getrennt bleiben. Die erste Version nutzt ausdrücklich einen einzelnen
Backend-Prozess; Zugriffe werden über ein Lock serialisiert.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    mode TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (mode, key)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT,
    broker_order_id TEXT,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT,
    notional TEXT,
    time_in_force TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_qty REAL NOT NULL DEFAULT 0,
    filled_avg_price REAL,
    est_price REAL,
    est_value REAL,
    trading_day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_checked_at TEXT,
    check_count INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    message TEXT,
    UNIQUE (mode, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_orders_mode_day ON orders (mode, trading_day);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_mode ON events (mode, id);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bar_time TEXT,
    close REAL,
    sma_short REAL,
    sma_long REAL,
    signal TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_mode ON decisions (mode, id);

-- Abgeschlossene und laufende Trades (Einstieg + Ausstieg) der Scalping-Engine
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,           -- entering | open | exiting | closed | canceled
    entry_cid TEXT,
    entry_time TEXT,
    entry_qty REAL,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    deadline TEXT,
    exit_cid TEXT,
    exit_time TEXT,
    exit_qty REAL,
    exit_price REAL,
    exit_reason TEXT,
    exit_attempts INTEGER NOT NULL DEFAULT 0,
    gross_pnl REAL,
    fees_est REAL,
    net_pnl REAL,
    fee_pct REAL,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trades_mode ON trades (mode, id);

-- Gespeicherte Backtests (zum Vergleichen von Parametern)
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    request TEXT NOT NULL,
    metrics TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_backtests_mode ON backtests (mode, id);

-- Simulierter Broker für DEMO (entspricht dem Server-Zustand bei Alpaca)
CREATE TABLE IF NOT EXISTS demo_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL,
    initial_cash REAL NOT NULL,
    last_equity REAL,
    last_equity_day TEXT
);
CREATE TABLE IF NOT EXISTS demo_positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL,
    avg_entry_price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    notional REAL,
    time_in_force TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_qty REAL NOT NULL DEFAULT 0,
    filled_avg_price REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    filled_at TEXT,
    canceled_at TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        # Beobachter für neue Ereignisse (z. B. Push-Benachrichtigungen); Fehler dort stören nie den Handel.
        self.event_listeners: list[Callable[[str, str, str, str, bool], None]] = []
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Neue Spalten in bestehenden Datenbanken ergänzen (idempotent)."""
        wanted = {
            "orders": [("order_type", "TEXT NOT NULL DEFAULT 'market'"), ("stop_price", "REAL"), ("limit_price", "REAL")],
            "trades": [("stop_cid", "TEXT"), ("stop_attempts", "INTEGER NOT NULL DEFAULT 0"), ("sl_seen_at", "TEXT")],
            "demo_orders": [("order_type", "TEXT NOT NULL DEFAULT 'market'"), ("stop_price", "REAL"), ("limit_price", "REAL")],
        }
        for table, cols in wanted.items():
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ kv
    def get(self, mode: str, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE mode=? AND key=?", (mode, key))
        return json.loads(row["value"]) if row else default

    def set(self, mode: str, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv (mode, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(mode, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (mode, key, json.dumps(value), iso(utcnow())),
        )

    # -------------------------------------------------------------- events
    def add_event(
        self, mode: str, level: str, category: str, message: str, data: dict | None = None, *, push: bool = False
    ) -> None:
        """Ereignis speichern. ``push`` markiert Meldungen, die sich als Benachrichtigung eignen."""
        self.execute(
            "INSERT INTO events (mode, ts, level, category, message, data) VALUES (?, ?, ?, ?, ?, ?)",
            (mode, iso(utcnow()), level, category, message, json.dumps(data) if data else None),
        )
        for listener in self.event_listeners:
            try:
                listener(mode, level, category, message, push)
            except Exception:  # noqa: BLE001
                pass

    def list_events(self, mode: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM events WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))
        for r in rows:
            r["data"] = json.loads(r["data"]) if r["data"] else None
        return rows

    # ----------------------------------------------------------- decisions
    def add_decision(self, mode: str, **fields: Any) -> None:
        self.execute(
            "INSERT INTO decisions (mode, ts, symbol, bar_time, close, sma_short, sma_long, signal, action, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mode,
                iso(utcnow()),
                fields.get("symbol", ""),
                fields.get("bar_time"),
                fields.get("close"),
                fields.get("sma_short"),
                fields.get("sma_long"),
                fields.get("signal", "none"),
                fields.get("action", "none"),
                fields.get("reason", ""),
            ),
        )

    def list_decisions(self, mode: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM decisions WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))

"""SQLite persistence for sandboxes, API tokens and the audit log."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    owner           TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    status          TEXT NOT NULL,
    image           TEXT NOT NULL,
    cpu             REAL NOT NULL,
    memory_mb       INTEGER NOT NULL,
    runtime         TEXT NOT NULL,
    handle          TEXT,
    labels          TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    last_active_at  REAL NOT NULL,
    terminated_at   REAL,
    terminate_reason TEXT,
    exec_count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sandboxes_status ON sandboxes(status);
CREATE INDEX IF NOT EXISTS idx_sandboxes_owner ON sandboxes(owner);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash  TEXT PRIMARY KEY,
    principal   TEXT NOT NULL,
    role        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    sandbox_id  TEXT,
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_sandbox ON audit(sandbox_id);
"""

ACTIVE_STATUSES = ("creating", "running")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("labels", "detail"):
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    def _q(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def _x(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    # --- sandboxes -------------------------------------------------------
    def insert_sandbox(self, sb: dict[str, Any]) -> None:
        cols = list(sb)
        vals = [json.dumps(v) if k == "labels" else v for k, v in sb.items()]
        self._x(
            f"INSERT INTO sandboxes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(vals),
        )

    def update_sandbox(self, sandbox_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if k == "labels" else v for k, v in fields.items()]
        self._x(f"UPDATE sandboxes SET {sets} WHERE id=?", (*vals, sandbox_id))

    def touch_sandbox(self, sandbox_id: str, exec_inc: int = 0) -> None:
        self._x(
            "UPDATE sandboxes SET last_active_at=?, exec_count=exec_count+? WHERE id=?",
            (time.time(), exec_inc, sandbox_id),
        )

    def get_sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        return rows[0] if rows else None

    def list_sandboxes(
        self,
        owner: str | None = None,
        created_by: str | None = None,
        status: str | None = None,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if owner:
            where.append("owner=?")
            params.append(owner)
        if created_by:
            where.append("created_by=?")
            params.append(created_by)
        if status:
            where.append("status=?")
            params.append(status)
        if active_only:
            where.append(f"status IN ({','.join('?' * len(ACTIVE_STATUSES))})")
            params.extend(ACTIVE_STATUSES)
        sql = "SELECT * FROM sandboxes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        return self._q(sql, (*params, limit))

    def count_active(self, owner: str | None = None) -> int:
        sql = f"SELECT COUNT(*) AS n FROM sandboxes WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})"
        params: tuple = ACTIVE_STATUSES
        if owner:
            sql += " AND owner=?"
            params = (*params, owner)
        return self._q(sql, params)[0]["n"]

    def status_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._q("SELECT status, COUNT(*) AS n FROM sandboxes GROUP BY status")}

    # --- tokens ----------------------------------------------------------
    def insert_token(self, token: str, principal: str, role: str, description: str = "") -> None:
        self._x(
            "INSERT INTO tokens (token_hash, principal, role, description, created_at) VALUES (?,?,?,?,?)",
            (hash_token(token), principal, role, description, time.time()),
        )

    def lookup_token(self, token: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM tokens WHERE token_hash=? AND revoked=0", (hash_token(token),))
        return rows[0] if rows else None

    def list_tokens(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT substr(token_hash,1,12) AS id, principal, role, description, created_at, revoked "
            "FROM tokens ORDER BY created_at DESC"
        )

    def revoke_token(self, token_id: str) -> int:
        return self._x("UPDATE tokens SET revoked=1 WHERE substr(token_hash,1,12)=?", (token_id,))

    # --- audit -----------------------------------------------------------
    def audit(self, actor: str, action: str, sandbox_id: str | None = None, **detail: Any) -> None:
        self._x(
            "INSERT INTO audit (ts, actor, action, sandbox_id, detail) VALUES (?,?,?,?,?)",
            (time.time(), actor, action, sandbox_id, json.dumps(detail, ensure_ascii=False)),
        )

    def list_audit(self, sandbox_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if sandbox_id:
            return self._q("SELECT * FROM audit WHERE sandbox_id=? ORDER BY id DESC LIMIT ?", (sandbox_id, limit))
        return self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

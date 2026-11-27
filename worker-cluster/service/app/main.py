"""Worker HTTP API: run SQL statements against task-local SQLite files, one per tenant.

Single writer per file: every statement on a tenant's file runs while holding that
tenant's lock, so only one statement touches a file at a time. This relies on
running exactly one server process (uvicorn --workers 1).
"""

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/tenants"))
TENANT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
BUSY_TIMEOUT_SECONDS = 5

app = FastAPI(title="worker")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(tenant_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(tenant_id, threading.Lock())


def _db_path(tenant_id: str) -> Path:
    # Strict allowlist so an ID can never point outside DATA_DIR.
    if not TENANT_ID.fullmatch(tenant_id):
        raise HTTPException(status_code=400, detail="invalid tenant id")
    return DATA_DIR / f"{tenant_id}.db"


# ATTACH would let a tenant open another tenant's file; PRAGMA could change
# journaling or other settings the service relies on.
_DENIED_ACTIONS = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA}


def _authorizer(action: int, *_args) -> int:
    return sqlite3.SQLITE_DENY if action in _DENIED_ACTIONS else sqlite3.SQLITE_OK


class Statement(BaseModel):
    sql: str
    params: list[Any] | dict[str, Any] = []


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/tenants/{tenant_id}", status_code=201)
def create_tenant(tenant_id: str) -> dict:
    path = _db_path(tenant_id)
    with _lock_for(tenant_id):
        if path.exists():
            raise HTTPException(status_code=409, detail="tenant already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
    return {"tenant_id": tenant_id}


@app.post("/tenants/{tenant_id}/execute")
def execute(tenant_id: str, statement: Statement) -> dict:
    path = _db_path(tenant_id)
    with _lock_for(tenant_id):
        if not path.exists():
            raise HTTPException(status_code=404, detail="tenant not found")
        # Autocommit: each request is its own transaction.
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        conn.set_authorizer(_authorizer)
        try:
            # sqlite3 rejects multiple statements in one execute() call.
            cursor = conn.execute(statement.sql, statement.params)
            columns = [col[0] for col in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            return {"columns": columns, "rows": rows, "rowcount": cursor.rowcount}
        except sqlite3.Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            conn.close()

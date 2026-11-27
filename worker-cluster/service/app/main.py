"""Worker HTTP API: run SQL statements against task-local SQLite files, one per tenant.

Guards on every /execute request, in order:

1. Tenant ID must match an allowlist. The file path is built only from that ID.
2. This task must hold the tenant's lease at the current epoch, else 409 WRONG_OWNER.
   Checked locally first (lease_until minus a safety margin, no network needed), then
   against DynamoDB (503 if unreachable), and again after taking the tenant lock.
3. Tenant status must be ACTIVE, else 403 TENANT_NOT_ACTIVE.
4. The file is opened with mode=rw, which never creates a file.
5. The SQLite authorizer denies ATTACH/DETACH, load_extension and any PRAGMA not on an
   introspection allowlist. No databases can be attached at all, which also blocks
   VACUUM INTO.
6. Limits: statement timeout, max rows returned, max database size.

Single writer per file: every statement on a tenant's file runs while holding that
tenant's lock, so only one statement touches a file at a time. This relies on
running exactly one server process (uvicorn --workers 1).

Leases: this process remembers the leases it holds, and a background thread renews them.
The lease protocol is in placement.py.
"""

import logging
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.placement import (
    Lease,
    LeaseConflict,
    LeaseLost,
    PlacementClient,
    get_placement_client,
)
from app.replication import ReplicationConfig, Replicator
from app.tenants import ACTIVE, TenantCatalog, get_tenant_catalog

log = logging.getLogger("worker")
# uvicorn configures only its own loggers; this makes the worker's lease logs visible too.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/tenants"))
TENANT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Unique per process: ownership is per process, because the locks and files are.
TASK_ID = os.environ.get("WORKER_TASK_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
LEASE_SECONDS = 30
RENEW_INTERVAL_SECONDS = 10
# Stop serving this long before our own view of lease_until, to allow for clock skew
# between this task and whoever decides the lease has expired.
LEASE_SAFETY_MARGIN_MS = 2000

BUSY_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_SECONDS = float(os.environ.get("STATEMENT_TIMEOUT_SECONDS", "5"))
MAX_ROWS = int(os.environ.get("MAX_ROWS", "1000"))
MAX_DB_BYTES = int(os.environ.get("MAX_DB_BYTES", str(100 * 1024 * 1024)))
MAX_SQL_LENGTH = 100_000
# Check the timeout every N SQLite VM instructions.
PROGRESS_STEPS = 1000


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# ---- leases held by this process -------------------------------------------------

_leases: dict[str, Lease] = {}
_leases_guard = threading.Lock()

# Litestream replication of owned tenants to S3. None when no bucket is configured
# (local docker compose, tests).
_replicator: Replicator | None = None


def _held_lease(tenant_id: str) -> Lease | None:
    with _leases_guard:
        return _leases.get(tenant_id)


def _store_lease(lease: Lease) -> None:
    with _leases_guard:
        _leases[lease.tenant_id] = lease


def _drop_lease(lease: Lease) -> None:
    with _leases_guard:
        dropped = _leases.get(lease.tenant_id) == lease
        if dropped:
            del _leases[lease.tenant_id]
    # Not the owner any more: stop writing to S3 right away.
    if dropped and _replicator is not None:
        _replicator.stop(lease.tenant_id)


def _locally_valid(lease: Lease, placement: PlacementClient) -> bool:
    """Our own view: is the lease still safely inside lease_until? Needs no network."""
    return placement.now_ms() < lease.lease_until_ms - LEASE_SAFETY_MARGIN_MS


def _still_held(tenant_id: str, lease: Lease, placement: PlacementClient) -> bool:
    """Same owner and epoch as `lease`, and locally unexpired. Renewals may have replaced
    the stored lease with a later lease_until, so compare epochs, not whole leases."""
    current = _held_lease(tenant_id)
    return (
        current is not None
        and current.epoch == lease.epoch
        and _locally_valid(current, placement)
    )


def renew_all(placement: PlacementClient) -> None:
    """Renew every held lease once (conditional on owner + epoch).

    A failed renew drops the lease at once, so the tenant stops being served.
    """
    with _leases_guard:
        held = list(_leases.values())
    for lease in held:
        try:
            renewed = placement.renew(lease, LEASE_SECONDS)
        except LeaseLost:
            log.warning("lost lease on %s at epoch %s", lease.tenant_id, lease.epoch)
            _drop_lease(lease)
        except Exception:
            # Network trouble: keep the lease while it's still valid and retry next
            # round. Once it has expired, renew can't succeed anyway, so give it up.
            log.exception("renewing lease on %s failed", lease.tenant_id)
            if not _locally_valid(lease, placement):
                log.warning("lease on %s expired while renewals failed", lease.tenant_id)
                _drop_lease(lease)
        else:
            _store_lease(renewed)

    if _replicator is not None:
        # Restart crashed replicators, stop ones for tenants no longer held.
        with _leases_guard:
            held_now = dict(_leases)
        _replicator.reconcile(held_now)


def _heartbeat(placement: PlacementClient, stop: threading.Event) -> None:
    while not stop.wait(RENEW_INTERVAL_SECONDS):
        renew_all(placement)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Fail at startup, not on the first request, if the tables aren't configured.
    placement = get_placement_client()
    catalog = get_tenant_catalog()
    if os.environ.get("CREATE_LOCAL_TABLES") == "1":
        from app.local_dev import create_local_tables

        create_local_tables(placement, catalog)

    global _replicator
    replication_config = ReplicationConfig.from_env(DATA_DIR)
    if replication_config is None:
        log.warning("TENANT_BUCKET_NAME not set: tenant databases are NOT replicated to S3")
    else:
        _replicator = Replicator(replication_config, kms_key_for=catalog.kms_key_id)

    log.info("worker task id %s", TASK_ID)
    stop = threading.Event()
    thread = threading.Thread(
        target=_heartbeat, args=(placement, stop), name="lease-heartbeat", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)
        # Final sync of every tenant to S3 while we still own them, then release.
        if _replicator is not None:
            _replicator.stop_all()
        # The files die with this task, so let another task take over right away.
        with _leases_guard:
            held = list(_leases.values())
        for lease in held:
            try:
                placement.release(lease)
            except Exception:
                log.exception("releasing lease on %s failed", lease.tenant_id)


app = FastAPI(title="worker", lifespan=lifespan)


# ---- per-tenant locks and files ----------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(tenant_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(tenant_id, threading.Lock())


def _db_path(tenant_id: str) -> Path:
    # Strict allowlist so an ID can never point outside DATA_DIR.
    if not TENANT_ID.fullmatch(tenant_id):
        raise _error(400, "INVALID_TENANT_ID", "tenant id must match [A-Za-z0-9_-]{1,64}")
    return DATA_DIR / f"{tenant_id}.db"


def _open(path: Path, *, create: bool) -> sqlite3.Connection:
    # mode=rw fails if the file is missing instead of silently creating an empty one.
    mode = "rwc" if create else "rw"
    return sqlite3.connect(
        f"{path.resolve().as_uri()}?mode={mode}",
        uri=True,
        timeout=BUSY_TIMEOUT_SECONDS,
        isolation_level=None,  # autocommit: each request is its own transaction
    )


# ---- authorizer and limits ----------------------------------------------------------

# Read-only introspection. Everything else (journal_mode, writable_schema, ...) is denied.
_SAFE_PRAGMAS = frozenset(
    {"table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
     "foreign_key_list"}
)
_SQLITE_FUNCTION = getattr(sqlite3, "SQLITE_FUNCTION", 31)


def _authorizer(action: int, arg1: str | None, arg2: str | None, *_rest) -> int:
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if (arg1 or "").lower() in _SAFE_PRAGMAS else sqlite3.SQLITE_DENY
    # For SQLITE_FUNCTION, arg2 is the function name.
    if action == _SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _apply_limits(conn: sqlite3.Connection) -> None:
    # Runs before the authorizer is installed, so the service's own PRAGMAs are allowed.
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count = {max(1, MAX_DB_BYTES // page_size)}")
    # No attached databases at all: blocks ATTACH and VACUUM INTO (which attaches its target).
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_LENGTH)
    conn.set_authorizer(_authorizer)
    deadline = time.monotonic() + STATEMENT_TIMEOUT_SECONDS
    # A non-zero return interrupts the statement with SQLITE_INTERRUPT.
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), PROGRESS_STEPS)


# ---- request guard ------------------------------------------------------------------


def _wrong_owner() -> HTTPException:
    return _error(409, "WRONG_OWNER", "this task does not own the tenant")


def _guard(
    tenant_id: str, placement: PlacementClient, catalog: TenantCatalog
) -> tuple[Path, Lease]:
    path = _db_path(tenant_id)

    # Local check first: an expired lease is refused even if DynamoDB is unreachable.
    lease = _held_lease(tenant_id)
    if lease is None or not _locally_valid(lease, placement):
        if lease is not None:
            _drop_lease(lease)
        raise _wrong_owner()

    try:
        holds = placement.holds(lease)
        status = catalog.get_status(tenant_id)
    except Exception:
        # Can't confirm ownership, so don't serve. Not WRONG_OWNER: we may still own it.
        log.exception("placement/tenants lookup failed for %s", tenant_id)
        raise _error(503, "PLACEMENT_UNAVAILABLE", "cannot verify tenant ownership") from None

    if not holds:
        _drop_lease(lease)
        raise _wrong_owner()
    if status != ACTIVE:
        raise _error(403, "TENANT_NOT_ACTIVE", "tenant is not active")

    return path, lease


# ---- routes -----------------------------------------------------------------------


class Statement(BaseModel):
    sql: str
    params: list[Any] | dict[str, Any] = []


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "task_id": TASK_ID}


@app.post("/tenants/{tenant_id}", status_code=201)
def create_tenant(
    tenant_id: str,
    placement: PlacementClient = Depends(get_placement_client),
    catalog: TenantCatalog = Depends(get_tenant_catalog),
) -> dict:
    """STAND-IN for control-plane provisioning: take ownership, mark ACTIVE, create the file.

    Once the placement controller assigns owners, this should only create the file.
    """
    path = _db_path(tenant_id)
    with _lock_for(tenant_id):
        if path.exists():
            raise _error(409, "TENANT_EXISTS", "tenant already exists on this task")

        lease = _held_lease(tenant_id)
        if lease is None or not placement.holds(lease):
            try:
                lease = placement.acquire(tenant_id, TASK_ID, LEASE_SECONDS)
            except LeaseConflict:
                raise _error(409, "WRONG_OWNER", "another task owns the tenant") from None
            _store_lease(lease)

        catalog.register(tenant_id)

        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open(path, create=True)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()

        if _replicator is not None:
            _replicator.start(lease)
    return {"tenant_id": tenant_id, "owner_task": TASK_ID, "epoch": lease.epoch}


@app.post("/tenants/{tenant_id}/execute")
def execute(
    tenant_id: str,
    statement: Statement,
    placement: PlacementClient = Depends(get_placement_client),
    catalog: TenantCatalog = Depends(get_tenant_catalog),
) -> dict:
    path, lease = _guard(tenant_id, placement, catalog)
    with _lock_for(tenant_id):
        # The lease may have been lost while this request waited for the lock.
        if not _still_held(tenant_id, lease, placement):
            raise _wrong_owner()
        try:
            conn = _open(path, create=False)
        except sqlite3.OperationalError:
            raise _error(404, "TENANT_NOT_FOUND", "tenant has no database on this task") from None
        try:
            _apply_limits(conn)
            # sqlite3 rejects multiple statements in one execute() call.
            cursor = conn.execute(statement.sql, statement.params)
            columns = [col[0] for col in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(MAX_ROWS + 1)
            if len(rows) > MAX_ROWS:
                raise _error(
                    400, "ROW_LIMIT", f"result has more than {MAX_ROWS} rows; add a LIMIT"
                )
            return {"columns": columns, "rows": rows, "rowcount": cursor.rowcount}
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if code == sqlite3.SQLITE_INTERRUPT:
                raise _error(
                    408, "STATEMENT_TIMEOUT", f"statement ran over {STATEMENT_TIMEOUT_SECONDS}s"
                ) from exc
            if code == sqlite3.SQLITE_FULL:
                raise _error(413, "DB_SIZE_LIMIT", f"database would exceed {MAX_DB_BYTES} bytes") from exc
            raise _error(400, "SQL_ERROR", str(exc)) from exc
        finally:
            conn.close()

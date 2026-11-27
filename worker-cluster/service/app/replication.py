"""Litestream replication: one `litestream replicate` child process per tenant this task owns.

Why one process per tenant: Litestream (v0.5) can't reload its config without a restart, and
its directory-watch mode uses one replica path and one KMS key for every database. Each
tenant here needs its own S3 path (tenants/{id}/gen-{epoch}/) and possibly its own KMS key,
so each tenant gets its own config file and process.

Desired state = the leases this task holds. The worker calls:
    start(lease)   after the tenant's file exists and this task owns it
    stop(tenant)   the moment the lease is lost or released (SIGTERM, no waiting)
    reconcile()    every heartbeat: restart crashed processes, reap stopped ones
    stop_all()     on shutdown: SIGTERM all and wait, so Litestream does its final sync

KMS key for a tenant, first match wins:
    1. the tenant's own key from the tenants table (kms_key_for callback)
    2. ReplicationConfig.default_kms_key_id (LITESTREAM_DEFAULT_KMS_KEY_ID)
    3. none: the bucket's default encryption applies (aws/s3)
"""

import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.placement import Lease

log = logging.getLogger("worker")

TENANTS_PREFIX = "tenants/"  # must match infra/worker_cluster/storage_layout.py
STOP_TIMEOUT_SECONDS = 35  # > Litestream's default 30s shutdown-sync-timeout


def replica_path(tenant_id: str, epoch: int) -> str:
    """S3 key prefix for a tenant's generation. Same as storage_layout.generation_prefix."""
    return f"{TENANTS_PREFIX}{tenant_id}/gen-{epoch}"


@dataclass(frozen=True)
class ReplicationConfig:
    bucket: str
    region: str | None
    config_dir: Path
    data_dir: Path
    binary: str = "litestream"
    sync_interval: str = "1s"
    default_kms_key_id: str | None = None

    @classmethod
    def from_env(cls, data_dir: Path) -> "ReplicationConfig | None":
        """None when TENANT_BUCKET_NAME isn't set (e.g. local docker compose): no replication."""
        bucket = os.environ.get("TENANT_BUCKET_NAME")
        if not bucket:
            return None
        return cls(
            bucket=bucket,
            region=os.environ.get("TENANT_BUCKET_REGION") or os.environ.get("AWS_REGION"),
            config_dir=Path(os.environ.get("LITESTREAM_CONFIG_DIR", "/data/litestream")),
            data_dir=data_dir,
            binary=os.environ.get("LITESTREAM_BIN", "litestream"),
            sync_interval=os.environ.get("LITESTREAM_SYNC_INTERVAL", "1s"),
            default_kms_key_id=os.environ.get("LITESTREAM_DEFAULT_KMS_KEY_ID") or None,
        )


@dataclass
class _Running:
    epoch: int
    proc: subprocess.Popen
    config_path: Path


@dataclass
class _Stopping:
    proc: subprocess.Popen
    deadline: float = field(default_factory=lambda: time.monotonic() + STOP_TIMEOUT_SECONDS)


class Replicator:
    def __init__(
        self,
        config: ReplicationConfig,
        *,
        kms_key_for: Callable[[str], str | None] = lambda _tenant: None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.config = config
        self._kms_key_for = kms_key_for
        self._popen = popen
        self._running: dict[str, _Running] = {}
        self._stopping: list[_Stopping] = []
        self._guard = threading.Lock()

    # ---- config -----------------------------------------------------------------

    def _kms_key(self, tenant_id: str) -> str | None:
        return self._kms_key_for(tenant_id) or self.config.default_kms_key_id

    def build_config(self, tenant_id: str, epoch: int) -> dict:
        replica: dict = {
            "type": "s3",
            "bucket": self.config.bucket,
            "path": replica_path(tenant_id, epoch),
            "sync-interval": self.config.sync_interval,
        }
        if self.config.region:
            replica["region"] = self.config.region
        key = self._kms_key(tenant_id)
        if key:
            # YAML key, not a URL parameter: Litestream ignores SSE params in a config url.
            replica["sse-kms-key-id"] = key
        return {
            "dbs": [
                {"path": str(self.config.data_dir / f"{tenant_id}.db"), "replica": replica}
            ]
        }

    def _write_config(self, tenant_id: str, epoch: int) -> Path:
        self.config.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.config_dir / f"{tenant_id}.yml"
        # JSON is valid YAML, so no YAML library is needed.
        path.write_text(json.dumps(self.build_config(tenant_id, epoch), indent=2))
        return path

    # ---- lifecycle --------------------------------------------------------------

    def start(self, lease: Lease) -> None:
        """Replicate the tenant at the lease's epoch. No-op if already doing exactly that."""
        tenant_id = lease.tenant_id
        db = self.config.data_dir / f"{tenant_id}.db"
        with self._guard:
            current = self._running.get(tenant_id)
            if current and current.epoch == lease.epoch and current.proc.poll() is None:
                return
            if current:
                # New epoch, or the process died: replace it.
                self._signal_stop(self._running.pop(tenant_id))
            if not db.exists():
                log.warning("not replicating %s: %s does not exist", tenant_id, db)
                return
            config_path = self._write_config(tenant_id, lease.epoch)
            proc = self._popen([self.config.binary, "replicate", "-config", str(config_path)])
            self._running[tenant_id] = _Running(lease.epoch, proc, config_path)
        log.info(
            "replicating %s to s3://%s/%s",
            tenant_id,
            self.config.bucket,
            replica_path(tenant_id, lease.epoch),
        )

    def stop(self, tenant_id: str) -> None:
        """Stop replicating a tenant now. Doesn't wait; reconcile() reaps the process."""
        with self._guard:
            running = self._running.pop(tenant_id, None)
            if running:
                self._signal_stop(running)
        if running:
            log.info("stopped replicating %s (epoch %s)", tenant_id, running.epoch)

    def _signal_stop(self, running: _Running) -> None:
        if running.proc.poll() is None:
            running.proc.send_signal(signal.SIGTERM)
            self._stopping.append(_Stopping(running.proc))
        running.config_path.unlink(missing_ok=True)

    def reconcile(self, leases: dict[str, Lease]) -> None:
        """Make running processes match `leases`: start missing or crashed ones, stop extras,
        and reap (or kill, past the deadline) processes that were told to stop."""
        with self._guard:
            extra = [t for t in self._running if t not in leases]
            crashed = [t for t, r in self._running.items() if r.proc.poll() is not None]
        for tenant_id in extra:
            self.stop(tenant_id)
        for tenant_id in crashed:
            log.warning("litestream for %s exited; restarting", tenant_id)
        for lease in leases.values():
            try:
                self.start(lease)
            except Exception:
                # E.g. the KMS key lookup failed. Retried on the next heartbeat; one bad
                # tenant must not stop the others (or the heartbeat thread).
                log.exception("starting replication for %s failed", lease.tenant_id)
        self._reap()

    def _reap(self) -> None:
        with self._guard:
            still: list[_Stopping] = []
            for s in self._stopping:
                if s.proc.poll() is not None:
                    continue
                if time.monotonic() > s.deadline:
                    s.proc.kill()
                    continue
                still.append(s)
            self._stopping = still

    def stop_all(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Shutdown: SIGTERM everything and wait, so Litestream can do its final sync."""
        with self._guard:
            for running in self._running.values():
                self._signal_stop(running)
            self._running.clear()
            procs = [s.proc for s in self._stopping]
            self._stopping = []
        deadline = time.monotonic() + timeout
        for proc in procs:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()

    def running_epochs(self) -> dict[str, int]:
        with self._guard:
            return {t: r.epoch for t, r in self._running.items()}

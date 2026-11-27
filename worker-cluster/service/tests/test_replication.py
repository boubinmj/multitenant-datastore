"""Replicator tests with a fake process launcher: no Litestream binary or S3 needed."""

import json
import signal

import pytest

from app import main
from app.placement import Lease
from app.replication import ReplicationConfig, Replicator, replica_path


class FakeProc:
    def __init__(self, args):
        self.args = args
        self.returncode = None
        self.signals = []
        self.killed = False

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def crash(self):
        self.returncode = 1


class FakeLauncher:
    def __init__(self):
        self.procs: list[FakeProc] = []

    def __call__(self, args):
        proc = FakeProc(args)
        self.procs.append(proc)
        return proc


def lease(tenant_id="acme", epoch=1):
    return Lease(tenant_id, "task-a", epoch, 10**12)


@pytest.fixture
def launcher():
    return FakeLauncher()


@pytest.fixture
def config(tmp_path):
    data = tmp_path / "tenants"
    data.mkdir()
    return ReplicationConfig(
        bucket="tenant-bucket",
        region="us-east-2",
        config_dir=tmp_path / "litestream",
        data_dir=data,
    )


@pytest.fixture
def make_replicator(config, launcher):
    def make(kms_key_for=lambda _t: None, default_key=None):
        cfg = config
        if default_key:
            cfg = ReplicationConfig(**{**config.__dict__, "default_kms_key_id": default_key})
        return Replicator(cfg, kms_key_for=kms_key_for, popen=launcher)

    return make


def touch_db(config, tenant_id="acme"):
    (config.data_dir / f"{tenant_id}.db").touch()


def written_config(proc) -> dict:
    # args: [binary, "replicate", "-config", path]
    with open(proc.args[3]) as f:
        return json.load(f)


def test_replica_path_is_generation_prefix():
    assert replica_path("acme", 3) == "tenants/acme/gen-3"


def test_start_writes_per_tenant_config_and_launches(make_replicator, config, launcher):
    touch_db(config)
    make_replicator().start(lease(epoch=3))

    [proc] = launcher.procs
    assert proc.args[:3] == ["litestream", "replicate", "-config"]
    cfg = written_config(proc)
    [db] = cfg["dbs"]
    assert db["path"] == str(config.data_dir / "acme.db")
    assert db["replica"] == {
        "type": "s3",
        "bucket": "tenant-bucket",
        "path": "tenants/acme/gen-3",
        "sync-interval": "1s",
        "region": "us-east-2",
    }


def test_tenant_kms_key_wins_over_default(make_replicator, config, launcher):
    touch_db(config)
    r = make_replicator(kms_key_for=lambda t: f"arn:key/{t}", default_key="arn:key/default")
    r.start(lease())
    assert written_config(launcher.procs[0])["dbs"][0]["replica"]["sse-kms-key-id"] == "arn:key/acme"


def test_default_kms_key_when_tenant_has_none(make_replicator, config, launcher):
    touch_db(config)
    make_replicator(default_key="arn:key/default").start(lease())
    assert written_config(launcher.procs[0])["dbs"][0]["replica"]["sse-kms-key-id"] == "arn:key/default"


def test_no_kms_key_leaves_bucket_default(make_replicator, config, launcher):
    touch_db(config)
    make_replicator().start(lease())
    assert "sse-kms-key-id" not in written_config(launcher.procs[0])["dbs"][0]["replica"]


def test_start_is_idempotent_for_same_epoch(make_replicator, config, launcher):
    touch_db(config)
    r = make_replicator()
    r.start(lease(epoch=1))
    r.start(lease(epoch=1))
    assert len(launcher.procs) == 1


def test_new_epoch_restarts_to_new_generation(make_replicator, config, launcher):
    touch_db(config)
    r = make_replicator()
    r.start(lease(epoch=1))
    r.start(lease(epoch=2))

    old, new = launcher.procs
    assert old.signals == [signal.SIGTERM]
    assert written_config(new)["dbs"][0]["replica"]["path"] == "tenants/acme/gen-2"
    assert r.running_epochs() == {"acme": 2}


def test_missing_db_file_is_not_replicated(make_replicator, launcher):
    make_replicator().start(lease())
    assert launcher.procs == []


def test_stop_signals_immediately_and_removes_config(make_replicator, config, launcher):
    touch_db(config)
    r = make_replicator()
    r.start(lease())
    config_file = launcher.procs[0].args[3]

    r.stop("acme")
    assert launcher.procs[0].signals == [signal.SIGTERM]
    assert r.running_epochs() == {}
    assert not (config.config_dir / "acme.yml").exists()
    assert config_file.endswith("acme.yml")


def test_reconcile_adds_removes_and_restarts(make_replicator, config, launcher):
    for t in ("a", "b", "c"):
        touch_db(config, t)
    r = make_replicator()
    r.reconcile({"a": lease("a"), "b": lease("b")})
    assert set(r.running_epochs()) == {"a", "b"}

    launcher.procs[0].crash()  # "a" died
    r.reconcile({"a": lease("a"), "c": lease("c")})  # "b" no longer held, "c" new

    assert set(r.running_epochs()) == {"a", "c"}
    b_proc = launcher.procs[1]
    assert b_proc.signals == [signal.SIGTERM]
    assert len(launcher.procs) == 4  # a, b, then restarted a, new c


def test_reconcile_survives_one_failing_tenant(make_replicator, config, launcher):
    touch_db(config, "a")
    touch_db(config, "b")

    def flaky(tenant_id):
        if tenant_id == "a":
            raise ConnectionError("dynamodb down")
        return None

    r = make_replicator(kms_key_for=flaky)
    r.reconcile({"a": lease("a"), "b": lease("b")})
    assert set(r.running_epochs()) == {"b"}


def test_stop_all_waits_for_final_sync(make_replicator, config, launcher):
    touch_db(config, "a")
    touch_db(config, "b")
    r = make_replicator()
    r.start(lease("a"))
    r.start(lease("b"))

    r.stop_all()
    assert all(p.signals == [signal.SIGTERM] for p in launcher.procs)
    assert all(p.returncode == 0 for p in launcher.procs)  # waited, not killed
    assert r.running_epochs() == {}


def test_config_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TENANT_BUCKET_NAME", raising=False)
    assert ReplicationConfig.from_env(tmp_path) is None

    monkeypatch.setenv("TENANT_BUCKET_NAME", "b")
    monkeypatch.setenv("TENANT_BUCKET_REGION", "us-east-2")
    monkeypatch.setenv("LITESTREAM_DEFAULT_KMS_KEY_ID", "arn:key/default")
    cfg = ReplicationConfig.from_env(tmp_path)
    assert (cfg.bucket, cfg.region, cfg.default_kms_key_id) == ("b", "us-east-2", "arn:key/default")
    assert cfg.data_dir == tmp_path


# ---- wiring into the API ------------------------------------------------------------


@pytest.fixture
def replicated_client(client, monkeypatch, tmp_path, launcher):
    config = ReplicationConfig(
        bucket="tenant-bucket",
        region="us-east-2",
        config_dir=tmp_path / "litestream",
        data_dir=tmp_path,  # client fixture points DATA_DIR at tmp_path
    )
    replicator = Replicator(config, popen=launcher)
    monkeypatch.setattr(main, "_replicator", replicator)
    return client, replicator


def test_create_tenant_starts_replication(replicated_client):
    client, replicator = replicated_client
    assert client.post("/tenants/acme").status_code == 201
    assert replicator.running_epochs() == {"acme": 1}


def test_lost_lease_stops_replication(replicated_client, placement, clock):
    client, replicator = replicated_client
    client.post("/tenants/acme")
    clock.advance(main.LEASE_SECONDS * 1000 + 1)
    placement.acquire("acme", "task-other", main.LEASE_SECONDS)

    main.renew_all(placement)
    assert replicator.running_epochs() == {}


def test_request_that_finds_lease_gone_stops_replication(replicated_client, clock):
    client, replicator = replicated_client
    client.post("/tenants/acme")
    clock.advance(main.LEASE_SECONDS * 1000 + 1)

    assert client.post("/tenants/acme/execute", json={"sql": "SELECT 1"}).status_code == 409
    assert replicator.running_epochs() == {}

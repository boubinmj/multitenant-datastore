"""API tests. Fixtures (client, placement, catalog, clock, ddb) are in conftest.py."""

from app import main

LEASE_MS = main.LEASE_SECONDS * 1000


def run(client, tenant, sql, params=None):
    return client.post(f"/tenants/{tenant}/execute", json={"sql": sql, "params": params or []})


def code(resp) -> str:
    return resp.json()["detail"]["code"]


# ---- basics ---------------------------------------------------------------------


def test_create_write_read(client):
    created = client.post("/tenants/acme")
    assert created.status_code == 201
    assert created.json()["epoch"] == 1
    assert run(client, "acme", "CREATE TABLE t (id INTEGER, name TEXT)").status_code == 200
    assert run(client, "acme", "INSERT INTO t VALUES (?, ?)", [1, "a"]).json()["rowcount"] == 1

    body = run(client, "acme", "SELECT id, name FROM t").json()
    assert body["columns"] == ["id", "name"]
    assert body["rows"] == [[1, "a"]]


def test_tenants_are_separate_files(client, tmp_path):
    client.post("/tenants/a")
    client.post("/tenants/b")
    run(client, "a", "CREATE TABLE only_in_a (x)")
    assert run(client, "b", "SELECT * FROM only_in_a").status_code == 400
    assert (tmp_path / "a.db").exists() and (tmp_path / "b.db").exists()


def test_create_twice_conflicts(client):
    client.post("/tenants/acme")
    resp = client.post("/tenants/acme")
    assert resp.status_code == 409
    assert code(resp) == "TENANT_EXISTS"


def test_invalid_tenant_id(client):
    assert client.post("/tenants/bad.id").status_code == 400
    assert run(client, "bad.id", "SELECT 1").status_code == 400


def test_multiple_statements_rejected(client):
    client.post("/tenants/acme")
    assert run(client, "acme", "SELECT 1; SELECT 2").status_code == 400


# ---- lease + epoch ----------------------------------------------------------------


def test_unknown_tenant_is_wrong_owner(client):
    resp = run(client, "nobody", "SELECT 1")
    assert resp.status_code == 409
    assert code(resp) == "WRONG_OWNER"


def test_create_refused_while_another_task_owns_tenant(client, placement, tmp_path):
    placement.acquire("acme", "task-other", main.LEASE_SECONDS)
    resp = client.post("/tenants/acme")
    assert resp.status_code == 409
    assert code(resp) == "WRONG_OWNER"
    assert not (tmp_path / "acme.db").exists()


def test_takeover_by_another_task_fences_this_one(client, placement, clock):
    client.post("/tenants/acme")
    clock.advance(LEASE_MS + 1)
    placement.acquire("acme", "task-other", main.LEASE_SECONDS)  # epoch 2

    resp = run(client, "acme", "SELECT 1")
    assert resp.status_code == 409
    assert code(resp) == "WRONG_OWNER"


def test_expired_lease_is_wrong_owner(client, clock):
    client.post("/tenants/acme")
    clock.advance(LEASE_MS + 1)
    assert code(run(client, "acme", "SELECT 1")) == "WRONG_OWNER"


def test_heartbeat_keeps_lease_alive(client, placement, clock):
    client.post("/tenants/acme")
    for _ in range(3):
        clock.advance(LEASE_MS // 2)
        main.renew_all(placement)
    assert run(client, "acme", "SELECT 1").status_code == 200


def test_heartbeat_drops_lost_lease(client, placement, clock):
    client.post("/tenants/acme")
    clock.advance(LEASE_MS + 1)
    placement.acquire("acme", "task-other", main.LEASE_SECONDS)
    main.renew_all(placement)
    assert "acme" not in main._leases


def _reassign_in_dynamodb(ddb, placement, tenant_id, new_owner):
    """Simulate the placement controller moving the tenant: new owner, epoch + 1."""
    ddb.update_item(
        TableName=placement.config.table_name,
        Key={"tenant_id": {"S": tenant_id}},
        UpdateExpression="SET #owner = :o ADD #epoch :one",
        ExpressionAttributeNames={"#owner": "owner_task", "#epoch": "epoch"},
        ExpressionAttributeValues={":o": {"S": new_owner}, ":one": {"N": "1"}},
    )


def test_failed_renew_stops_serving_immediately(client, placement, ddb):
    client.post("/tenants/acme")
    _reassign_in_dynamodb(ddb, placement, "acme", "task-other")

    main.renew_all(placement)  # conditional write on epoch fails
    assert "acme" not in main._leases
    assert code(run(client, "acme", "SELECT 1")) == "WRONG_OWNER"


def test_reassignment_seen_on_next_request_even_before_renew(client, placement, ddb):
    client.post("/tenants/acme")
    _reassign_in_dynamodb(ddb, placement, "acme", "task-other")
    assert code(run(client, "acme", "SELECT 1")) == "WRONG_OWNER"
    assert "acme" not in main._leases


def test_stops_serving_inside_safety_margin(client, clock):
    client.post("/tenants/acme")
    clock.advance(LEASE_MS - main.LEASE_SAFETY_MARGIN_MS)
    assert code(run(client, "acme", "SELECT 1")) == "WRONG_OWNER"


def test_expired_lease_refused_without_dynamodb(client, placement, clock, monkeypatch):
    client.post("/tenants/acme")
    clock.advance(LEASE_MS + 1)

    def unreachable(_lease):
        raise ConnectionError("dynamodb down")

    monkeypatch.setattr(placement, "holds", unreachable)
    assert code(run(client, "acme", "SELECT 1")) == "WRONG_OWNER"


def test_dynamodb_unreachable_with_valid_lease_is_503(client, placement, monkeypatch):
    client.post("/tenants/acme")

    def unreachable(_lease):
        raise ConnectionError("dynamodb down")

    monkeypatch.setattr(placement, "holds", unreachable)
    resp = run(client, "acme", "SELECT 1")
    assert resp.status_code == 503
    assert code(resp) == "PLACEMENT_UNAVAILABLE"
    assert "acme" in main._leases  # may still own it; keep renewing


def test_lease_lost_while_waiting_for_lock(client, monkeypatch):
    client.post("/tenants/acme")
    real_lock_for = main._lock_for

    class LoseLeaseOnEnter:
        """Passes the guard, then the heartbeat drops the lease before the lock is taken."""

        def __init__(self, tenant_id):
            self.lock = real_lock_for(tenant_id)

        def __enter__(self):
            main._leases.pop("acme", None)
            return self.lock.__enter__()

        def __exit__(self, *exc):
            return self.lock.__exit__(*exc)

    monkeypatch.setattr(main, "_lock_for", LoseLeaseOnEnter)
    assert code(run(client, "acme", "SELECT 1")) == "WRONG_OWNER"


def test_heartbeat_keeps_lease_through_brief_outage(client, placement, clock, monkeypatch):
    client.post("/tenants/acme")

    def unreachable(_lease, _seconds):
        raise ConnectionError("dynamodb down")

    monkeypatch.setattr(placement, "renew", unreachable)
    clock.advance(main.RENEW_INTERVAL_SECONDS * 1000)
    main.renew_all(placement)
    assert "acme" in main._leases  # still valid: keep it and retry

    clock.advance(LEASE_MS)
    main.renew_all(placement)
    assert "acme" not in main._leases  # expired during the outage: give it up


# ---- ACTIVE status ------------------------------------------------------------------


def test_suspended_tenant_rejected(client, ddb, catalog):
    client.post("/tenants/acme")
    ddb.update_item(
        TableName=catalog.config.table_name,
        Key={"tenant_id": {"S": "acme"}},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": "SUSPENDED"}},
    )
    resp = run(client, "acme", "SELECT 1")
    assert resp.status_code == 403
    assert code(resp) == "TENANT_NOT_ACTIVE"


# ---- mode=rw ----------------------------------------------------------------------


def test_missing_file_is_not_recreated(client, tmp_path):
    client.post("/tenants/acme")
    (tmp_path / "acme.db").unlink()
    resp = run(client, "acme", "SELECT 1")
    assert resp.status_code == 404
    assert code(resp) == "TENANT_NOT_FOUND"
    assert not (tmp_path / "acme.db").exists()


# ---- authorizer ---------------------------------------------------------------------


def test_attach_denied(client, tmp_path):
    client.post("/tenants/a")
    client.post("/tenants/b")
    resp = run(client, "a", "ATTACH DATABASE ? AS other", [str(tmp_path / "b.db")])
    assert resp.status_code == 400


def test_vacuum_into_denied(client, tmp_path):
    client.post("/tenants/acme")
    resp = run(client, "acme", "VACUUM INTO ?", [str(tmp_path / "copy.db")])
    assert resp.status_code == 400
    assert not (tmp_path / "copy.db").exists()


def test_load_extension_denied(client):
    client.post("/tenants/acme")
    assert run(client, "acme", "SELECT load_extension('anything')").status_code == 400


def test_risky_pragma_denied(client):
    client.post("/tenants/acme")
    assert run(client, "acme", "PRAGMA journal_mode=DELETE").status_code == 400
    assert run(client, "acme", "PRAGMA writable_schema=ON").status_code == 400


def test_introspection_pragma_allowed(client):
    client.post("/tenants/acme")
    run(client, "acme", "CREATE TABLE t (id INTEGER)")
    resp = run(client, "acme", "PRAGMA table_info(t)")
    assert resp.status_code == 200
    assert resp.json()["rows"][0][1] == "id"


# ---- limits -------------------------------------------------------------------------


def test_statement_timeout(client, monkeypatch):
    monkeypatch.setattr(main, "STATEMENT_TIMEOUT_SECONDS", 0.05)
    client.post("/tenants/acme")
    forever = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    resp = run(client, "acme", forever)
    assert resp.status_code == 408
    assert code(resp) == "STATEMENT_TIMEOUT"


def test_row_limit(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_ROWS", 5)
    client.post("/tenants/acme")
    rows = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT {}) SELECT x FROM c"
    assert run(client, "acme", rows.format(5)).status_code == 200
    resp = run(client, "acme", rows.format(6))
    assert resp.status_code == 400
    assert code(resp) == "ROW_LIMIT"


def test_db_size_limit(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_DB_BYTES", 64 * 1024)
    client.post("/tenants/acme")
    run(client, "acme", "CREATE TABLE blobs (b BLOB)")
    resp = run(client, "acme", "INSERT INTO blobs VALUES (randomblob(200000))")
    assert resp.status_code == 413
    assert code(resp) == "DB_SIZE_LIMIT"
    # Small writes still fit.
    assert run(client, "acme", "INSERT INTO blobs VALUES (randomblob(100))").status_code == 200

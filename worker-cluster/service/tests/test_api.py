import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    return TestClient(main.app)


def run(client, tenant, sql, params=None):
    return client.post(f"/tenants/{tenant}/execute", json={"sql": sql, "params": params or []})


def test_create_write_read(client):
    assert client.post("/tenants/acme").status_code == 201
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
    assert client.post("/tenants/acme").status_code == 409


def test_unknown_tenant(client):
    assert run(client, "nobody", "SELECT 1").status_code == 404


def test_invalid_tenant_id(client):
    assert client.post("/tenants/bad.id").status_code == 400


def test_multiple_statements_rejected(client):
    client.post("/tenants/acme")
    assert run(client, "acme", "SELECT 1; SELECT 2").status_code == 400


def test_attach_denied(client, tmp_path):
    client.post("/tenants/a")
    client.post("/tenants/b")
    resp = run(client, "a", "ATTACH DATABASE ? AS other", [str(tmp_path / "b.db")])
    assert resp.status_code == 400


def test_pragma_denied(client):
    client.post("/tenants/acme")
    assert run(client, "acme", "PRAGMA journal_mode=DELETE").status_code == 400

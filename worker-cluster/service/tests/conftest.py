"""Shared fixtures: an in-memory DynamoDB (moto) with the placement and tenants tables."""

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app import main
from app.placement import PlacementClient, PlacementConfig, get_placement_client
from app.tenants import TenantCatalog, TenantsConfig, get_tenant_catalog

REGION = "us-east-2"


class Clock:
    def __init__(self) -> None:
        self.now_ms = 1_000_000

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(name, value)


def create_table(ddb, table_name: str, key_attr: str) -> None:
    ddb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": key_attr, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": key_attr, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ddb():
    with mock_aws():
        yield boto3.client("dynamodb", region_name=REGION)


@pytest.fixture
def placement(ddb, clock):
    config = PlacementConfig(table_name="placement", region=REGION)
    create_table(ddb, config.table_name, config.key_attr)
    return PlacementClient(config, dynamodb_client=ddb, clock_ms=clock)


@pytest.fixture
def catalog(ddb):
    config = TenantsConfig(table_name="tenants", region=REGION)
    create_table(ddb, config.table_name, config.key_attr)
    return TenantCatalog(config, dynamodb_client=ddb)


@pytest.fixture
def client(tmp_path, monkeypatch, placement, catalog):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "TASK_ID", "task-under-test")
    monkeypatch.setattr(main, "_leases", {})
    monkeypatch.setattr(main, "_locks", {})
    monkeypatch.setattr(main, "_replicator", None)
    main.app.dependency_overrides[get_placement_client] = lambda: placement
    main.app.dependency_overrides[get_tenant_catalog] = lambda: catalog
    # Not used as a context manager, so the lifespan (env-based clients, heartbeat) doesn't run.
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()

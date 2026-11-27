"""Lease protocol tests against an in-memory DynamoDB (moto). No AWS account needed."""

import boto3
import pytest
from moto import mock_aws

from app.placement import (
    LeaseConflict,
    LeaseLost,
    PlacementClient,
    PlacementConfig,
)

REGION = "us-east-2"
LEASE_SECONDS = 30
LEASE_MS = LEASE_SECONDS * 1000


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


def make_client(config: PlacementConfig, clock: Clock) -> PlacementClient:
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=config.table_name,
        KeySchema=[{"AttributeName": config.key_attr, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": config.key_attr, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return PlacementClient(config, dynamodb_client=ddb, clock_ms=clock)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def placement(clock):
    with mock_aws():
        yield make_client(PlacementConfig(table_name="placement", region=REGION), clock)


def test_first_acquire_starts_at_epoch_1(placement, clock):
    lease = placement.acquire("acme", "task-a", LEASE_SECONDS)
    assert lease.owner_task == "task-a"
    assert lease.epoch == 1
    assert lease.lease_until_ms == clock.now_ms + LEASE_MS
    assert placement.holds(lease)


def test_cannot_acquire_while_another_task_holds_a_valid_lease(placement):
    placement.acquire("acme", "task-a", LEASE_SECONDS)
    with pytest.raises(LeaseConflict):
        placement.acquire("acme", "task-b", LEASE_SECONDS)


def test_takeover_after_expiry_bumps_epoch_and_fences_old_owner(placement, clock):
    old = placement.acquire("acme", "task-a", LEASE_SECONDS)
    clock.advance(LEASE_MS + 1)

    new = placement.acquire("acme", "task-b", LEASE_SECONDS)
    assert new.epoch == old.epoch + 1
    assert placement.holds(new)
    assert not placement.holds(old)
    with pytest.raises(LeaseLost):
        placement.renew(old, LEASE_SECONDS)
    with pytest.raises(LeaseLost):
        placement.release(old)


def test_renew_extends_lease_without_changing_epoch(placement, clock):
    lease = placement.acquire("acme", "task-a", LEASE_SECONDS)
    clock.advance(LEASE_MS - 1)

    renewed = placement.renew(lease, LEASE_SECONDS)
    assert renewed.epoch == lease.epoch
    assert renewed.lease_until_ms == clock.now_ms + LEASE_MS

    clock.advance(LEASE_MS - 1)
    assert placement.holds(renewed)


def test_expired_lease_cannot_be_renewed(placement, clock):
    lease = placement.acquire("acme", "task-a", LEASE_SECONDS)
    clock.advance(LEASE_MS + 1)
    assert not placement.holds(lease)
    with pytest.raises(LeaseLost):
        placement.renew(lease, LEASE_SECONDS)


def test_release_frees_tenant_but_keeps_epoch(placement):
    lease = placement.acquire("acme", "task-a", LEASE_SECONDS)
    placement.release(lease)
    assert placement.get("acme") is None

    # Next owner gets a higher epoch, never a reused one.
    again = placement.acquire("acme", "task-b", LEASE_SECONDS)
    assert again.epoch == lease.epoch + 1


def test_tenants_are_independent(placement):
    a = placement.acquire("acme", "task-a", LEASE_SECONDS)
    b = placement.acquire("globex", "task-b", LEASE_SECONDS)
    assert (a.epoch, b.epoch) == (1, 1)


def test_attribute_names_come_from_config(clock):
    # Simulates pointing at a real table whose attributes are named differently.
    config = PlacementConfig(
        table_name="cp-placement",
        region=REGION,
        key_attr="tenantId",
        owner_attr="ownerTask",
        epoch_attr="ownerEpoch",
        lease_attr="leaseUntilMs",
    )
    with mock_aws():
        client = make_client(config, clock)
        lease = client.acquire("acme", "task-a", LEASE_SECONDS)
        item = client._ddb.get_item(TableName="cp-placement", Key={"tenantId": {"S": "acme"}})["Item"]
        assert item["ownerTask"] == {"S": "task-a"}
        assert item["ownerEpoch"] == {"N": "1"}
        assert int(item["leaseUntilMs"]["N"]) == lease.lease_until_ms


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("PLACEMENT_TABLE_NAME", "cp-placement")
    monkeypatch.setenv("PLACEMENT_TABLE_REGION", "us-west-2")
    monkeypatch.setenv("PLACEMENT_EPOCH_ATTR", "ownerEpoch")
    config = PlacementConfig.from_env()
    assert config.table_name == "cp-placement"
    assert config.region == "us-west-2"
    assert config.epoch_attr == "ownerEpoch"
    assert config.key_attr == "tenant_id"


def test_config_requires_table_name(monkeypatch):
    monkeypatch.delenv("PLACEMENT_TABLE_NAME", raising=False)
    with pytest.raises(RuntimeError):
        PlacementConfig.from_env()

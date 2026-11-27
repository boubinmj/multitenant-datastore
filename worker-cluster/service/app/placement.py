"""Client for the placement table: which task owns each tenant, under which epoch, until when.

Every detail that identifies the table (name, region, endpoint, attribute names) lives in
PlacementConfig. To point the worker at the control plane's real table (CP-01), change the
config (env vars below), not this code. Worker code should only call PlacementClient methods.

Item shape (PROPOSED; the protocol still needs to be agreed with the control plane):

    tenant_id    S  partition key
    owner_task   S  current owner's task ID; absent when nobody owns the tenant
    epoch        N  +1 on every acquire; never decreases, never reset
    lease_until  N  Unix time in milliseconds; absent when released

Items are never deleted: release keeps the epoch. If the epoch were reset, a new owner could
reuse an old gen-{epoch} prefix in S3 and mix its writes with a stale owner's.

Env vars read by PlacementConfig.from_env():

    PLACEMENT_TABLE_NAME     required
    PLACEMENT_TABLE_REGION   optional, defaults to AWS_REGION
    PLACEMENT_ENDPOINT_URL   optional, e.g. http://localhost:8000 for DynamoDB Local
    PLACEMENT_KEY_ATTR, PLACEMENT_OWNER_ATTR, PLACEMENT_EPOCH_ATTR, PLACEMENT_LEASE_ATTR
                             optional, override attribute names if the real table differs
"""

import functools
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

DEFAULT_LEASE_SECONDS = 30


@dataclass(frozen=True)
class PlacementConfig:
    table_name: str
    region: str | None = None
    endpoint_url: str | None = None
    key_attr: str = "tenant_id"
    owner_attr: str = "owner_task"
    epoch_attr: str = "epoch"
    lease_attr: str = "lease_until"

    @classmethod
    def from_env(cls) -> "PlacementConfig":
        table_name = os.environ.get("PLACEMENT_TABLE_NAME")
        if not table_name:
            raise RuntimeError("PLACEMENT_TABLE_NAME is not set")
        defaults = cls(table_name=table_name)
        return cls(
            table_name=table_name,
            region=os.environ.get("PLACEMENT_TABLE_REGION") or os.environ.get("AWS_REGION"),
            endpoint_url=os.environ.get("PLACEMENT_ENDPOINT_URL") or None,
            key_attr=os.environ.get("PLACEMENT_KEY_ATTR", defaults.key_attr),
            owner_attr=os.environ.get("PLACEMENT_OWNER_ATTR", defaults.owner_attr),
            epoch_attr=os.environ.get("PLACEMENT_EPOCH_ATTR", defaults.epoch_attr),
            lease_attr=os.environ.get("PLACEMENT_LEASE_ATTR", defaults.lease_attr),
        )


@dataclass(frozen=True)
class Lease:
    tenant_id: str
    owner_task: str
    epoch: int
    lease_until_ms: int


class LeaseConflict(Exception):
    """Another task holds a valid lease on the tenant."""


class LeaseLost(Exception):
    """The caller no longer owns the tenant: the owner or epoch changed, or the lease expired."""


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class PlacementClient:
    """Acquire / renew / release a tenant's lease with conditional writes."""

    def __init__(
        self,
        config: PlacementConfig,
        *,
        dynamodb_client: Any = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self._ddb = dynamodb_client or boto3.client(
            "dynamodb", region_name=config.region, endpoint_url=config.endpoint_url
        )
        self._now_ms = clock_ms or (lambda: int(time.time() * 1000))
        # Each update below uses all three names. DynamoDB rejects unused ones, so keep it
        # that way if you add an update.
        self._names = {
            "#owner": config.owner_attr,
            "#epoch": config.epoch_attr,
            "#lease": config.lease_attr,
        }

    def _key(self, tenant_id: str) -> dict:
        return {self.config.key_attr: {"S": tenant_id}}

    def now_ms(self) -> int:
        """The clock leases are judged by (injectable for tests)."""
        return self._now_ms()

    def _lease_from_item(self, tenant_id: str, item: dict) -> Lease:
        c = self.config
        return Lease(
            tenant_id=tenant_id,
            owner_task=item[c.owner_attr]["S"],
            epoch=int(item[c.epoch_attr]["N"]),
            lease_until_ms=int(item[c.lease_attr]["N"]),
        )

    def acquire(
        self, tenant_id: str, owner_task: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> Lease:
        """Take ownership if nobody owns the tenant or the current lease has expired.

        Bumps the epoch. Raises LeaseConflict if another task holds a valid lease.
        """
        now = self._now_ms()
        try:
            resp = self._ddb.update_item(
                TableName=self.config.table_name,
                Key=self._key(tenant_id),
                UpdateExpression="SET #owner = :me, #lease = :until ADD #epoch :one",
                ConditionExpression="attribute_not_exists(#owner) OR #lease < :now",
                ExpressionAttributeNames=self._names,
                ExpressionAttributeValues={
                    ":me": {"S": owner_task},
                    ":until": {"N": str(now + int(lease_seconds * 1000))},
                    ":one": {"N": "1"},
                    ":now": {"N": str(now)},
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise LeaseConflict(tenant_id) from exc
            raise
        return self._lease_from_item(tenant_id, resp["Attributes"])

    def renew(
        self, lease: Lease, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> Lease:
        """Extend a lease the caller still holds. Raises LeaseLost otherwise.

        An expired lease can't be renewed: another task may already have been told it's free.
        Call acquire() instead, which bumps the epoch.
        """
        now = self._now_ms()
        try:
            resp = self._ddb.update_item(
                TableName=self.config.table_name,
                Key=self._key(lease.tenant_id),
                UpdateExpression="SET #lease = :until",
                ConditionExpression="#owner = :me AND #epoch = :epoch AND #lease >= :now",
                ExpressionAttributeNames=self._names,
                ExpressionAttributeValues={
                    ":me": {"S": lease.owner_task},
                    ":epoch": {"N": str(lease.epoch)},
                    ":until": {"N": str(now + int(lease_seconds * 1000))},
                    ":now": {"N": str(now)},
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise LeaseLost(lease.tenant_id) from exc
            raise
        return self._lease_from_item(lease.tenant_id, resp["Attributes"])

    def release(self, lease: Lease) -> None:
        """Give up ownership. Keeps the item and its epoch. Raises LeaseLost if not the owner."""
        try:
            self._ddb.update_item(
                TableName=self.config.table_name,
                Key=self._key(lease.tenant_id),
                UpdateExpression="REMOVE #owner, #lease",
                ConditionExpression="#owner = :me AND #epoch = :epoch",
                ExpressionAttributeNames=self._names,
                ExpressionAttributeValues={
                    ":me": {"S": lease.owner_task},
                    ":epoch": {"N": str(lease.epoch)},
                },
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise LeaseLost(lease.tenant_id) from exc
            raise

    def get(self, tenant_id: str) -> Lease | None:
        """Current owner's lease (strongly consistent read), or None if nobody owns the tenant.

        The returned lease may already be expired; use holds() to check validity.
        """
        resp = self._ddb.get_item(
            TableName=self.config.table_name,
            Key=self._key(tenant_id),
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item or self.config.owner_attr not in item:
            return None
        return self._lease_from_item(tenant_id, item)

    def holds(self, lease: Lease) -> bool:
        """True if `lease` is still the current, unexpired lease. The per-request guard."""
        current = self.get(lease.tenant_id)
        return (
            current is not None
            and current.owner_task == lease.owner_task
            and current.epoch == lease.epoch
            and current.lease_until_ms >= self._now_ms()
        )


@functools.cache
def get_placement_client() -> PlacementClient:
    """Process-wide client built from env vars. The one place worker code gets a client."""
    return PlacementClient(PlacementConfig.from_env())

"""Client for the tenants table: each tenant's status (ACTIVE, SUSPENDED, ...).

The control plane owns this table. Like placement.py, everything that identifies the table
comes from TenantsConfig, so pointing at the real table means changing env vars only.

    TENANTS_TABLE_NAME     required
    TENANTS_TABLE_REGION   optional, defaults to AWS_REGION
    TENANTS_ENDPOINT_URL   optional, e.g. http://localhost:8000 for DynamoDB Local
    TENANTS_KEY_ATTR, TENANTS_STATUS_ATTR
                           optional, override attribute names if the real table differs
"""

import functools
import os
from dataclasses import dataclass
from typing import Any

import boto3

ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class TenantsConfig:
    table_name: str
    region: str | None = None
    endpoint_url: str | None = None
    key_attr: str = "tenant_id"
    status_attr: str = "status"

    @classmethod
    def from_env(cls) -> "TenantsConfig":
        table_name = os.environ.get("TENANTS_TABLE_NAME")
        if not table_name:
            raise RuntimeError("TENANTS_TABLE_NAME is not set")
        defaults = cls(table_name=table_name)
        return cls(
            table_name=table_name,
            region=os.environ.get("TENANTS_TABLE_REGION") or os.environ.get("AWS_REGION"),
            endpoint_url=os.environ.get("TENANTS_ENDPOINT_URL") or None,
            key_attr=os.environ.get("TENANTS_KEY_ATTR", defaults.key_attr),
            status_attr=os.environ.get("TENANTS_STATUS_ATTR", defaults.status_attr),
        )


class TenantCatalog:
    def __init__(self, config: TenantsConfig, *, dynamodb_client: Any = None) -> None:
        self.config = config
        self._ddb = dynamodb_client or boto3.client(
            "dynamodb", region_name=config.region, endpoint_url=config.endpoint_url
        )

    def _key(self, tenant_id: str) -> dict:
        return {self.config.key_attr: {"S": tenant_id}}

    def get_status(self, tenant_id: str) -> str | None:
        """The tenant's status, or None if the tenant isn't in the catalog."""
        resp = self._ddb.get_item(
            TableName=self.config.table_name,
            Key=self._key(tenant_id),
            ConsistentRead=True,
        )
        item = resp.get("Item") or {}
        status = item.get(self.config.status_attr)
        return status["S"] if status else None

    def register(self, tenant_id: str) -> str:
        """Add a new tenant as ACTIVE. Leaves an existing tenant's status unchanged.

        STAND-IN for the control plane's Tenant API, which will own tenant creation. Remove
        the worker's call to this once that exists.
        """
        resp = self._ddb.update_item(
            TableName=self.config.table_name,
            Key=self._key(tenant_id),
            UpdateExpression="SET #status = if_not_exists(#status, :active)",
            ExpressionAttributeNames={"#status": self.config.status_attr},
            ExpressionAttributeValues={":active": {"S": ACTIVE}},
            ReturnValues="ALL_NEW",
        )
        return resp["Attributes"][self.config.status_attr]["S"]


@functools.cache
def get_tenant_catalog() -> TenantCatalog:
    """Process-wide catalog built from env vars. The one place worker code gets a catalog."""
    return TenantCatalog(TenantsConfig.from_env())

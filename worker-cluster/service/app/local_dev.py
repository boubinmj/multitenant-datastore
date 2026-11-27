"""Create the placement and tenants tables in DynamoDB Local, for `docker compose up`.

Only runs when CREATE_LOCAL_TABLES=1, and refuses to touch a table that has no endpoint
override, so it can never create tables in a real AWS account.
"""

import logging
import time

from botocore.exceptions import ClientError, EndpointConnectionError

from app.placement import PlacementClient
from app.tenants import TenantCatalog

log = logging.getLogger("worker")


def _create(ddb, table_name: str, key_attr: str, attempts: int) -> None:
    for attempt in range(attempts):
        try:
            ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": key_attr, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key_attr, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            log.info("created local table %s", table_name)
            return
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceInUseException":
                return  # already exists
            raise
        except EndpointConnectionError:
            # DynamoDB Local may still be starting.
            if attempt == attempts - 1:
                raise
            time.sleep(1)


def create_local_tables(
    placement: PlacementClient, catalog: TenantCatalog, attempts: int = 30
) -> None:
    for name, config in (("placement", placement.config), ("tenants", catalog.config)):
        if not config.endpoint_url:
            raise RuntimeError(
                f"CREATE_LOCAL_TABLES=1 but the {name} table has no endpoint override; "
                "refusing to create tables in AWS"
            )
    _create(placement._ddb, placement.config.table_name, placement.config.key_attr, attempts)
    _create(catalog._ddb, catalog.config.table_name, catalog.config.key_attr, attempts)

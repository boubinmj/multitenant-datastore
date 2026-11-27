"""TEMPORARY stand-ins for the control plane's DynamoDB tables (CP-01 and friends).

Lets the worker cluster develop against real tables before the control plane creates them.
When the real tables exist, swap them in worker_cluster_stack.py and delete this file.
Nothing else in the infra refers to these constructs.
"""

from aws_cdk import RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

# Must match the key_attr defaults in service/app/placement.py and service/app/tenants.py.
TENANT_KEY = "tenant_id"


def _table(scope: Construct, construct_id: str) -> dynamodb.Table:
    return dynamodb.Table(
        scope,
        construct_id,
        partition_key=dynamodb.Attribute(name=TENANT_KEY, type=dynamodb.AttributeType.STRING),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        # Throwaway data: `cdk destroy` deletes the tables and everything in them.
        removal_policy=RemovalPolicy.DESTROY,
    )


class ControlPlaneStandins(Construct):
    """`placement` (owner_task, epoch, lease_until) and `tenants` (status), keyed by tenant_id."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.placement_table = _table(self, "PlacementTable")
        self.tenants_table = _table(self, "TenantsTable")

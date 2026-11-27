"""TEMPORARY stand-in for the control plane's placement table (CP-01).

Lets the worker cluster develop against a real DynamoDB table before the control plane
creates one. When the real table exists, swap it in worker_cluster_stack.py and delete
this file. Nothing else in the infra refers to this construct.
"""

from aws_cdk import RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

# Must match PlacementConfig.key_attr in service/app/placement.py.
PLACEMENT_KEY = "tenant_id"


class PlacementTable(Construct):
    """On-demand table keyed by tenant_id. Other attributes are schemaless (see the service)."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        self.table = dynamodb.Table(
            self,
            "Table",
            partition_key=dynamodb.Attribute(name=PLACEMENT_KEY, type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Throwaway data: `cdk destroy` deletes the table and all leases in it.
            removal_policy=RemovalPolicy.DESTROY,
        )

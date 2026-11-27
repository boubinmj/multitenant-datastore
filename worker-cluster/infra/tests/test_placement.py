"""Synth-only tests for the temporary placement table and the worker's access to it."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from worker_cluster.placement import PLACEMENT_KEY
from worker_cluster.worker_cluster_stack import WorkerClusterStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = WorkerClusterStack(app, "TestStack")
    return Template.from_stack(stack)


def test_table_keyed_by_tenant_on_demand(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [{"AttributeName": PLACEMENT_KEY, "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_task_role_can_only_get_and_update_items(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Sid": "PlacementLeases",
                                "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"],
                            }
                        )
                    ]
                )
            }
        },
    )


def test_container_gets_table_name_and_region(template):
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "ContainerDefinitions": [
                Match.object_like(
                    {
                        "Environment": Match.array_with(
                            [
                                Match.object_like({"Name": "PLACEMENT_TABLE_NAME"}),
                                Match.object_like({"Name": "PLACEMENT_TABLE_REGION"}),
                            ]
                        )
                    }
                )
            ]
        },
    )

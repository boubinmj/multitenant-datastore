"""Synth-only tests for the stand-in control-plane tables and the worker's access to them."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from worker_cluster.control_plane_standins import TENANT_KEY
from worker_cluster.worker_cluster_stack import WorkerClusterStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = WorkerClusterStack(app, "TestStack")
    return Template.from_stack(stack)


def test_two_tables_keyed_by_tenant_on_demand(template):
    template.resource_count_is("AWS::DynamoDB::Table", 2)
    template.all_resources_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [{"AttributeName": TENANT_KEY, "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


@pytest.mark.parametrize("sid", ["PlacementLeases", "TenantStatus"])
def test_task_role_can_only_get_and_update_items(template, sid):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {"Sid": sid, "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"]}
                        )
                    ]
                )
            }
        },
    )


def test_container_gets_table_names_and_regions(template):
    names = [
        "PLACEMENT_TABLE_NAME",
        "PLACEMENT_TABLE_REGION",
        "TENANTS_TABLE_NAME",
        "TENANTS_TABLE_REGION",
    ]
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "ContainerDefinitions": [
                Match.object_like(
                    {
                        "Environment": Match.array_with(
                            [Match.object_like({"Name": name}) for name in names]
                        )
                    }
                )
            ]
        },
    )

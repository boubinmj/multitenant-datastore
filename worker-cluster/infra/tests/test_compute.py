"""Synth-only tests for the CL-03 worker service. No Docker or AWS credentials needed."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from worker_cluster.worker_cluster_stack import WorkerClusterStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = WorkerClusterStack(app, "TestStack")
    return Template.from_stack(stack)


def test_one_private_task_never_two_at_once(template):
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "DesiredCount": 1,
            "LaunchType": "FARGATE",
            "DeploymentConfiguration": Match.object_like(
                {"MaximumPercent": 100, "MinimumHealthyPercent": 0}
            ),
            "NetworkConfiguration": {
                "AwsvpcConfiguration": Match.object_like({"AssignPublicIp": "DISABLED"})
            },
        },
    )


def test_task_role_lists_only_its_prefixes(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Sid": "ListOwnPrefixes",
                                "Action": "s3:ListBucket",
                                "Condition": {
                                    "StringLike": {"s3:prefix": ["tenants/*", "migrations/*"]}
                                },
                            }
                        )
                    ]
                )
            }
        },
    )


def test_migrations_are_read_only(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [Match.object_like({"Sid": "MigrationsReadOnly", "Action": "s3:GetObject"})]
                )
            }
        },
    )


def test_api_port_open_only_inside_vpc(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {
            "SecurityGroupIngress": [
                Match.object_like(
                    {
                        "FromPort": 8080,
                        "ToPort": 8080,
                        "CidrIp": {"Fn::GetAtt": [Match.any_value(), "CidrBlock"]},
                    }
                )
            ]
        },
    )

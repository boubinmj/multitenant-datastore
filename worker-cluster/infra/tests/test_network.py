"""Synth-only tests for the CL-01 network. No AWS credentials needed."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from worker_cluster.worker_cluster_stack import WorkerClusterStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = WorkerClusterStack(app, "TestStack")
    return Template.from_stack(stack)


def test_single_private_subnet(template):
    template.resource_count_is("AWS::EC2::VPC", 1)
    template.resource_count_is("AWS::EC2::Subnet", 1)
    template.has_resource_properties("AWS::EC2::Subnet", {"MapPublicIpOnLaunch": False})


def test_no_internet_access(template):
    template.resource_count_is("AWS::EC2::InternetGateway", 0)
    template.resource_count_is("AWS::EC2::NatGateway", 0)


def test_only_the_five_agreed_endpoints(template):
    template.resource_count_is("AWS::EC2::VPCEndpoint", 5)
    interface = template.find_resources(
        "AWS::EC2::VPCEndpoint", {"Properties": {"VpcEndpointType": "Interface"}}
    )
    assert len(interface) == 3

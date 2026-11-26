"""CL-01: private network for the worker cluster."""

from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class WorkerNetwork(Construct):
    """VPC with a single private subnet in one AZ and no internet access.

    There is no internet gateway or NAT gateway. Workers reach AWS services only
    through VPC endpoints:

    - Gateway endpoints (free): S3, DynamoDB.
      S3 is also required for ECR image pulls, because ECR stores image layers in S3.
    - Interface endpoints (~$7/month each): ECR API, ECR Docker registry, CloudWatch Logs.
    """

    def __init__(self, scope: Construct, construct_id: str, *, cidr: str = "10.0.0.0/16") -> None:
        super().__init__(scope, construct_id)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(cidr),
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        # Workers and other constructs (CL-02, CL-03) should place resources here.
        self.private_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)

        # Gateway endpoints: add routes to the private subnet's route table.
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[self.private_subnets],
        )
        self.vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            subnets=[self.private_subnets],
        )

        # Interface endpoints: an ENI in the private subnet with private DNS, so the
        # normal service hostnames resolve to it. CDK creates a security group for each
        # that allows HTTPS (443) from inside the VPC.
        interface_endpoints = {
            "EcrApiEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDockerEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "LogsEndpoint": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        }
        for endpoint_id, service in interface_endpoints.items():
            self.vpc.add_interface_endpoint(
                endpoint_id,
                service=service,
                subnets=self.private_subnets,
                private_dns_enabled=True,
            )

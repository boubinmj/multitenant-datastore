"""CL-03: ECS Fargate service running the worker HTTP API."""

from pathlib import Path

from aws_cdk import RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from worker_cluster.network import WorkerNetwork
from worker_cluster.storage_layout import MIGRATIONS_PREFIX, TENANTS_PREFIX

SERVICE_DIR = Path(__file__).resolve().parents[2] / "service"
API_PORT = 8080


class WorkerService(Construct):
    """One Fargate task in the private subnet serving the worker API on port 8080.

    Tenant .db files live on the task's ephemeral storage, so they are lost whenever
    the task is replaced (deploy, crash, scale-in) until snapshot/WAL upload to S3
    exists.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: WorkerNetwork,
        bucket: s3.IBucket,
        placement_table: dynamodb.ITable,
        tenants_table: dynamodb.ITable,
    ) -> None:
        super().__init__(scope, construct_id)

        vpc = network.vpc

        self.cluster = ecs.Cluster(self, "Cluster", vpc=vpc)

        # Task role: what the worker code itself may do. S3 access only, scoped to
        # the worker's prefixes. No KMS permissions needed: the bucket uses the
        # aws/s3 managed key, which S3 uses on the caller's behalf.
        self.task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="TenantDataReadWrite",
                actions=[
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:GetObjectTagging",
                    "s3:PutObjectTagging",
                ],
                resources=[bucket.arn_for_objects(f"{TENANTS_PREFIX}*")],
            )
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="MigrationsReadOnly",
                actions=["s3:GetObject"],
                resources=[bucket.arn_for_objects(f"{MIGRATIONS_PREFIX}*")],
            )
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="ListOwnPrefixes",
                actions=["s3:ListBucket"],
                resources=[bucket.bucket_arn],
                conditions={
                    "StringLike": {
                        "s3:prefix": [f"{TENANTS_PREFIX}*", f"{MIGRATIONS_PREFIX}*"]
                    }
                },
            )
        )

        # Lease reads and conditional writes only (service/app/placement.py). Reached
        # through the DynamoDB gateway endpoint from CL-01.
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="PlacementLeases",
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[placement_table.table_arn],
            )
        )
        # Status reads for the ACTIVE guard. UpdateItem is only for the stand-in tenant
        # registration in POST /tenants/{id}; drop it once the control plane creates tenants.
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="TenantStatus",
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[tenants_table.table_arn],
            )
        )

        task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            cpu=256,
            memory_limit_mib=512,
            task_role=self.task_role,
        )

        log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        task_definition.add_container(
            "Worker",
            # Built locally by `cdk deploy` (needs Docker) and pushed to the
            # bootstrap ECR repo; the task pulls it through the ECR endpoints.
            image=ecs.ContainerImage.from_asset(
                str(SERVICE_DIR),
                platform=ecr_assets.Platform.LINUX_AMD64,
            ),
            port_mappings=[ecs.PortMapping(container_port=API_PORT)],
            # Read by PlacementConfig.from_env() and TenantsConfig.from_env(). Add
            # PLACEMENT_*_ATTR / TENANTS_*_ATTR here if the real tables' attribute names
            # differ from the service defaults. Regions are each table's own, which is
            # correct even for a table imported by ARN.
            environment={
                "PLACEMENT_TABLE_NAME": placement_table.table_name,
                "PLACEMENT_TABLE_REGION": placement_table.env.region,
                "TENANTS_TABLE_NAME": tenants_table.table_name,
                "TENANTS_TABLE_REGION": tenants_table.env.region,
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="worker", log_group=log_group),
        )

        # Only reachable from inside the VPC (e.g. the controller, later).
        self.security_group = ec2.SecurityGroup(self, "ServiceSg", vpc=vpc)
        self.security_group.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(API_PORT),
            "Worker API from inside the VPC",
        )

        self.service = ecs.FargateService(
            self,
            "Service",
            cluster=self.cluster,
            task_definition=task_definition,
            desired_count=1,
            # Stop the old task before starting the new one, so two tasks never
            # run at once. Means a short outage on each deploy.
            min_healthy_percent=0,
            max_healthy_percent=100,
            assign_public_ip=False,
            vpc_subnets=network.private_subnets,
            security_groups=[self.security_group],
        )

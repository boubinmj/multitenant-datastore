from aws_cdk import CfnOutput, Stack
from constructs import Construct

from worker_cluster.compute import WorkerService
from worker_cluster.control_plane_standins import ControlPlaneStandins
from worker_cluster.network import WorkerNetwork
from worker_cluster.storage import TenantStorage


class WorkerClusterStack(Stack):
    """Worker cluster: CL-01 network, CL-02 tenant storage, CL-03 worker service."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.network = WorkerNetwork(self, "Network")
        self.storage = TenantStorage(self, "Storage")

        # TEMPORARY stand-ins until the control plane creates the real placement and
        # tenants tables. To switch, import the real tables instead and delete
        # control_plane_standins.py:
        #     from aws_cdk import aws_dynamodb as dynamodb
        #     placement_table = dynamodb.Table.from_table_name(self, "Placement", "<name>")
        #     tenants_table = dynamodb.Table.from_table_name(self, "Tenants", "<name>")
        # (use Table.from_table_arn if a table is in another region or account).
        standins = ControlPlaneStandins(self, "ControlPlaneStandins")
        placement_table = standins.placement_table
        tenants_table = standins.tenants_table

        self.workers = WorkerService(
            self,
            "Workers",
            network=self.network,
            bucket=self.storage.bucket,
            placement_table=placement_table,
            tenants_table=tenants_table,
        )

        CfnOutput(self, "VpcId", value=self.network.vpc.vpc_id)
        CfnOutput(
            self,
            "PrivateSubnetId",
            value=self.network.vpc.isolated_subnets[0].subnet_id,
        )
        CfnOutput(self, "TenantBucketName", value=self.storage.bucket.bucket_name)
        CfnOutput(self, "ClusterName", value=self.workers.cluster.cluster_name)
        CfnOutput(self, "ServiceName", value=self.workers.service.service_name)
        CfnOutput(self, "PlacementTableName", value=placement_table.table_name)
        CfnOutput(self, "TenantsTableName", value=tenants_table.table_name)

from aws_cdk import CfnOutput, Stack
from constructs import Construct

from worker_cluster.compute import WorkerService
from worker_cluster.network import WorkerNetwork
from worker_cluster.placement import PlacementTable
from worker_cluster.storage import TenantStorage


class WorkerClusterStack(Stack):
    """Worker cluster: CL-01 network, CL-02 tenant storage, CL-03 worker service."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.network = WorkerNetwork(self, "Network")
        self.storage = TenantStorage(self, "Storage")

        # TEMPORARY stand-in until the control plane (CP-01) creates the real placement
        # table. To switch, replace the next line with an import of the real table and
        # delete placement.py:
        #     placement_table = dynamodb.Table.from_table_name(self, "Placement", "<name>")
        # (use Table.from_table_arn if it's in another region or account).
        placement_table = PlacementTable(self, "Placement").table

        self.workers = WorkerService(
            self,
            "Workers",
            network=self.network,
            bucket=self.storage.bucket,
            placement_table=placement_table,
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

from aws_cdk import CfnOutput, Stack
from constructs import Construct

from worker_cluster.compute import WorkerService
from worker_cluster.network import WorkerNetwork
from worker_cluster.storage import TenantStorage


class WorkerClusterStack(Stack):
    """Worker cluster: CL-01 network, CL-02 tenant storage, CL-03 worker service."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.network = WorkerNetwork(self, "Network")
        self.storage = TenantStorage(self, "Storage")
        self.workers = WorkerService(
            self,
            "Workers",
            network=self.network,
            bucket=self.storage.bucket,
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

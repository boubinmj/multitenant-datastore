from aws_cdk import CfnOutput, Stack
from constructs import Construct

from worker_cluster.network import WorkerNetwork
from worker_cluster.storage import TenantStorage


class WorkerClusterStack(Stack):
    """Worker cluster infrastructure: CL-01 network and CL-02 tenant storage."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.network = WorkerNetwork(self, "Network")
        self.storage = TenantStorage(self, "Storage")

        CfnOutput(self, "VpcId", value=self.network.vpc.vpc_id)
        CfnOutput(
            self,
            "PrivateSubnetId",
            value=self.network.vpc.isolated_subnets[0].subnet_id,
        )
        CfnOutput(self, "TenantBucketName", value=self.storage.bucket.bucket_name)

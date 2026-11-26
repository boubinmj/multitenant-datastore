from aws_cdk import CfnOutput, Stack
from constructs import Construct

from worker_cluster.network import WorkerNetwork


class WorkerClusterStack(Stack):
    """Worker cluster infrastructure. CL-01 network today; CL-02/CL-03 build on it."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.network = WorkerNetwork(self, "Network")

        CfnOutput(self, "VpcId", value=self.network.vpc.vpc_id)
        CfnOutput(
            self,
            "PrivateSubnetId",
            value=self.network.vpc.isolated_subnets[0].subnet_id,
        )

#!/usr/bin/env python3
"""CDK entry point for the worker cluster infrastructure."""

import os

import aws_cdk as cdk

from worker_cluster.worker_cluster_stack import WorkerClusterStack

app = cdk.App()

WorkerClusterStack(
    app,
    "WorkerClusterStack",
    # Uses the account/region from your current AWS credentials (AWS_PROFILE / AWS_REGION).
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)

app.synth()

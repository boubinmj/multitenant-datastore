#!/usr/bin/env python3
"""CDK entry point for the worker cluster infrastructure."""

import os

import aws_cdk as cdk

from worker_cluster.worker_cluster_stack import WorkerClusterStack

# The whole team deploys to Ohio. Pinned here so a different AWS_REGION in
# someone's shell or .env can't send the stack to another region.
REGION = "us-east-2"

app = cdk.App()

WorkerClusterStack(
    app,
    "WorkerClusterStack",
    # Account comes from your current AWS credentials; region is always REGION.
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=REGION,
    ),
)

app.synth()

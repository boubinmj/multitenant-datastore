#!/usr/bin/env python3
"""CDK entry point for the edge layer (identity + API Gateway)."""

import os

import aws_cdk as cdk

from edge_api.edge_stack import EdgeStack

# Same as worker-cluster: the whole team deploys to Ohio.
REGION = "us-east-2"

app = cdk.App()

EdgeStack(
    app,
    "EdgeStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=REGION,
    ),
)

app.synth()

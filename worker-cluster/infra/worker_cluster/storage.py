"""CL-02: S3 bucket for tenant snapshots, WAL segments, and migration scripts."""

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_s3 as s3
from constructs import Construct

from worker_cluster.storage_layout import (
    GENERATION_RETIRED,
    GENERATION_STATE_TAG,
    TENANTS_PREFIX,
)


class TenantStorage(Construct):
    """Private, SSE-KMS encrypted bucket using the layout in storage_layout.py.

    Workers in the private subnet reach it through the S3 gateway endpoint from CL-01.
    Encryption uses the AWS managed key (aws/s3): S3 calls KMS on the caller's behalf,
    so workers don't need a KMS endpoint, and there's no monthly key charge.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        retired_generation_days: int = 30,
    ) -> None:
        super().__init__(scope, construct_id)

        self.bucket = s3.Bucket(
            self,
            "Bucket",
            encryption=s3.BucketEncryption.KMS_MANAGED,
            # Caches a data key per bucket so S3 makes far fewer KMS calls.
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # Bucket policy that rejects non-HTTPS requests.
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireRetiredGenerations",
                    prefix=TENANTS_PREFIX,
                    tag_filters={GENERATION_STATE_TAG: GENERATION_RETIRED},
                    expiration=Duration.days(retired_generation_days),
                ),
            ],
            # The stack is deployed and destroyed on demand, so `cdk destroy` empties
            # and deletes the bucket instead of leaving it orphaned. This DELETES
            # ALL TENANT DATA on destroy.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

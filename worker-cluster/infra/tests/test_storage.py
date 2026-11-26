"""Synth-only tests for the CL-02 tenant bucket and its key layout."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from worker_cluster import storage_layout as layout
from worker_cluster.worker_cluster_stack import WorkerClusterStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = WorkerClusterStack(app, "TestStack")
    return Template.from_stack(stack)


def test_sse_kms_with_bucket_key(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {
                        "BucketKeyEnabled": True,
                        "ServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"},
                    }
                ]
            }
        },
    )


def test_public_access_blocked(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )


def test_lifecycle_expires_only_retired_generations(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Prefix": "tenants/",
                                "Status": "Enabled",
                                "ExpirationInDays": 30,
                                "TagFilters": [
                                    {"Key": "generation-state", "Value": "retired"}
                                ],
                            }
                        )
                    ]
                )
            }
        },
    )


def test_key_layout():
    assert layout.snapshots_prefix("acme", 1700000000) == "tenants/acme/gen-1700000000/snapshots/"
    assert layout.wal_prefix("acme", 1700000000) == "tenants/acme/gen-1700000000/wal/"
    assert layout.migration_prefix(3) == "migrations/v3/"


@pytest.mark.parametrize("bad_id", ["", "a/b", "../other"])
def test_tenant_id_cannot_escape_its_prefix(bad_id):
    with pytest.raises(ValueError):
        layout.tenant_prefix(bad_id)

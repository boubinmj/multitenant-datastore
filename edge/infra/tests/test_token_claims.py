import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from edge_api.edge_stack import EdgeStack
from lambdas.pre_token.handler import build_claims, lambda_handler


def test_tenant_user_gets_tenant_id_and_role():
    assert build_claims({"custom:tenant_id": "t1"}, ["tenant_user"]) == {
        "role": "tenant_user",
        "tenant_id": "t1",
    }


def test_admin_without_tenant_is_allowed():
    assert build_claims({}, ["admin"]) == {"role": "admin"}


def test_tenant_user_without_tenant_is_rejected():
    with pytest.raises(ValueError):
        build_claims({}, ["tenant_user"])


def test_handler_sets_access_and_id_token_claims():
    event = {
        "request": {
            "userAttributes": {"custom:tenant_id": "t1"},
            "groupConfiguration": {"groupsToOverride": ["tenant_user"]},
        },
        "response": {},
    }
    out = lambda_handler(event, None)["response"]["claimsAndScopeOverrideDetails"]
    assert out["accessTokenGeneration"]["claimsToAddOrOverride"]["tenant_id"] == "t1"
    assert out["idTokenGeneration"]["claimsToAddOrOverride"]["role"] == "tenant_user"


def test_user_pool_uses_v2_pre_token_trigger():
    app = cdk.App()
    stack = EdgeStack(
        app, "T", env=cdk.Environment(account="123456789012", region="us-east-2")
    )
    Template.from_stack(stack).has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "LambdaConfig": Match.object_like(
                {"PreTokenGenerationConfig": Match.object_like({"LambdaVersion": "V2_0"})}
            )
        },
    )

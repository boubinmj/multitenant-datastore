import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from edge_api.edge_stack import EdgeStack


def _template() -> Template:
    app = cdk.App()
    stack = EdgeStack(
        app,
        "TestEdgeStack",
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    return Template.from_stack(stack)


def test_user_pool_has_immutable_tenant_id():
    _template().has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "Schema": Match.array_with(
                [Match.object_like({"Name": "tenant_id", "Mutable": False})]
            )
        },
    )


def test_tenant_and_admin_groups_exist():
    t = _template()
    t.resource_count_is("AWS::Cognito::UserPoolGroup", 2)
    t.has_resource_properties("AWS::Cognito::UserPoolGroup", {"GroupName": "tenant_user"})
    t.has_resource_properties("AWS::Cognito::UserPoolGroup", {"GroupName": "admin"})


def test_sdk_and_console_clients_exist():
    t = _template()
    t.resource_count_is("AWS::Cognito::UserPoolClient", 2)
    t.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {"ClientName": "sdk-client", "GenerateSecret": False},
    )
    t.has_resource_properties(
        "AWS::Cognito::UserPoolClient", {"ClientName": "console-client"}
    )


def test_clients_cannot_write_tenant_id():
    t = _template()
    clients = t.find_resources("AWS::Cognito::UserPoolClient")
    for client in clients.values():
        assert "custom:tenant_id" not in client["Properties"]["WriteAttributes"]

from aws_cdk import CfnOutput, Stack
from constructs import Construct

from edge_api.identity import Identity
from edge_api.token_claims import TokenClaims


class EdgeStack(Stack):
    """Edge layer: ID-01 identity, ID-02 token claims (later: authorizer, API)."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.identity = Identity(self, "Identity")
        self.token_claims = TokenClaims(
            self, "TokenClaims", user_pool=self.identity.user_pool
        )

        CfnOutput(self, "UserPoolId", value=self.identity.user_pool.user_pool_id)
        CfnOutput(
            self, "SdkClientId", value=self.identity.sdk_client.user_pool_client_id
        )
        CfnOutput(
            self,
            "ConsoleClientId",
            value=self.identity.console_client.user_pool_client_id,
        )
        CfnOutput(self, "HostedUiBaseUrl", value=self.identity.domain.base_url())

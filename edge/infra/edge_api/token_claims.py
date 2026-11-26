"""ID-02: wires the pre-token-generation Lambda into the user pool."""

from pathlib import Path

from aws_cdk import Duration
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_lambda as _lambda
from constructs import Construct

LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambdas" / "pre_token"


class TokenClaims(Construct):
    def __init__(
        self, scope: Construct, construct_id: str, user_pool: cognito.UserPool
    ) -> None:
        super().__init__(scope, construct_id)

        self.function = _lambda.Function(
            self,
            "PreTokenFn",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(str(LAMBDA_DIR)),
            timeout=Duration.seconds(5),
            description="ID-02: adds tenant_id and role claims to Cognito tokens",
        )

        # V2 is required to customise the ACCESS token (not just the ID token).
        user_pool.add_trigger(
            cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG,
            self.function,
            cognito.LambdaVersion.V2_0,
        )

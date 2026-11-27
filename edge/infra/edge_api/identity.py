"""ID-01: Cognito user pool for tenant users and admins."""

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from constructs import Construct

TENANT_USER_GROUP = "tenant_user"
ADMIN_GROUP = "admin"


class Identity(Construct):
    """One user pool; tenant users and admins are separated by groups.

    Every user carries an immutable custom:tenant_id attribute that only an
    admin can set (at user creation), so a client can never change its tenant.
    """

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="mtds-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            custom_attributes={
                "tenant_id": cognito.StringAttribute(
                    min_len=1, max_len=64, mutable=False
                ),
            },
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        cognito.UserPoolGroup(
            self,
            "TenantUserGroup",
            user_pool=self.user_pool,
            group_name=TENANT_USER_GROUP,
            description="End users of a single tenant",
        )
        cognito.UserPoolGroup(
            self,
            "AdminGroup",
            user_pool=self.user_pool,
            group_name=ADMIN_GROUP,
            description="Operators who manage tenants",
        )

        read_attrs = (
            cognito.ClientAttributes()
            .with_standard_attributes(email=True, email_verified=True)
            .with_custom_attributes("tenant_id")
        )
        write_attrs = cognito.ClientAttributes().with_standard_attributes(email=True)

        token_lifetimes = dict(
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        self.sdk_client = self.user_pool.add_client(
            "SdkClient",
            user_pool_client_name="sdk-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            prevent_user_existence_errors=True,
            read_attributes=read_attrs,
            write_attributes=write_attrs,
            **token_lifetimes,
        )

        self.console_client = self.user_pool.add_client(
            "ConsoleClient",
            user_pool_client_name="console-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=["http://localhost:5173/callback"],
                logout_urls=["http://localhost:5173/"],
            ),
            prevent_user_existence_errors=True,
            read_attributes=read_attrs,
            write_attributes=write_attrs,
            **token_lifetimes,
        )

        self.domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"mtds-auth-{Stack.of(self).account}",
            ),
        )

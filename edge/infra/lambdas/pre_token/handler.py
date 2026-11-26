"""ID-02: Cognito pre-token-generation trigger (V2).

Adds `tenant_id` and `role` claims to both the ID token and the access token,
so the API authorizer (ID-03) never has to look them up.
"""

ADMIN_GROUP = "admin"
TENANT_USER_GROUP = "tenant_user"


def build_claims(user_attributes: dict, groups: list) -> dict:
    role = "admin" if ADMIN_GROUP in groups else TENANT_USER_GROUP
    claims = {"role": role}

    tenant_id = (user_attributes or {}).get("custom:tenant_id")
    if tenant_id:
        claims["tenant_id"] = tenant_id
    elif role != "admin":
        # A tenant user without a tenant is misconfigured: refuse to issue tokens.
        raise ValueError("User has no tenant_id")
    return claims


def lambda_handler(event, context):
    request = event.get("request", {})
    groups = (request.get("groupConfiguration") or {}).get("groupsToOverride") or []
    claims = build_claims(request.get("userAttributes", {}), groups)

    event.setdefault("response", {})["claimsAndScopeOverrideDetails"] = {
        "idTokenGeneration": {"claimsToAddOrOverride": claims},
        "accessTokenGeneration": {"claimsToAddOrOverride": claims},
    }
    return event

# Auth tokens (ID-02)

Issued by Cognito user pool `mtds-users` (`us-east-2_vwGvJcu85`, us-east-2).

## Custom claims (in both ID and access tokens)

| Claim | Type | Example | Source |
|---|---|---|---|
| `tenant_id` | string | `tenant-a` | `custom:tenant_id` user attribute (immutable, admin-set) |
| `role` | `tenant_user` or `admin` | `tenant_user` | Cognito group membership |

Admins may have no `tenant_id`. A tenant user without one cannot get a token.

## Which token to send

Clients send the access token in `Authorization: Bearer <token>`.
The authorizer (ID-03) validates it against the pool JWKS and checks
`token_use == "access"` and that `client_id` is one of our app clients.

## Lifetimes

| Token | Lifetime |
|---|---|
| Access | 1 hour |
| ID | 1 hour |
| Refresh | 30 days |

## App clients

| Client | ID | Auth flows |
|---|---|---|
| `sdk-client` | `2pei2arukr56bnjf0capp53urh` | USER_PASSWORD, SRP |
| `console-client` | `2ogaaanbhush7hiicojk0rsbad` | Hosted UI (auth code) |

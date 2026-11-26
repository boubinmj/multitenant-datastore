"""S3 key layout for the tenant data bucket.

Plain Python with no CDK imports, so worker and controller code can import it too.

    tenants/{tenant_id}/gen-{epoch}/snapshots/
    tenants/{tenant_id}/gen-{epoch}/wal/
    migrations/v{n}/

S3 has no real folders; these prefixes exist once objects are written under them.
"""

TENANTS_PREFIX = "tenants/"
MIGRATIONS_PREFIX = "migrations/"

# When a newer generation replaces an old one, tag every object in the old
# generation with GENERATION_STATE_TAG=GENERATION_RETIRED. The bucket's lifecycle
# rule expires tagged objects (see storage.py). Untagged objects never expire.
GENERATION_STATE_TAG = "generation-state"
GENERATION_RETIRED = "retired"


def tenant_prefix(tenant_id: str) -> str:
    # A "/" in the ID could put keys under another tenant's prefix.
    if not tenant_id or "/" in tenant_id:
        raise ValueError(f"invalid tenant_id: {tenant_id!r}")
    return f"{TENANTS_PREFIX}{tenant_id}/"


def generation_prefix(tenant_id: str, epoch: int) -> str:
    return f"{tenant_prefix(tenant_id)}gen-{epoch}/"


def snapshots_prefix(tenant_id: str, epoch: int) -> str:
    return f"{generation_prefix(tenant_id, epoch)}snapshots/"


def wal_prefix(tenant_id: str, epoch: int) -> str:
    return f"{generation_prefix(tenant_id, epoch)}wal/"


def migration_prefix(version: int) -> str:
    return f"{MIGRATIONS_PREFIX}v{version}/"

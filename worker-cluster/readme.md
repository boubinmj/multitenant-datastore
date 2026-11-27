## Worker Cluster

The worker cluster stores each tenant's data in its own SQLite file and runs SQL against it over an HTTP API. It's defined with AWS CDK (Python) and deploys to **us-east-2** as a single stack, `WorkerClusterStack`.

| Ticket | What it adds | Code |
|---|---|---|
| CL-01 | Private network: VPC, one private subnet in one AZ, VPC endpoints | [infra/worker_cluster/network.py](infra/worker_cluster/network.py) |
| CL-02 | S3 bucket for tenant snapshots, WAL segments and migrations | [infra/worker_cluster/storage.py](infra/worker_cluster/storage.py), [storage_layout.py](infra/worker_cluster/storage_layout.py) |
| CL-03 | Worker HTTP API running as one ECS Fargate task | [service/](service/), [infra/worker_cluster/compute.py](infra/worker_cluster/compute.py) |
| (temporary) | Stand-in `placement` and `tenants` tables until the control plane creates the real ones | [infra/worker_cluster/control_plane_standins.py](infra/worker_cluster/control_plane_standins.py), [service/app/placement.py](service/app/placement.py), [service/app/tenants.py](service/app/tenants.py) |
| Query-task guards | Lease + epoch check, ACTIVE check, `mode=rw`, authorizer, timeout/row/size limits | [service/app/main.py](service/app/main.py) |

### Layout

```
worker-cluster/
├── infra/                      CDK app
└── service/                    worker API (FastAPI), built into the 
```

### Architecture

- **Network** The VPC has no internet gateway and no NAT. Resources inside it reach AWS services only through VPC endpoints:
  - Gateway endpoints (free): S3 and DynamoDB. S3 is also needed for image pulls, because ECR stores image layers in S3.
  - Interface endpoints: ECR API, ECR Docker registry and CloudWatch Logs.
- **Storage** A private bucket encrypted with the AWS managed key (`aws/s3`), which only accepts HTTPS requests. Keys are laid out like this:
  ```
  tenants/{tenant_id}/gen-{epoch}/ltx/...   (written by Litestream, see Replication)
  migrations/v{n}/
  ```
  A lifecycle rule deletes objects tagged `generation-state=retired` after 30 days.
- **Worker service (CL-03).** One Fargate task (0.25 vCPU, 512 MiB) in the private subnet, with no public IP. Port 8080 accepts traffic only from inside the VPC. The task role can read and write `tenants/*` and can only read `migrations/*`. Logs go to CloudWatch and are kept for one week.

### Control-plane tables (temporary)

Each query task needs to know whether it owns a tenant and whether the tenant is active. The control plane's `placement` and `tenants` tables record this (see the architecture diagram). Until those tables exist, this stack creates stand-in DynamoDB tables with the same shape. The deployed task reaches them through the DynamoDB gateway endpoint and is allowed only `GetItem` and `UpdateItem` on each.

`tenants` holds `tenant_id` (partition key) and `status` (`ACTIVE`, `SUSPENDED`, ...). Client: [service/app/tenants.py](service/app/tenants.py).

`placement`:

| Attribute | Type | Meaning |
|---|---|---|
| `tenant_id` | S | Partition key |
| `owner_task` | S | Owning task's ID; absent when nobody owns the tenant |
| `epoch` | N | +1 on every acquire; never reset. Matches `gen-{epoch}` in S3 |
| `lease_until` | N | Lease expiry, Unix time in milliseconds |

This protocol is a **proposal** and still needs to be agreed with the control plane owner.

Worker code goes through `get_placement_client()` in [service/app/placement.py](service/app/placement.py), which provides:

- `acquire(tenant, task)`: succeeds only if nobody owns the tenant or the lease has expired. Increments the epoch.
- `renew(lease)`: succeeds only if the owner and epoch are unchanged and the lease hasn't expired.
- `release(lease)`: clears the owner but keeps the epoch.
- `holds(lease)`: the per-request guard.

All of these use conditional writes, so two tasks can never both own a tenant.

**Switching to the real tables.** Nothing in the worker code names a table directly. To switch:

1. In [worker_cluster_stack.py](infra/worker_cluster/worker_cluster_stack.py), replace `ControlPlaneStandins(...)` with `dynamodb.Table.from_table_name(...)` for each table, or `from_table_arn(...)` if a table is in another region or account. Then delete `infra/worker_cluster/control_plane_standins.py`.
2. If the real tables use different attribute names, add `PLACEMENT_KEY_ATTR`, `PLACEMENT_OWNER_ATTR`, `PLACEMENT_EPOCH_ATTR`, `PLACEMENT_LEASE_ATTR`, `TENANTS_KEY_ATTR` or `TENANTS_STATUS_ATTR` to the container environment in [compute.py](infra/worker_cluster/compute.py).
3. Once the control plane assigns owners and creates tenants, `POST /tenants/{id}` should stop acquiring the lease and registering the tenant itself (see below). Then remove `dynamodb:UpdateItem` from the `TenantStatus` policy.

### Replication (Litestream)

The worker copies every tenant it owns to S3 continuously with [Litestream](https://litestream.io) v0.5.18. Changes are pushed about once a second to `s3://<tenant bucket>/tenants/{id}/gen-{epoch}/`. Code: [service/app/replication.py](service/app/replication.py).

#### Dynamic configuration: how it works

**The problem.** Tenants are added and removed while the task is running, and each tenant needs its own S3 path (its `gen-{epoch}`) and possibly its own KMS key. Litestream reads its config file once at startup and can't reload it. Its directory-watch mode (`dir:` + `watch: true`) does pick up new files, but it uses **one** replica path and **one** KMS key for the whole directory, so it can't do per-tenant paths or keys.

**The approach: one Litestream process per tenant, managed by the worker.** Each process is a child of the worker in the same container, not a separate sidecar container.

| Event | What the worker does |
|---|---|
| Tenant created on this task (`POST /tenants/{id}`) | Writes `/data/litestream/{id}.yml` and starts `litestream replicate -config /data/litestream/{id}.yml` |
| Lease lost (failed renew, takeover, or found expired by a request) | Sends SIGTERM to that tenant's process **right away** and deletes its config. The process is reaped later; the request isn't held up |
| Epoch changes | Stops the old process and starts a new one pointed at the new `gen-{epoch}` |
| Every heartbeat (10 s) | Reconciles: starts processes for held leases that have none (or whose process crashed), stops processes for tenants no longer held, and kills processes that ignored SIGTERM for 35 s |
| Task shutdown | SIGTERM to every process, then waits for Litestream's final sync (up to 30 s). Only then are leases released. The container's stop timeout is 60 s to allow for this |

Each per-tenant config file looks like this (JSON, which is valid YAML):

```json
{"dbs": [{"path": "/data/tenants/acme.db",
          "replica": {"type": "s3", "bucket": "<tenant bucket>", "path": "tenants/acme/gen-3",
                      "region": "us-east-2", "sync-interval": "1s",
                      "sse-kms-key-id": "<key, if any>"}}]}
```

**Why one process per tenant.** It's the only setup that gives each tenant its own path and key without restarting replication for *every* tenant whenever one changes. Stopping one process is also the most direct way to make a stale owner stop writing to S3. The cost is one process per tenant, so how many tenants fit in a 512 MiB task still needs to be measured.

**Fencing.** Each owner writes only under its own `gen-{epoch}`. If an old owner's process hasn't exited yet, its last writes go to the old generation and can never mix with the new owner's.

#### Encryption keys (per-tenant KMS)

The worker picks each tenant's key in this order:

1. The tenant's own key: the `kms_key_arn` attribute on its `tenants` item (the attribute name can be changed with `TENANTS_KMS_KEY_ATTR`).
2. A default key: `LITESTREAM_DEFAULT_KMS_KEY_ID` in the container environment.
3. Neither: the bucket's default encryption (the AWS managed `aws/s3` key).

**Who creates per-tenant keys hasn't been decided.** The worker code doesn't depend on that decision. Today no keys exist, so backups use option 3. When keys are introduced, the task role also needs `kms:GenerateDataKey` and `kms:Decrypt` on them. No KMS VPC endpoint is needed, because S3 calls KMS on the worker's behalf.

#### Settings

| Env var | Default | Meaning |
|---|---|---|
| `TENANT_BUCKET_NAME` | set by CDK | Bucket to replicate to. **If unset, replication is off** (for example under `docker compose`) |
| `TENANT_BUCKET_REGION` | set by CDK | Bucket region |
| `LITESTREAM_SYNC_INTERVAL` | `1s` | How often changes are pushed |
| `LITESTREAM_DEFAULT_KMS_KEY_ID` | unset | Fallback KMS key |
| `LITESTREAM_CONFIG_DIR` | `/data/litestream` | Where per-tenant config files are written |

Litestream's retention deletes old files, so the task role has `s3:DeleteObject` on `tenants/*`.

### Worker API

| Method | Path | Body | Result |
|---|---|---|---|
| `GET` | `/health` | | `{"status": "ok", "task_id": "..."}` |
| `POST` | `/tenants/{id}` | | **Stand-in for control-plane provisioning:** takes the tenant's lease, registers it as `ACTIVE` if new, and creates `{id}.db` in WAL mode. Returns `201` with `owner_task` and `epoch` |
| `POST` | `/tenants/{id}/execute` | `{"sql": "...", "params": [...] or {...}}` | `{"columns": [...], "rows": [...], "rowcount": n}` |

Errors look like `{"detail": {"code": "WRONG_OWNER", "message": "..."}}`. The router should act on `code`.

**Guards on every `/execute` request**, in this order:

1. **Validated tenant ID.** It must match `[A-Za-z0-9_-]{1,64}`, and the file path is built only from it.
2. **Lease + epoch.** This task must hold the tenant's lease at the current epoch. The lease is checked three times:
   - locally first, against the task's own copy of `lease_until` minus a 2 s margin for clock differences, with no network call;
   - then with a strongly consistent read of `placement`;
   - and again after the request takes the tenant lock.

   **Heartbeat:** a background thread renews held leases every 10 s, and each lease lasts 30 s. Each renewal is a conditional write on owner + epoch. If it fails, the lease is dropped at once and the tenant gets `409` from then on. If DynamoDB can't be reached, the task keeps the lease while it's still valid and gives it up once it expires. Leases are released on shutdown.
3. **ACTIVE.** The tenant's status in `tenants` must be `ACTIVE`.
4. **`mode=rw` open.** SQLite opens the file read-write and never creates it, so a missing file is an error rather than a new, empty database.
5. **Authorizer.** It denies `ATTACH`/`DETACH`, `load_extension`, and every `PRAGMA` except read-only introspection (`table_info`, `table_xinfo`, `table_list`, `index_list`, `index_info`, `index_xinfo`, `foreign_key_list`). No databases can be attached at all, which also blocks `VACUUM INTO`.
6. **Limits.** These can be set with environment variables:

   | Limit | Env var | Default |
   |---|---|---|
   | Statement timeout | `STATEMENT_TIMEOUT_SECONDS` | 5 |
   | Rows returned | `MAX_ROWS` | 1000 |
   | Database size | `MAX_DB_BYTES` | 100 MiB |
   | SQL length | (constant) | 100,000 characters |

| Status | `code` | Meaning |
|---|---|---|
| 400 | `INVALID_TENANT_ID` | Tenant ID fails the allowlist |
| 400 | `SQL_ERROR` | SQLite error, denied by the authorizer, or more than one statement |
| 400 | `ROW_LIMIT` | Result has more than `MAX_ROWS` rows; add a `LIMIT` |
| 403 | `TENANT_NOT_ACTIVE` | Tenant is suspended or not in `tenants` |
| 404 | `TENANT_NOT_FOUND` | This task owns the tenant but has no file for it |
| 408 | `STATEMENT_TIMEOUT` | Statement ran longer than the timeout and was rolled back |
| 409 | `WRONG_OWNER` | This task doesn't hold the tenant's lease at the current epoch. The router should look up the owner again and retry |
| 409 | `TENANT_EXISTS` | `POST /tenants/{id}` for a tenant this task already has |
| 413 | `DB_SIZE_LIMIT` | The write would make the database larger than `MAX_DB_BYTES` |
| 503 | `PLACEMENT_UNAVAILABLE` | DynamoDB can't be reached, so ownership can't be confirmed. Safe to retry |

Other rules:

- **One statement per request.** Each request commits on its own (autocommit).
- **Single writer per tenant.** An in-memory lock per tenant means only one statement touches a file at a time. This only works with exactly one server process. The Dockerfile runs `uvicorn --workers 1`, and the service never runs two tasks at once. Don't change either without replacing the locks.

### Setup

Run from the repo root. The root [requirements.txt](../requirements.txt) installs the CDK libraries, the test tools and the service's runtime dependencies.

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You also need Docker (for `cdk deploy` and local runs), the AWS CDK CLI (`npm install -g aws-cdk`), and AWS credentials for the target account.

### Tests

```
cd worker-cluster/service && pytest   # API tests
cd worker-cluster/infra && pytest     # CDK tests (check the synthesized CloudFormation template)
```

### Run the API locally

The deployed API can only be reached from inside the VPC, so test it locally. The worker needs the `placement` and `tenants` tables even locally, so [service/compose.yaml](service/compose.yaml) runs it next to DynamoDB Local and creates the tables at startup:

```
cd worker-cluster/service
docker compose up --build
```

Then, from another terminal:

```
curl localhost:8080/health
curl -X POST localhost:8080/tenants/acme
curl -X POST localhost:8080/tenants/acme/execute \
  -H 'content-type: application/json' \
  -d '{"sql": "CREATE TABLE t (x INTEGER)"}'
curl -X POST localhost:8080/tenants/acme/execute \
  -H 'content-type: application/json' \
  -d '{"sql": "INSERT INTO t VALUES (?)", "params": [1]}'
curl -X POST localhost:8080/tenants/acme/execute \
  -H 'content-type: application/json' \
  -d '{"sql": "SELECT * FROM t"}'
```

Stop with Ctrl+C, then `docker compose down`. All data is in memory and is lost when you stop. A plain `docker run` of the image fails at startup because no tables are configured.

### Deploy

Docker must be running: `cdk deploy` builds the image from `service/` and pushes it to the CDK bootstrap ECR repository.

#### One-time setup: Docker credential store (Linux with Docker Desktop)

To push the image, `cdk deploy` runs `docker login` to ECR, and Docker has to save that login somewhere. If Docker Desktop is installed, `~/.docker/config.json` contains `"credsStore": "desktop"`. On Linux that saves logins in [`pass`](https://www.passwordstore.org/), a password manager encrypted with a GPG key. If `pass` hasn't been set up, the deploy fails with:

```
error storing credentials ... pass not initialized ... Error: password store is empty. Try "pass init".
```

Set it up once per machine:

1. Create a GPG key. Enter your name and email when prompted, and choose a passphrase:
   ```
   gpg --generate-key
   ```
2. Copy the key ID gpg prints (a 40-character hex string). To see it again later, run `gpg --list-secret-keys`.
3. Create the password store with that key:
   ```
   pass init <KEY_ID>
   ```
4. Re-run the deploy. During `docker login`, you may be asked for the GPG passphrase.

To check the setup without deploying, log in to ECR directly:
```
aws ecr get-login-password --region us-east-2 \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-2.amazonaws.com
```
It should print `Login Succeeded`.

#### Deploy commands

```
cd worker-cluster/infra
cdk bootstrap        # once per account, in us-east-2
cdk deploy
```

The stack outputs `VpcId`, `PrivateSubnetId`, `TenantBucketName`, `ClusterName`, `ServiceName`, `PlacementTableName` and `TenantsTableName`. To check the deploy worked:

1. In the ECS console, open the cluster and confirm the service's task is **RUNNING**.
2. In CloudWatch Logs, open the stack's log group (stream prefix `worker`) and look for `Uvicorn running on http://0.0.0.0:8080`.

Each deploy stops the old task before starting the new one, so expect a short outage.

### Tear down

```
cd worker-cluster/infra
cdk destroy
```

> **Warning:** `cdk destroy` empties and deletes the tenant bucket. **All tenant data in S3 is deleted.**

### Known limitations

- **Backups exist, but nothing restores them yet.** Litestream copies each tenant to S3, but when a task takes over a tenant it starts with an empty database instead of restoring the previous generation. Until restore-on-acquire exists, a replaced task still means the tenant's data isn't available on the new task, though it's safe in S3.
- **Replication doesn't run locally.** `docker compose` sets no bucket, so replication is off.
- **Litestream's behavior on SIGTERM isn't fully confirmed.** Its docs say it does a final sync on "a shutdown signal" without naming which signals. Check the logs on the first real shutdown.
- **No access from outside the VPC.** There's no load balancer or public endpoint yet.
- **One task, one AZ.** No high availability, and there's an outage during each deploy.
- **No authentication on the API.** Anything inside the VPC can call it.
- **The lease is checked before a statement, not during it.** A statement already running when the lease is lost can still finish. The S3 `gen-{epoch}` prefixes are meant to stop such a stale owner's writes from reaching durable storage, once uploads exist.
- **Each `/execute` makes two DynamoDB reads** (placement and tenants), with no caching.

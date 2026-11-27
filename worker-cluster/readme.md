## Worker Cluster

The worker cluster stores each tenant's data in its own SQLite file and runs SQL against it over an HTTP API. It's defined with AWS CDK (Python) and deploys to **us-east-2** as a single stack, `WorkerClusterStack`.

| Ticket | What it adds | Code |
|---|---|---|
| CL-01 | Private network: VPC, one private subnet in one AZ, VPC endpoints | [infra/worker_cluster/network.py](infra/worker_cluster/network.py) |
| CL-02 | S3 bucket for tenant snapshots, WAL segments and migrations | [infra/worker_cluster/storage.py](infra/worker_cluster/storage.py), [storage_layout.py](infra/worker_cluster/storage_layout.py) |
| CL-03 | Worker HTTP API running as one ECS Fargate task | [service/](service/), [infra/worker_cluster/compute.py](infra/worker_cluster/compute.py) |

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
  tenants/{tenant_id}/gen-{epoch}/snapshots/
  tenants/{tenant_id}/gen-{epoch}/wal/
  migrations/v{n}/
  ```
  A lifecycle rule deletes objects tagged `generation-state=retired` after 30 days.
- **Worker service (CL-03).** One Fargate task (0.25 vCPU, 512 MiB) in the private subnet, with no public IP. Port 8080 accepts traffic only from inside the VPC. The task role can read and write `tenants/*` and can only read `migrations/*`. Logs go to CloudWatch and are kept for one week.

### Worker API

| Method | Path | Body | Result |
|---|---|---|---|
| `GET` | `/health` | | `{"status": "ok"}` |
| `POST` | `/tenants/{id}` | | Creates `{id}.db` in WAL mode. `201`, or `409` if the tenant exists |
| `POST` | `/tenants/{id}/execute` | `{"sql": "...", "params": [...] or {...}}` | `{"columns": [...], "rows": [...], "rowcount": n}`. `404` if the tenant doesn't exist, `400` on a SQL error |

Rules:

- A tenant ID must match `[A-Za-z0-9_-]{1,64}`.
- **One statement per request.** Each request commits on its own (autocommit).
- `ATTACH`, `DETACH` and `PRAGMA` are rejected, so a tenant can't open another tenant's file or change the journal mode.
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

The deployed API can only be reached from inside the VPC, so test it locally with Docker:

```
docker build -t worker worker-cluster/service
docker run --rm -p 8080:8080 worker

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

The stack outputs `VpcId`, `PrivateSubnetId`, `TenantBucketName`, `ClusterName` and `ServiceName`. To check the deploy worked:

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

- **Tenant databases are lost when the task is replaced** (deploy, crash or restart). The `.db` files live on the task's temporary disk, and nothing uploads snapshots or WAL segments to S3 yet.
- **No access from outside the VPC.** There's no load balancer or public endpoint yet.
- **One task, one AZ.** No high availability, and there's an outage during each deploy.
- **No authentication on the API.** Anything inside the VPC can call it.

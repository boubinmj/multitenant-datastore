# Scalable Multi-Tenant Database Service

A managed database service that gives each tenant its own isolated SQLite database. Tenants are provisioned on demand through an API, and the service handles storage, backups, scaling, and schema migrations.

## Goals

- **On-demand provisioning:** create a tenant's database through an API in seconds.
- **Strong isolation:** one database file per tenant, so no cross-tenant data leakage and each tenant can be backed up, restored, or deleted independently.
- **Horizontal scalability:** spread databases across compute nodes behind stateless API and routing layers.
- **Managed operations:** automated backups, monitoring, health checks, and lifecycle management (create, suspend, delete).
- **Cross-tenant migrations (P1):** apply versioned schema migrations safely across all or selected tenants.
- **Offline sync (P2):** let clients work on a local SQLite copy and sync changes back with conflict resolution.

## Features

- Tenant management API (`POST /tenants`, `GET /tenants/{id}`, `DELETE /tenants/{id}`, suspend/resume)
- Parameterized SQL query API over REST or a lightweight SDK
- Tenant routing layer that maps tenant IDs to the node or storage location holding their database
- Authentication and authorization via Amazon Cognito
- Per-tenant resource limits (database size, request rate, query timeout)
- Scheduled snapshots to S3 and point-in-time restore
- Per-tenant metrics, structured CloudWatch logs, and alarms

## Architecture

| Layer | AWS service |
| --- | --- |
| API entry | API Gateway (or ALB) |
| Authentication | Cognito / Lambda authorizer |
| Control plane | Lambda |
| Data plane | ECS on Fargate (or EC2 Auto Scaling) running the SQLite query service |
| Database storage | EFS (or EBS with S3 as source of truth) |
| Object storage | S3 (backups, WAL segments, migration scripts) |
| Tenant catalog | DynamoDB |
| Orchestration | Step Functions |
| Async processing | SQS with DLQ |
| Scheduling | EventBridge |
| Secrets and encryption | KMS, Secrets Manager |
| Monitoring | CloudWatch, X-Ray |

## Roadmap

- **P1 — Schema migrations:** versioned migration registry, per-tenant `schema_migrations` tracking, parallel fan-out with canary rollout, pre-migration snapshots, and rollback.
- **P2 — Offline sync:** client SDK with change tracking, push/pull protocol keyed by per-tenant sync tokens, configurable conflict resolution (last-write-wins by default), and resumable, idempotent sync.

## Team

Matt Boubin, Khushi Agrawal, Vaishnavi Rathod

See [docs/Project Plan_ Scalable Multi-Tenant Database Service.pdf](docs/Project%20Plan_%20Scalable%20Multi-Tenant%20Database%20Service.pdf) for the full plan.

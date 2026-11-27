# Litestream binary, copied from the official image at build time: the task runs in a VPC
# with no internet access, so nothing can be downloaded at runtime.
FROM litestream/litestream:0.5.18 AS litestream
# The docs don't state where the binary lives in the image, so look it up instead of
# hard-coding a path.
RUN cp "$(command -v litestream)" /litestream-bin

FROM python:3.12-slim

COPY --from=litestream /litestream-bin /usr/local/bin/litestream

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/

# Tenant .db files live on the task's ephemeral storage; Litestream configs next to them.
RUN useradd --create-home worker \
    && mkdir -p /data/tenants /data/litestream \
    && chown -R worker /data
USER worker
ENV DATA_DIR=/data/tenants LITESTREAM_CONFIG_DIR=/data/litestream

EXPOSE 8080
# One process only: the per-tenant locks that enforce a single writer are in-memory.
# uvicorn is PID 1, so it gets ECS's SIGTERM and its shutdown hook stops Litestream
# (final sync) before releasing leases.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

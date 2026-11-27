FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/

# Tenant .db files live on the task's ephemeral storage.
RUN useradd --create-home worker && mkdir -p /data/tenants && chown -R worker /data
USER worker
ENV DATA_DIR=/data/tenants

EXPOSE 8080
# One process only: the per-tenant locks that enforce a single writer are in-memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

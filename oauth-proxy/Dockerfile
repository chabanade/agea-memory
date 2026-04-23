FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY oauth_proxy.py .
COPY templates ./templates

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8081/healthz || exit 1

CMD ["uvicorn", "oauth_proxy:app", "--host", "0.0.0.0", "--port", "8081", "--log-level", "info"]

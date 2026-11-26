# GCP AI Usage & Spend Caps API - Headless Container Image
#
# Build: docker build -t gcp-ai-usage-caps-api:1.1.0 .
# Run:   docker run --rm -p 127.0.0.1:8080:8080 \
#          -e PROJECT_ID=my-project \
#          -e BOOTSTRAP_ADMIN_KEY=gcpai_bootstrap_change_me \
#          gcp-ai-usage-caps-api:1.1.0
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data \
    && chmod 0700 /app/data

COPY config.yaml /app/config.yaml
COPY openapi.yaml /app/openapi.yaml
COPY src/ /app/src/

USER 10001:10001

EXPOSE 8080

CMD ["python3", "-u", "src/server.py"]

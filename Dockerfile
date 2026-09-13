# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md requirements.lock ./
COPY app ./app
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.lock && \
    /opt/venv/bin/pip install --no-deps .

FROM python:3.12-slim-bookworm

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_BIND_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    AUDIT_DB_PATH=/data/audit.db

RUN groupadd --system --gid 10001 awita && \
    useradd --system --uid 10001 --gid awita --home-dir /nonexistent --shell /usr/sbin/nologin awita && \
    install -d -o awita -g awita -m 0700 /data
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
USER 10001:10001
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]
ENTRYPOINT ["awita-mail"]

# awLPay v1 — production image.
#
# Deterministic build: pinned base tag, pinned pip requirements
# (server/requirements.txt, all ==), --no-cache-dir, non-root runtime
# user, no build secrets. The relayer key is NEVER baked in — it
# arrives at runtime via Fly secrets (see DEPLOY.md).
#
# Build:  docker build -t awlpay:1.0 .
# Run:    docker run -p 8080:8080 -e AWL_TEST_MODE=0 awlpay:1.0

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    AWL_MODE=prod

# Non-root runtime user (no shell, no home write beyond /tmp).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin awlpay

WORKDIR /app

# Install deps first (layer cache friendly) as root, then drop privs.
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

COPY server/ /app/server/

RUN chown -R 10001:10001 /app
USER 10001

EXPOSE 8080

# Fly's http_service.checks hits /healthz; the Docker HEALTHCHECK is a
# backstop for non-Fly runtimes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# access log off + warning level: stdout stays clean JSON (one line per
# request from server/logging.py).
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", "--log-level", "warning"]

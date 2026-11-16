# Vocto as an always-on server for a team (docs/server.md).
#   docker compose up -d        then open http://<server>:8765 and create the admin account.
# Everything that changes (test projects, runs, secrets, speech models, users) lives in the /data volume.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VATF_MODELS_DIR=/data/.models \
    VOCTO_URL_POLICY=server

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "uv==0.11.32"

WORKDIR /app
# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY schemas ./schemas
COPY src ./src
RUN uv sync --frozen --no-dev

# A user of its own: the server never runs as root.
RUN useradd --system --create-home --uid 10001 vocto \
 && mkdir -p /data && chown vocto:vocto /data
USER vocto
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8765/api/auth/status', headers={'Host': 'localhost'}), timeout=4)"]

ENTRYPOINT ["/app/.venv/bin/vatf"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--root", "/data", "--runs-dir", "runs"]

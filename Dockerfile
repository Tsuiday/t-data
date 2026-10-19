FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 \
    ASX_SHORTS_DB=/data/asx_shorts.sqlite PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && mkdir -p /data
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
VOLUME ["/data"]
EXPOSE 8000

# Starts as root only long enough to chown the mounted volume, then runs as `app`.
ENTRYPOINT ["docker-entrypoint.sh"]
# Proxy headers on: Fly's edge terminates TLS.
CMD ["uvicorn", "asx_shorts.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

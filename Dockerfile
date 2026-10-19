FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy ASX_SHORTS_DB=/data/asx_shorts.sqlite

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8000

# Proxy headers on: the platform's load balancer terminates TLS.
CMD ["uv", "run", "--no-dev", "uvicorn", "asx_shorts.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

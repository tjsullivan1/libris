# The hosted MCP server (#171, ADR 0035). Runs the same uvicorn command used
# locally (ADR 0007), configured entirely from environment variables.
#
#   docker build -t libris-hosted .
#   docker run -p 8000:8000 --env-file hosted.env libris-hosted

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra hosted --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra hosted --no-editable

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home libris
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER libris
EXPOSE 8000
# The Container App's ingress terminates TLS and forwards to this port, so the
# proxy headers are trusted to say the request arrived over https.
CMD ["uvicorn", "--factory", "libris.hosted:create_app_from_env", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

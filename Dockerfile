# Stage 1: build the React page to static files.
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: runtime. Python 3.12 + uv for the app, Node 22 for the Claude CLI (ADR 0007).
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    OLIST_DB=/app/olist.duckdb \
    FASTEMBED_CACHE_PATH=/app/.models
COPY --from=ghcr.io/astral-sh/uv:0.8.14 /uv /uvx /bin/
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm i -g @anthropic-ai/claude-code@2.1.312

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY pipeline/ pipeline/
COPY analyst/ analyst/
COPY web/ web/
# The database is built in the image, so the first start is fast. No secrets are needed for this.
# The committed embeddings cache comes along, so the build embeds only texts it lacks (none).
COPY data/olist.zip data/review_embeddings.parquet data/
RUN uv run python -m pipeline --zip data/olist.zip --db /app/olist.duckdb
COPY --from=frontend /frontend/dist frontend/dist

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]

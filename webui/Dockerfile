FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY search/ ./search/
COPY db.py ./
COPY webui/app.py ./
COPY webui/static/ ./static/

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]

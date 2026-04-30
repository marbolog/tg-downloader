FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Install dependencies first — separate layer so rebuilds are fast when only source changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# Copy source
COPY *.py ./

# data/ holds the session file and downloaded files (mounted from the host)
RUN mkdir -p data

# Keep the container alive; the tool is run interactively via docker compose exec
CMD ["sleep", "infinity"]

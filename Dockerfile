FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Install dependencies first — separate layer so rebuilds are fast when only source changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# Copy source
COPY *.py ./

# data/ holds the session file, the SQLite database, and downloaded files (mounted from host)
RUN mkdir -p data

# Start the real-time listener as the main process
CMD ["uv", "run", "python", "main.py", "listen"]

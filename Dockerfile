FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies first (layer cache)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY signal_engine.py signal_api.py ./
COPY ai/ ./ai/
COPY core/ ./core/
COPY prompts/ ./prompts/

# Railway injects PORT — default to 8000
ENV PORT=8000

CMD ["uv", "run", "uvicorn", "signal_api:app", "--host", "0.0.0.0", "--port", "8000"]

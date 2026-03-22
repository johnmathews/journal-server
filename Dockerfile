FROM python:3.13-slim AS base

RUN pip install uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev

ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/data/journal.db
ENV CHROMADB_HOST=chromadb
ENV CHROMADB_PORT=8000

EXPOSE 8000

CMD ["uv", "run", "python", "-m", "journal.mcp_server"]

# Production Dockerfile for VaultSwap on Google Cloud Run
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Run unified ASGI server (serves Flask Web UI + /mcp Streamable HTTP on port 8080)
CMD exec uvicorn asgi:asgi_app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-keep-alive 65

# services/api-gateway/Dockerfile
#
# Build context is this directory (see docker-compose.yml); `shared/` and
# `src/` are bind-mounted read-only at runtime by docker-compose, so the
# COPY below only matters when running this image outside compose.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

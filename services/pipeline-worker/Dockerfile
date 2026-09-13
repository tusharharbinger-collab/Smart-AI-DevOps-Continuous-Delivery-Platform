# services/pipeline-worker/Dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl docker.io git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

EXPOSE 8001
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]

FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn prometheus-client
COPY main.py .
EXPOSE 8080
CMD ["python", "main.py"]

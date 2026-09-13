# services/verification-engine/Dockerfile
#
# Python 3.11 per spec §2. Deliberately does NOT install the `kubernetes`
# package and mounts no kubeconfig — the structural boundary from §8.1.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

EXPOSE 8002
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8002"]

# Control-plane image (API + dashboard). Talks to the host Docker daemon via the mounted socket.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY intrallm_sandbox ./intrallm_sandbox
RUN pip install --no-cache-dir .

ENV SANDBOX_DB_PATH=/data/sandbox.db
VOLUME ["/data"]
EXPOSE 8080
CMD ["intrallm-sandbox", "serve", "--host", "0.0.0.0", "--port", "8080"]

# Default image for sandboxes created by the IntraLLM agent.
# Build: docker build -t intrallm/sandbox:latest sandbox-image/
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq less procps findutils coreutils \
      build-essential nodejs npm \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy pandas matplotlib requests pyyaml openpyxl

RUN useradd --create-home --uid 1000 --shell /bin/bash sandbox \
 && mkdir -p /workspace && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace
ENV HOME=/home/sandbox PYTHONUNBUFFERED=1 MPLBACKEND=Agg
CMD ["sleep", "infinity"]

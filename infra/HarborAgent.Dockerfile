# Demo-only agent image. The simulator and grading secrets remain in the sidecar.
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl python3 coreutils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install --global opencode-ai@1.19.5
WORKDIR /app

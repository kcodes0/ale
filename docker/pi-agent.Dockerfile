FROM node:22-bookworm

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    openssh-client \
    python3 \
    python3-pip \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g @mariozechner/pi-coding-agent

RUN useradd -m -s /bin/bash pi \
  && mkdir -p /workspace /home/pi/.pi \
  && chown -R pi:pi /workspace /home/pi

# Run as root inside the container so mounted workspaces and mounted auth files
# are readable/writable regardless of the host service user's UID. The container
# itself is still the isolation boundary; do not run this image outside Docker.
ENV HOME=/home/pi
WORKDIR /workspace

CMD ["sh", "-lc", "pi -p \"$PI_TASK_PROMPT\""]

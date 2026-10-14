# Immagine base unica per tutti i progetti.
# Build: docker build -t claude-project:latest ./project-image
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client tmux less nano \
        build-essential python3 python3-pip python3-venv nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Le cartelle che diventano volumi devono esistere già con il proprietario giusto:
# Docker copia la proprietà nel volume vuoto al primo montaggio.
RUN mkdir -p /workspace /home/ubuntu/.claude \
    && chown -R ubuntu:ubuntu /workspace /home/ubuntu/.claude

# Utente non-root (in ubuntu:24.04 esiste già "ubuntu", uid 1000).
# Serve anche perché il CLI rifiuta la modalità bypass quando gira come root.
USER ubuntu
WORKDIR /home/ubuntu

# Installer nativo di Claude Code
RUN curl -fsSL https://claude.ai/install.sh | bash

ENV PATH=/home/ubuntu/.local/bin:$PATH \
    CLAUDE_CONFIG_DIR=/home/ubuntu/.claude

# NON impostare qui CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, ANTHROPIC_API_KEY
# o CLAUDE_CODE_OAUTH_TOKEN: disattivano o bloccano Remote Control.

WORKDIR /workspace
CMD ["bash", "-l"]

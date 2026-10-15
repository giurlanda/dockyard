# The Base Image: every Project environment starts from it and every Image an Administrator
# defines derives from it. Claude Code, git, gh, glab, Node and Python, run by the non-root user
# "ubuntu" (uid 1000).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg jq less nano openssh-client procps \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Node.js (LTS) and gh, from their official apt repositories.
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/*

# glab: the latest release for this architecture.
RUN arch=$(dpkg --print-architecture) \
    && url=$(curl -fsSL "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases/permalink/latest" \
         | jq -r --arg a "$arch" '.assets.links[] | select(.name | test("linux_" + $a + "\\.tar\\.gz$"; "i")) | .url' \
         | head -1) \
    && test -n "$url" \
    && mkdir /tmp/glab && curl -fsSL "$url" | tar -xz -C /tmp/glab \
    && install -m 0755 "$(find /tmp/glab -type f -name glab | head -1)" /usr/local/bin/glab \
    && rm -rf /tmp/glab

# Claude Code. The native installer puts it in the home, but a Project's home is a persistent
# volume where the binary would stay frozen at its first version: move it out of the home and
# switch the auto-updater off. Claude Code is updated by rebuilding this image.
USER ubuntu
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root
RUN install -m 0755 "$(readlink -f /home/ubuntu/.local/bin/claude)" /usr/local/bin/claude \
    && rm -rf /home/ubuntu/.local /home/ubuntu/.claude /home/ubuntu/.claude.json /home/ubuntu/.cache \
       /home/ubuntu/.npm

# ~/.claude receives the User's Claude config; /run/dockyard receives the Project's Git Credential
# at every start (a tmpfs over it).
RUN mkdir -p /workspace /home/ubuntu/.claude /run/dockyard \
    && chown ubuntu:ubuntu /workspace /home/ubuntu/.claude /run/dockyard \
    && chmod 0700 /home/ubuntu/.claude /run/dockyard

USER ubuntu
ENV CLAUDE_CONFIG_DIR=/home/ubuntu/.claude \
    DISABLE_AUTOUPDATER=1
# Never a volume mount point: with Docker 29 that resets the volume root to root:root.
WORKDIR /workspace
CMD ["bash", "-l"]

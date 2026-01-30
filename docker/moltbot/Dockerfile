# Extend official moltbot image with additional tooling
FROM ghcr.io/moltbot/moltbot:main

USER root

# Install system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Install glab (GitLab CLI)
RUN curl -fsSL https://gitlab.com/gitlab-org/cli/-/releases/v1.46.1/downloads/glab_1.46.1_linux_amd64.deb -o /tmp/glab.deb \
    && dpkg -i /tmp/glab.deb \
    && rm /tmp/glab.deb

# Install yq (YAML processor)
RUN curl -fsSL https://github.com/mikefarah/yq/releases/download/v4.44.1/yq_linux_amd64 -o /usr/local/bin/yq \
    && chmod +x /usr/local/bin/yq

# Install bun (for qmd and fast JS tooling)
RUN curl -fsSL https://bun.sh/install | bash \
    && mv /root/.bun/bin/bun /usr/local/bin/ \
    && rm -rf /root/.bun

# Install qmd (quick markdown search) globally
RUN bun install -g https://github.com/tobi/qmd

# Ensure node user can access installed tools
RUN chmod -R a+rx /usr/local/bin

USER node

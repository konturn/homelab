# Extend official OpenClaw image with additional tooling
FROM ghcr.io/openclaw/openclaw:2026.2.17@sha256:1f28ead56a68352c2bbbbd4fb45523a237f271dc3be827b073b9274986a2a829

USER root

# Install system packages including Chromium for headless browser automation
# Chromium deps: minimal set for headless operation (no X11 desktop)
RUN apt-get update && apt-get install -y --no-install-recommends \
    jq \
    ipmitool \
    chromium \
    fonts-liberation \
    fonts-noto-color-emoji \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libnss3 \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Set Chromium flags for container/sandbox environment
ENV CHROMIUM_FLAGS="--headless --no-sandbox --disable-gpu --disable-dev-shm-usage"
ENV PUPPETEER_EXECUTABLE_PATH="/usr/bin/chromium"
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH="/usr/bin/chromium"

# Install glab (GitLab CLI) from GitHub
RUN curl -fsSL https://github.com/profclems/glab/releases/download/v1.22.0/glab_1.22.0_Linux_x86_64.deb -o /tmp/glab.deb \
    && dpkg -i /tmp/glab.deb \
    && rm /tmp/glab.deb

# Install yq (YAML processor)
RUN curl -fsSL https://github.com/mikefarah/yq/releases/download/v4.44.1/yq_linux_amd64 -o /usr/local/bin/yq \
    && chmod +x /usr/local/bin/yq

# Install bun (for qmd and fast JS tooling)
RUN curl -fsSL https://bun.sh/install | bash \
    && mv /root/.bun/bin/bun /usr/local/bin/ \
    && rm -rf /root/.bun

# Install qmd (quick markdown search) - clone, build, and install
RUN cd /tmp \
    && git clone --depth 1 https://github.com/tobi/qmd.git \
    && cd qmd \
    && bun install \
    && bun run build \
    && mkdir -p /usr/local/lib/qmd \
    && cp -r dist /usr/local/lib/qmd/ \
    && cp qmd /usr/local/lib/qmd/qmd.sh \
    && ln -sf /usr/local/lib/qmd/qmd.sh /usr/local/bin/qmd \
    && cd / && rm -rf /tmp/qmd

# Install playwright globally first, then agent-browser
# Uses system Chromium via PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
RUN npm install -g playwright \
    && npm install -g agent-browser \
    && agent-browser install --skip-download

# Ensure node user can access installed tools
RUN chmod -R a+rx /usr/local/bin

USER node

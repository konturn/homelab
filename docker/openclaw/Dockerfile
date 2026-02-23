# Extend official OpenClaw image with additional tooling
FROM ghcr.io/openclaw/openclaw:2026.2.21@sha256:ce271192cd70250d16fc5911903d9953467a40faf8b34e87cbd042e6b49b6036

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
# Must include package.json + node_modules alongside dist/ because the
# wrapper runs via node and needs to resolve imports (fast-glob, etc.)
RUN cd /tmp \
    && git clone --depth 1 https://github.com/tobi/qmd.git \
    && cd qmd \
    && bun install \
    && bun run build \
    && mkdir -p /usr/local/lib/qmd \
    && cp -r dist package.json node_modules /usr/local/lib/qmd/ \
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

# Extend official OpenClaw image with additional tooling
FROM ghcr.io/openclaw/openclaw:2026.2.22@sha256:51c3fe85913545ca6c15c0fe2447230167ed1d329a07088f67d2a1612541b64b

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

# Install playwright globally first, then agent-browser
# Uses system Chromium via PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
RUN npm install -g playwright \
    && npm install -g agent-browser \
    && agent-browser install --skip-download

# Pre-install clawrouter extension
RUN mkdir -p /home/node/.openclaw/extensions/clawrouter \
    && npm install --prefix /home/node/.openclaw/extensions/clawrouter @blockrun/clawrouter@0.10.5

# Ensure node user can access installed tools
RUN chmod -R a+rx /usr/local/bin

USER node

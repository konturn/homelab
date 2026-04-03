# Extend official OpenClaw image with additional tooling
FROM ghcr.io/openclaw/openclaw:2026.4.2

USER root

# Install system packages including Chromium for headless browser automation
# Chromium deps: minimal set for headless operation (no X11 desktop)
RUN apt-get update && apt-get install -y --no-install-recommends \
    jq \
    ipmitool \
    unzip \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-fonts-recommended \
    texlive-fonts-extra \
    lmodern \
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
    # Pentesting / security tools
    nmap \
    hydra \
    sqlmap \
    dirb \
    net-tools \
    dnsutils \
    whois \
    tcpdump \
    netcat-openbsd \
    openssh-client \
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

# Install nuclei (template-based vulnerability scanner)
RUN curl -fsSL https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_amd64.zip -o /tmp/nuclei.zip \
    && unzip /tmp/nuclei.zip -d /tmp/nuclei \
    && mv /tmp/nuclei/nuclei /usr/local/bin/nuclei \
    && chmod +x /usr/local/bin/nuclei \
    && rm -rf /tmp/nuclei /tmp/nuclei.zip

# Install ffuf (web fuzzer)
RUN curl -fsSL https://github.com/ffuf/ffuf/releases/download/v2.1.0/ffuf_2.1.0_linux_amd64.tar.gz -o /tmp/ffuf.tar.gz \
    && tar -xzf /tmp/ffuf.tar.gz -C /usr/local/bin ffuf \
    && chmod +x /usr/local/bin/ffuf \
    && rm /tmp/ffuf.tar.gz

# Install testssl.sh (TLS configuration testing)
RUN curl -fsSL https://github.com/testssl/testssl.sh/archive/refs/tags/v3.2.3.tar.gz -o /tmp/testssl.tar.gz \
    && tar -xzf /tmp/testssl.tar.gz -C /opt \
    && ln -s /opt/testssl.sh-3.2.3/testssl.sh /usr/local/bin/testssl \
    && rm /tmp/testssl.tar.gz

# Install trivy (container image vulnerability scanner)
RUN curl -fsSL https://github.com/aquasecurity/trivy/releases/download/v0.69.3/trivy_0.69.3_Linux-64bit.deb -o /tmp/trivy.deb \
    && dpkg -i /tmp/trivy.deb \
    && rm /tmp/trivy.deb

# Install nikto (web server vulnerability scanner) - not in base image repos
RUN git clone --depth 1 https://github.com/sullo/nikto.git /opt/nikto \
    && ln -s /opt/nikto/program/nikto.pl /usr/local/bin/nikto \
    && chmod +x /opt/nikto/program/nikto.pl

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

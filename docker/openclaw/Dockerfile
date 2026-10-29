# Extend official OpenClaw image with additional tooling
FROM ghcr.io/openclaw/openclaw:2026.7.1-2@sha256:8789721d2e9b24b780a1504b56deb4c6bd5c7dbf96a1dd117e7c45c2ed72c8ac

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
    # nikto runtime dependencies (XML::Writer is required; Net::SSLeay for HTTPS targets)
    libxml-writer-perl \
    libnet-ssleay-perl \
    && rm -rf /var/lib/apt/lists/*

# Set Chromium flags for container/sandbox environment
ENV CHROMIUM_FLAGS="--headless --no-sandbox --disable-gpu --disable-dev-shm-usage"
ENV PUPPETEER_EXECUTABLE_PATH="/usr/bin/chromium"
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH="/usr/bin/chromium"
# Playwright-managed browsers are baked into the image (see the tools layer
# below), root-owned so the agent cannot replace them at runtime.
ENV PLAYWRIGHT_BROWSERS_PATH="/opt/ms-playwright"

# Every download below is pinned to a release and verified with sha256sum -c.
# Hashes come from each project's published checksums file where one exists
# (glab, yq, nuclei, ffuf, trivy); testssl.sh publishes none, so its tag
# archive hash was recorded when the pin was set.

# Install glab (GitLab CLI) from GitHub
RUN curl -fsSL https://github.com/profclems/glab/releases/download/v1.22.0/glab_1.22.0_Linux_x86_64.deb -o /tmp/glab.deb \
    && echo "5761f235ee5ef358dabcb8a5d86013572eb504cc43237b84c258114ab3fd7c27  /tmp/glab.deb" | sha256sum -c - \
    && dpkg -i /tmp/glab.deb \
    && rm /tmp/glab.deb

# Install yq (YAML processor)
RUN curl -fsSL https://github.com/mikefarah/yq/releases/download/v4.44.1/yq_linux_amd64 -o /usr/local/bin/yq \
    && echo "6dc2d0cd4e0caca5aeffd0d784a48263591080e4a0895abe69f3a76eb50d1ba3  /usr/local/bin/yq" | sha256sum -c - \
    && chmod +x /usr/local/bin/yq

# Install nuclei (template-based vulnerability scanner)
RUN curl -fsSL https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_amd64.zip -o /tmp/nuclei.zip \
    && echo "72dbc77e87b3a6751adc151e514b15f51e2c94e493a561fc9e1138397c6e22c6  /tmp/nuclei.zip" | sha256sum -c - \
    && unzip /tmp/nuclei.zip -d /tmp/nuclei \
    && mv /tmp/nuclei/nuclei /usr/local/bin/nuclei \
    && chmod +x /usr/local/bin/nuclei \
    && rm -rf /tmp/nuclei /tmp/nuclei.zip

# Install ffuf (web fuzzer)
RUN curl -fsSL https://github.com/ffuf/ffuf/releases/download/v2.1.0/ffuf_2.1.0_linux_amd64.tar.gz -o /tmp/ffuf.tar.gz \
    && echo "fc2c82736c14dcbea4daf3d3cf3878c1c4773008ba45c2bc0fceba7d17b40bb5  /tmp/ffuf.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/ffuf.tar.gz -C /usr/local/bin ffuf \
    && chmod +x /usr/local/bin/ffuf \
    && rm /tmp/ffuf.tar.gz

# Install testssl.sh (TLS configuration testing)
RUN curl -fsSL https://github.com/testssl/testssl.sh/archive/refs/tags/v3.2.3.tar.gz -o /tmp/testssl.tar.gz \
    && echo "1c4bb10185a67592164eb870c717b8bdd03f290c8d68f9a8c658335ff5ac8b91  /tmp/testssl.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/testssl.tar.gz -C /opt \
    && ln -s /opt/testssl.sh-3.2.3/testssl.sh /usr/local/bin/testssl \
    && rm /tmp/testssl.tar.gz

# Install trivy (container image vulnerability scanner)
RUN curl -fsSL https://github.com/aquasecurity/trivy/releases/download/v0.69.3/trivy_0.69.3_Linux-64bit.deb -o /tmp/trivy.deb \
    && echo "a484057aafde31089cf2558ca0f79a4bc835125a5ee6834183a5bcf0735af358  /tmp/trivy.deb" | sha256sum -c - \
    && dpkg -i /tmp/trivy.deb \
    && rm /tmp/trivy.deb

# Install nikto (web server vulnerability scanner) - not in base image repos.
# Pinned to a commit: git fetches that exact object and checks its hash, so the
# tree cannot drift with the upstream branch. .git is dropped after checkout.
ARG NIKTO_COMMIT=312645d873478a77986627ab1fc8cffe595e85d4
RUN git init -q /opt/nikto \
    && git -C /opt/nikto fetch -q --depth 1 https://github.com/sullo/nikto.git "${NIKTO_COMMIT}" \
    && git -C /opt/nikto checkout -q --detach FETCH_HEAD \
    && test "$(git -C /opt/nikto rev-parse HEAD)" = "${NIKTO_COMMIT}" \
    && rm -rf /opt/nikto/.git \
    && ln -s /opt/nikto/program/nikto.pl /usr/local/bin/nikto \
    && chmod +x /opt/nikto/program/nikto.pl

# playwright + agent-browser from tools/package-lock.json (exact versions and
# integrity hashes; npm ci fails on any mismatch). Install scripts are skipped:
# agent-browser ships its native binary in the tarball, and its postinstall
# only chmods it. The old /usr/local/lib/node_modules paths are kept as
# symlinks for scripts that require() them by absolute path.
# Browsers: `playwright install chromium` fetches the Chromium and headless
# shell builds named in the locked playwright-core's browsers.json, so the
# browser revision is tied to the playwright version. agent-browser finds them
# through PLAYWRIGHT_BROWSERS_PATH.
COPY tools/package.json tools/package-lock.json /opt/openclaw-tools/
RUN cd /opt/openclaw-tools \
    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && chmod 755 node_modules/agent-browser/bin/agent-browser-linux-x64 \
    && ln -s /opt/openclaw-tools/node_modules/agent-browser/bin/agent-browser-linux-x64 /usr/local/bin/agent-browser \
    && ln -s /opt/openclaw-tools/node_modules/playwright/cli.js /usr/local/bin/playwright \
    && ln -s /opt/openclaw-tools/node_modules/playwright /usr/local/lib/node_modules/playwright \
    && ln -s /opt/openclaw-tools/node_modules/agent-browser /usr/local/lib/node_modules/agent-browser \
    && playwright install chromium \
    && chmod -R a+rX /opt/ms-playwright \
    && npm cache clean --force

# Install Claude CLI for the openclaw claude-cli backend (auth via CLAUDE_CODE_OAUTH_TOKEN env)
# Pinned: Opus 5.5 (claude-opus-5-5) needs >= 2.1.296; older CLIs reject the model ID
RUN npm install -g @anthropic-ai/claude-code@2.1.296

# Bundled diagnostics-otel plugin: install its production dependencies at
# build time from the upstream npm-shrinkwrap.json (exact versions and
# integrity hashes), so container start fetches nothing. package.json's
# devDependencies use `workspace:*`, which npm cannot resolve outside the
# upstream monorepo; the shrinkwrap has no dev entries, so dropping them keeps
# package.json and the lockfile in sync for `npm ci`.
RUN cd /app/extensions/diagnostics-otel \
    && npm pkg delete devDependencies \
    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && chown -R node:node node_modules \
    && npm cache clean --force

# Ensure node user can access installed tools
RUN chmod -R a+rx /usr/local/bin

USER node

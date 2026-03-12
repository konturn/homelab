FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    SCREEN_WIDTH=1920 \
    SCREEN_HEIGHT=1080 \
    SCREEN_DEPTH=24

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-dejavu-core \
    fonts-freefont-ttf \
    dbus-x11 \
    procps \
    curl \
    gosu \
    socat \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with UID 1000 to match openclaw's 'node' user
# This ensures shared volume mounts (/uploads) have matching ownership
RUN groupadd -g 1000 chrome && useradd -u 1000 -g chrome -G audio,video -m -s /bin/bash chrome \
    && mkdir -p /data/chrome-profile && chown -R chrome:chrome /data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Stealth extension for anti-detection patches
COPY stealth-extension /opt/stealth-ext

EXPOSE 6080 9222

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=30s \
    CMD curl -f http://localhost:9222/json/version || exit 1

ENTRYPOINT ["/entrypoint.sh"]

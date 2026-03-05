FROM telegraf:1.37.3@sha256:6aa6970c52c86d0dac03a66dc6d8daac7a7852b31648e5e4575859bc271df924

RUN echo "deb http://deb.debian.org/debian bookworm non-free" >> /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    smartmontools \
    lm-sensors \
    ipmitool \
    snmp-mibs-downloader \
    nvme-cli \
    jq \
    && rm -rf /var/lib/apt/lists/*

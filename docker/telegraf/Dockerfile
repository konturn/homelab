FROM telegraf:1.37.3@sha256:6aa6970c52c86d0dac03a66dc6d8daac7a7852b31648e5e4575859bc271df924

RUN apt-get update && apt-get install -y --no-install-recommends \
    smartmontools \
    lm-sensors \
    ipmitool \
    && rm -rf /var/lib/apt/lists/*

#!/bin/bash
mkdir -p /var/cache/apt/arm64/archives/partial /var/cache/apt/amd64/archives/partial
apt-get autoclean
apt install -y netcat
/usr/lib/rhasspy/bin/rhasspy-voltron --user-profiles /profiles --profile en

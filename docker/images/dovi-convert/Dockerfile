# dovi-convert: Dolby Vision Profile 7 -> 8.1 conversion toolbox.
#
# Every tool is pinned: the base image by index digest, the Debian packages by
# exact version (apt checks them against the signed archive metadata), and
# dovi_tool by release version plus the sha256 of its tarball.
# Bump a version: change it here and update the matching checksum/digest.
FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4

# UTF-8 locale: under the default C locale mkvmerge truncates its JSON (and
# mangles names) at the first non-ASCII track name.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

ARG DOVI_TOOL_VERSION=2.3.4
ARG DOVI_TOOL_SHA256=1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg=7:7.1.5-0+deb13u1 \
      jq \
      mediainfo=25.04-1 \
      mkvtoolnix=92.0-1+deb13u1 \
 && curl -fsSL -o /tmp/dovi_tool.tar.gz \
      "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
 && echo "${DOVI_TOOL_SHA256}  /tmp/dovi_tool.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin ./dovi_tool \
 && chmod 0755 /usr/local/bin/dovi_tool \
 && rm /tmp/dovi_tool.tar.gz \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY dv-p7-to-p8.sh dv-probe.sh /usr/local/bin/

# Fail the build if a tool is missing or not runnable.
RUN dovi_tool --version && mkvmerge --version && ffmpeg -version | head -1 && mediainfo --version

ENTRYPOINT ["/usr/local/bin/dv-p7-to-p8.sh"]

# PETLIBRO PLAF203 camera -> RTSP, for Frigate (docs/petlibro-camera.md).
#
# go2rtc with the petlibro:// source (a pure-Go TUTK/Kalay LAN client, no
# vendor libraries, no cloud account), built from the source release that
# tannerln7/Petlibro-Home-Assistant 0.3.14 pins (addon/go2rtc-source.lock).
# Reviewed copy: private GitLab project root/petlibro-local, tag
# v0.3.14-homelab.1 (HOMELAB.md), which also keeps a copy of this tarball in
# its package registry. Only go2rtc is built: the project's MQTT controller,
# installer and State Agent are not used.
#
# Bumps are manual: review the new go2rtc source, update the fork, then the
# version, URL and SHA-256 below.
FROM golang:1.27.3-alpine3.23@sha256:5d5113301ffcac84e2bfb20c42648803fecaea6789c89bb9b66884e18cbb30a7 AS build

ARG GO2RTC_VERSION=v1.9.14-petlibro.1
ARG GO2RTC_SOURCE_SHA256=a3ee426178fe94426927d356f02009d6299e41a511bf967d29a77396b2de7c78
ARG GO2RTC_SOURCE_URL=https://github.com/tannerln7/go2rtc/releases/download/v1.9.14-petlibro.1/go2rtc_v1.9.14-petlibro.1_source.tar.gz

RUN set -eu; \
    wget -q -O /tmp/src.tar.gz "${GO2RTC_SOURCE_URL}"; \
    echo "${GO2RTC_SOURCE_SHA256}  /tmp/src.tar.gz" | sha256sum -c -; \
    mkdir -p /src; \
    tar -xzf /tmp/src.tar.gz --strip-components=1 -C /src; \
    rm /tmp/src.tar.gz; \
    test -f /src/internal/petlibro/petlibro.go
WORKDIR /src
RUN go mod download && go mod verify
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /out/go2rtc .

FROM alpine:3.23.8@sha256:5c216c9767869f6ec7c4718977601d52f087c5c7bf2fdfb198e403f6b48ec40d

COPY --from=build /out/go2rtc /usr/local/bin/go2rtc
COPY go2rtc.yaml /etc/go2rtc/go2rtc.yaml

# Unprivileged; only UDP to the camera and the RTSP listener (>1024).
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/go2rtc", "-config", "/etc/go2rtc/go2rtc.yaml"]

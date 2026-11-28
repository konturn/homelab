FROM rust:slim-trixie@sha256:3b199fd6db7d709274df56565b8f4003f9e30d7bfcff9d53148c86053b7855e2
RUN apt update &&  DEBIAN_FRONTEND=noninteractive apt install -y wget pkg-config libasound2-dev
RUN wget https://github.com/badaix/snapcast/releases/download/v0.34.0/snapclient_0.34.0-1_amd64_trixie.deb && apt install -y ./snapclient_0.34.0-1_amd64_trixie.deb
ENTRYPOINT ["/usr/bin/snapclient"]

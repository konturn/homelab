FROM rust:slim-trixie
RUN apt update &&  DEBIAN_FRONTEND=noninteractive apt install -y wget pkg-config libasound2-dev
RUN wget https://github.com/badaix/snapcast/releases/download/v0.34.0/snapclient_0.34.0-1_amd64_trixie.deb && apt install -y ./snapclient_0.34.0-1_amd64_trixie.deb
ENTRYPOINT ["/usr/bin/snapclient"]

FROM rust:slim-trixie
RUN apt update &&  DEBIAN_FRONTEND=noninteractive apt install -y wget pkg-config libasound2-dev libssl-dev
RUN wget https://github.com/badaix/snapcast/releases/download/v0.34.0/snapserver_0.34.0-1_amd64_trixie.deb && apt install -y ./snapserver_0.34.0-1_amd64_trixie.deb
RUN wget https://github.com/librespot-org/librespot/archive/refs/tags/v0.8.0.tar.gz && tar -xvf v0.8.0.tar.gz
RUN cd librespot-0.8.0 && cargo build --release
RUN mv librespot-0.8.0/target/release/librespot /bin/
RUN mkdir -p /root/.config/snapserver && echo '{"ConfigVersion": 2,"Groups": []}' > /root/.config/snapserver/server.json
ENTRYPOINT ["/usr/bin/snapserver"]

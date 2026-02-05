FROM rust:slim-bullseye
RUN apt update &&  DEBIAN_FRONTEND=noninteractive apt install -y wget pkg-config libasound2-dev
RUN wget https://github.com/badaix/snapcast/releases/download/v0.34.0/snapserver_0.34.0-1_amd64_bullseye.deb && apt install -y ./snapserver_0.34.0-1_amd64_bullseye.deb
RUN wget https://github.com/librespot-org/librespot/archive/refs/tags/v0.4.2.tar.gz && tar -xvf v0.4.2.tar.gz
RUN cd librespot-0.4.2 && cargo build
RUN mv librespot-0.4.2/target/debug/librespot /bin/
RUN mkdir -p /root/.config/snapserver && echo '{"ConfigVersion": 2,"Groups": []}' > /root/.config/snapserver/server.json
RUN apt update && apt install -y --no-install-recommends build-essential git autoconf automake libtool \
    libpopt-dev libconfig-dev libasound2-dev avahi-daemon libavahi-client-dev libssl-dev libsoxr-dev \
    libplist-dev libsodium-dev libavutil-dev libavcodec-dev libavformat-dev uuid-dev libgcrypt-dev xxd
RUN git clone https://github.com/mikebrady/shairport-sync.git && cd shairport-sync && autoreconf -fi && ./configure --with-stdout --with-avahi --with-ssl=openssl --with-metadata && make && make install
ENTRYPOINT ["/usr/bin/snapserver"]

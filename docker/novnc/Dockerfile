FROM python:3.12.14-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

RUN pip install --no-cache-dir websockify

RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/novnc && \
    wget -qO- https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz | \
    tar xz --strip-components=1 -C /opt/novnc

EXPOSE 6080

ENV VNCSERVER=localhost
ENV VNCPORT=5900

CMD websockify --web=/opt/novnc 6080 ${VNCSERVER}:${VNCPORT}

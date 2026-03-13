FROM python:3.12-slim

RUN pip install --no-cache-dir websockify

RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/novnc && \
    wget -qO- https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz | \
    tar xz --strip-components=1 -C /opt/novnc

EXPOSE 6080

ENV VNCSERVER=localhost
ENV VNCPORT=5900

CMD websockify --web=/opt/novnc 6080 ${VNCSERVER}:${VNCPORT}

# Patched amcrest2mqtt: fix TLS + password auth being mutually exclusive
# and support server-only TLS (no client certificates required).
#
# Upstream bugs:
# 1. tls_set() and username_pw_set() are in if/else branches, so enabling
#    TLS disables password authentication.
# 2. TLS mode requires client certificates (MQTT_TLS_CERT, MQTT_TLS_KEY),
#    but most MQTT brokers only need server-side TLS for encryption with
#    password-based client authentication.
#
# Upstream source: https://github.com/dchesterton/amcrest2mqtt
FROM dchesterton/amcrest2mqtt:1.0.16@sha256:5a503bcf7b39bd846ca60be1846ce8b73eae979fd074f2a31e14462f85447260

COPY patch.py /tmp/patch.py
RUN python3 /tmp/patch.py && rm /tmp/patch.py

# 3. Upstream exits as soon as the camera is unreachable, which turned an
#    offline doorbell into a container restart loop. The entrypoint supervises
#    the app instead: it waits in place (no exit) until the camera answers,
#    and only exits on a failure while the camera is reachable. See
#    entrypoint.sh. Setting ENTRYPOINT clears the CMD inherited from the base
#    image, so it is restated below.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-u", "/app/amcrest2mqtt.py"]

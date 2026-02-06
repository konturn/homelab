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
FROM dchesterton/amcrest2mqtt:latest

COPY patch.py /tmp/patch.py
RUN python3 /tmp/patch.py && rm /tmp/patch.py

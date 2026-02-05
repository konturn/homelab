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

# Apply both patches via a Python script for reliability over sed.
RUN python3 -c "
import re

with open('/app/amcrest2mqtt.py', 'r') as f:
    code = f.read()

# Patch 1: Make TLS client certs optional instead of required.
# Replace the block that exits on missing MQTT_TLS_CERT/KEY with optional usage.
old_tls = '''    if mqtt_tls_ca_cert is None:
        log(\"Missing var: MQTT_TLS_CA_CERT\", level=\"ERROR\")
        sys.exit(1)
    if mqtt_tls_cert is None:
        log(\"Missing var: MQTT_TLS_CERT\", level=\"ERROR\")
        sys.exit(1)
    if mqtt_tls_cert is None:
        log(\"Missing var: MQTT_TLS_KEY\", level=\"ERROR\")
        sys.exit(1)
    mqtt_client.tls_set(
        ca_certs=mqtt_tls_ca_cert,
        certfile=mqtt_tls_cert,
        keyfile=mqtt_tls_key,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS,
    )'''

new_tls = '''    tls_kwargs = {\"tls_version\": ssl.PROTOCOL_TLS}
    if mqtt_tls_ca_cert:
        tls_kwargs[\"ca_certs\"] = mqtt_tls_ca_cert
        tls_kwargs[\"cert_reqs\"] = ssl.CERT_REQUIRED
    else:
        tls_kwargs[\"cert_reqs\"] = ssl.CERT_NONE
    if mqtt_tls_cert and mqtt_tls_key:
        tls_kwargs[\"certfile\"] = mqtt_tls_cert
        tls_kwargs[\"keyfile\"] = mqtt_tls_key
    mqtt_client.tls_set(**tls_kwargs)'''

assert old_tls in code, 'Could not find TLS block to patch'
code = code.replace(old_tls, new_tls)

# Patch 2: Always set username/password when provided (not just non-TLS).
# Change 'else:' before username_pw_set to 'if mqtt_username is not None:'
old_auth = '''else:
    mqtt_client.username_pw_set(mqtt_username, password=mqtt_password)'''

new_auth = '''if mqtt_username is not None:
    mqtt_client.username_pw_set(mqtt_username, password=mqtt_password)'''

assert old_auth in code, 'Could not find auth block to patch'
code = code.replace(old_auth, new_auth)

with open('/app/amcrest2mqtt.py', 'w') as f:
    f.write(code)

print('Patches applied successfully')
"

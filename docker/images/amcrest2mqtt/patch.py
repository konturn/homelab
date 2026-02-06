"""Patch amcrest2mqtt to fix TLS + password auth being mutually exclusive."""

with open("/app/amcrest2mqtt.py", "r") as f:
    code = f.read()

# Patch 1: Make TLS client certs optional instead of required.
# Replace the block that exits on missing MQTT_TLS_CERT/KEY with optional usage.
# Server-only TLS (encryption + server verification) is the common case;
# client certificates are only needed for mutual TLS.
old_tls = """    if mqtt_tls_ca_cert is None:
        log("Missing var: MQTT_TLS_CA_CERT", level="ERROR")
        sys.exit(1)
    if mqtt_tls_cert is None:
        log("Missing var: MQTT_TLS_CERT", level="ERROR")
        sys.exit(1)
    if mqtt_tls_cert is None:
        log("Missing var: MQTT_TLS_KEY", level="ERROR")
        sys.exit(1)
    mqtt_client.tls_set(
        ca_certs=mqtt_tls_ca_cert,
        certfile=mqtt_tls_cert,
        keyfile=mqtt_tls_key,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS,
    )"""

new_tls = """    tls_kwargs = {
        "cert_reqs": ssl.CERT_REQUIRED,
        "tls_version": ssl.PROTOCOL_TLS,
    }
    if mqtt_tls_ca_cert:
        tls_kwargs["ca_certs"] = mqtt_tls_ca_cert
    if mqtt_tls_cert and mqtt_tls_key:
        tls_kwargs["certfile"] = mqtt_tls_cert
        tls_kwargs["keyfile"] = mqtt_tls_key
    mqtt_client.tls_set(**tls_kwargs)"""

assert old_tls in code, "Could not find TLS block to patch"
code = code.replace(old_tls, new_tls)

# Patch 2: Always set username/password when provided (not just non-TLS).
# Change 'else:' before username_pw_set to 'if mqtt_username is not None:'
# This decouples TLS (transport encryption) from password auth (identity).
old_auth = """else:
    mqtt_client.username_pw_set(mqtt_username, password=mqtt_password)"""

new_auth = """if mqtt_username is not None:
    mqtt_client.username_pw_set(mqtt_username, password=mqtt_password)"""

assert old_auth in code, "Could not find auth block to patch"
code = code.replace(old_auth, new_auth)

with open("/app/amcrest2mqtt.py", "w") as f:
    f.write(code)

print("Patches applied successfully")

#!/bin/bash
# Generate CA, server, and client certs for Docker socket proxy mTLS
# Usage: ./generate-certs.sh [cert_dir]
set -euo pipefail

CERT_DIR="${1:-.}"
mkdir -p "$CERT_DIR"

# CA
openssl genrsa -out "$CERT_DIR/ca-key.pem" 4096
openssl req -new -x509 -days 3650 -key "$CERT_DIR/ca-key.pem" -sha256 \
    -out "$CERT_DIR/ca.pem" -subj "/CN=docker-proxy-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign"

# Server cert (for the TLS sidecar)
openssl genrsa -out "$CERT_DIR/server-key.pem" 4096
openssl req -new -key "$CERT_DIR/server-key.pem" \
    -out "$CERT_DIR/server.csr" -subj "/CN=lab-nginx-docker-proxy"

cat > "$CERT_DIR/extfile.cnf" <<EOF
subjectAltName=DNS:lab.nkontur.com,DNS:lab_nginx,DNS:lab-nginx
extendedKeyUsage=serverAuth
EOF

openssl x509 -req -days 3650 -in "$CERT_DIR/server.csr" \
    -CA "$CERT_DIR/ca.pem" -CAkey "$CERT_DIR/ca-key.pem" -CAcreateserial \
    -out "$CERT_DIR/server-cert.pem" -extfile "$CERT_DIR/extfile.cnf"

# Client cert (for CI runners)
# Docker expects: ca.pem, cert.pem, key.pem in DOCKER_CERT_PATH
openssl genrsa -out "$CERT_DIR/key.pem" 4096
openssl req -new -key "$CERT_DIR/key.pem" \
    -out "$CERT_DIR/client.csr" -subj "/CN=ci-runner"

cat > "$CERT_DIR/client-extfile.cnf" <<EOF
extendedKeyUsage=clientAuth
EOF

openssl x509 -req -days 3650 -in "$CERT_DIR/client.csr" \
    -CA "$CERT_DIR/ca.pem" -CAkey "$CERT_DIR/ca-key.pem" -CAcreateserial \
    -out "$CERT_DIR/cert.pem" -extfile "$CERT_DIR/client-extfile.cnf"

# Cleanup temp files
rm -f "$CERT_DIR"/*.csr "$CERT_DIR"/*.cnf "$CERT_DIR"/*.srl

# Restrict private key permissions
chmod 600 "$CERT_DIR/ca-key.pem" "$CERT_DIR/server-key.pem" "$CERT_DIR/key.pem"
chmod 644 "$CERT_DIR/ca.pem" "$CERT_DIR/server-cert.pem" "$CERT_DIR/cert.pem"

# Verify cert chains
openssl verify -CAfile "$CERT_DIR/ca.pem" "$CERT_DIR/server-cert.pem"
openssl verify -CAfile "$CERT_DIR/ca.pem" "$CERT_DIR/cert.pem"

echo "Docker proxy TLS certs generated in $CERT_DIR"
echo "  CA:     ca.pem / ca-key.pem"
echo "  Server: server-cert.pem / server-key.pem"
echo "  Client: cert.pem / key.pem (Docker-compatible names)"

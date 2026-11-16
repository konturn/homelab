# Build stage
FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o jit-proxy .

# Runtime: distroless static, non-root (uid 65532), CA certificates only.
# No shell; the healthcheck is the binary itself (jit-proxy -healthcheck).
FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab

COPY --from=builder /app/jit-proxy /usr/local/bin/jit-proxy

USER 65532:65532
EXPOSE 8090 8091

ENTRYPOINT ["/usr/local/bin/jit-proxy"]

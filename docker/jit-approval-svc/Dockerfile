# Build stage
FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o jit-approval-svc .

# Runtime stage
FROM alpine:3.24.4@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf

RUN apk add --no-cache ca-certificates tzdata

RUN adduser -D -u 1000 svc
USER svc

COPY --from=builder /app/jit-approval-svc /usr/local/bin/jit-approval-svc

EXPOSE 8080

ENTRYPOINT ["jit-approval-svc"]

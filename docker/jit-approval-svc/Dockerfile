# Build stage
FROM golang:1.27.3-alpine@sha256:fa3d596898d9f126af7c01d14ee7597bab3f39cccf392ef36b69fe77cd681f18 AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o jit-approval-svc .

# Runtime stage
FROM alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6

RUN apk add --no-cache ca-certificates tzdata

RUN adduser -D -u 1000 svc
USER svc

COPY --from=builder /app/jit-approval-svc /usr/local/bin/jit-approval-svc

EXPOSE 8080

ENTRYPOINT ["jit-approval-svc"]

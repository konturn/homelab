# Build stage
FROM golang:1.27.2-alpine@sha256:85dc1069ac644ea3c527b177303a406eb3358192816cd7f9e5848eb658851673 AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o jit-approval-svc .

# Runtime stage
FROM alpine:3.24.2@sha256:31b6477333eb8257db9e5d7c3a7264fd0467928756f0bbcc27d35bea5d28cdbd

RUN apk add --no-cache ca-certificates tzdata

RUN adduser -D -u 1000 svc
USER svc

COPY --from=builder /app/jit-approval-svc /usr/local/bin/jit-approval-svc

EXPOSE 8080

ENTRYPOINT ["jit-approval-svc"]

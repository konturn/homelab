# Build stage
FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o jit-approval-svc .

# Runtime stage
FROM alpine:3.24.4@sha256:93b41ef40b1677fce8fcc64ff4af09daddfacf5040afde618bc46c1e2d1cc7d4

RUN apk add --no-cache ca-certificates tzdata

RUN adduser -D -u 1000 svc
USER svc

COPY --from=builder /app/jit-approval-svc /usr/local/bin/jit-approval-svc

EXPOSE 8080

ENTRYPOINT ["jit-approval-svc"]

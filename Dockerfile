# syntax=docker/dockerfile:1

FROM golang:1.24-bookworm AS builder


WORKDIR /app

COPY . .

RUN go mod download

ENV CGO_ENABLED=0

RUN go build -a -installsuffix cgo -ldflags="-w -s" -o fingerprint-server

FROM debian:bookworm-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
	python3 \
	python3-pip \
	python3-venv \
	&& rm -rf /var/lib/apt/lists/*


RUN python3 -m venv venv

COPY requirements.txt .

RUN venv/bin/pip install --no-cache-dir -r requirements.txt

COPY capture.py .

COPY --from=builder /app/fingerprint-server .

EXPOSE 8080


CMD ["./fingerprint-server"]

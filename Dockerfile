# syntax=docker/dockerfile:1.6
#
# Multi-stage build for `channel-service` only.
# The `channel` CLI lives on the host (brew / install.sh / cargo) — running
# `channel send` does not need `docker exec` because the CLI just speaks HTTP
# to the service container.

FROM rust:1.82-bookworm AS builder
WORKDIR /build

# Cache deps separately from source for faster incremental builds.
COPY Cargo.toml Cargo.lock ./
COPY src ./src

RUN cargo build --release --bin channel-service

# ─────────────────────────────────────────────────────────────────────────────
# Runtime stage — slim Debian + ca-certificates (reqwest rustls) + curl
# (healthcheck only). No build toolchain, no source.
# ─────────────────────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/target/release/channel-service /usr/local/bin/channel-service

EXPOSE 10101

# Bind to 0.0.0.0 inside the container; docker-compose maps :10101 to 127.0.0.1
# on the host so the service is not exposed externally by default.
ENV SESSION_CHANNEL_PORT=10101

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -fsS "http://localhost:${SESSION_CHANNEL_PORT}/health" || exit 1

ENTRYPOINT ["/usr/local/bin/channel-service"]

# syntax=docker/dockerfile:1.7
# Multi-stage build for session-channel.

FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml ./
COPY README.md ./
COPY cli ./cli
COPY wrappers ./wrappers
COPY scripts ./scripts
COPY templates ./templates
COPY main.py config.py config.yaml routes.py store.py auth.py ./

# Install deps + project into an isolated prefix so we can copy a clean
# layer into the runtime image.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir .

# --------------------------------------------------------------------------

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="session-channel"
LABEL org.opencontainers.image.description="Cross-pane, cross-CLI pub-sub bus over tmux + Redis Streams"
LABEL org.opencontainers.image.source="https://github.com/operonlab/session-channel"
LABEL org.opencontainers.image.licenses="MIT"

# Non-root user
RUN useradd --create-home --shell /bin/bash --uid 10101 channel
WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY --from=build /build /app
RUN chown -R channel:channel /app

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV SESSION_CHANNEL_HOME=/app
ENV SESSION_CHANNEL_PORT=10101

USER channel
EXPOSE 10101

# Health: GET /health is registered in routes.py
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
    sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:10101/health',timeout=2).status==200 else sys.exit(1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10101"]

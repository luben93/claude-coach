# Cycling Coach — self-hosted, LAN-only, single athlete.
#
# Python runs the FastAPI server + Agent SDK. Node is here ONLY so you can run
# `claude setup-token` inside the container to mint a subscription OAuth token
# (the SDK itself is pure Python and the Strava MCP is remote, so nothing else
# needs Node at runtime).
FROM python:3.12-slim

# --- Node + Claude Code CLI (for `claude setup-token` only) ----------------
# curl + bash stay in the image: the bundled brouter script uses them at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates bash \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY brouter /srv/brouter
COPY entrypoint.sh /srv/entrypoint.sh
RUN chmod +x /srv/brouter/fetch_route.sh /srv/entrypoint.sh

# Memory lives ON the volume (/data/memory) — the coach reads & writes it. HOME
# points at the volume so `claude` and the SDK share credentials and everything
# survives restarts.
ENV COACH_DATA_DIR=/data \
    CLAUDE_HOME_DIR=/data/claude-home \
    HOME=/data/claude-home \
    COACH_MEMORY_DIR=/data/memory \
    BROUTER_SCRIPT=/srv/brouter/fetch_route.sh \
    COACH_HOST=0.0.0.0 \
    COACH_PORT=8080

# --- Run as a non-root user --------------------------------------------------
# REQUIRED, not just hygiene: the Claude CLI refuses to operate without
# interactive permission prompts when running as root. A headless server has no
# one to answer prompts, so it must run non-root. We create `coach`, give it the
# app dir and the /data mountpoint, and drop to it.
RUN useradd --create-home --uid 10001 coach \
 && mkdir -p /data \
 && chown -R coach:coach /srv /data
USER coach

VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["/srv/entrypoint.sh"]
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8080"]

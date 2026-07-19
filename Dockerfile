# Syncplay server (https://syncplay.pl) - size-optimized image
#
# Build:
#   docker build -t syncplay-server .
#
# Run (basic):
#   docker run -d --name syncplay -p 8999:8999 syncplay-server
#
# Run with fork features:
#   docker run -d --name syncplay -p 8999:8999 syncplay-server \
#       --yap-timer --pause-warning-after 300 --admin-password S3cret
#
# Any syncplayServer.py argument can be appended to `docker run`.
# Environment variables honoured by the server:
#   SYNCPLAY_PASSWORD        server password (same as --password)
#   SYNCPLAY_SALT            salt for managed-room passwords (set one so room
#                            operator passwords survive restarts)
#   SYNCPLAY_ADMIN_PASSWORD  server-admin password (same as --admin-password)
#
# Persistent data / mounted files go under /config, e.g.:
#   docker run -d -p 8999:8999 -v syncplay-data:/config syncplay-server \
#       --rooms-db-file /config/rooms.db --motd-file /config/motd.txt \
#       --tls /config/certs
#
# Size strategy: alpine base; multi-stage so only stripped site-packages reach
# the final image; server-only source subset (no client/player/GUI code, no
# icons); no __pycache__/tests shipped. All dependencies install as pure-python
# or musllinux wheels - no compiler needed. (If a future dependency bump ever
# fails to find a musl wheel, add to the build stage:
#   RUN apk add --no-cache gcc musl-dev python3-dev libffi-dev openssl-dev)

FROM python:3.12-alpine AS build

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    # strip what the server never executes: bytecode caches (regenerated
    # in-memory at runtime), bundled test suites, and twisted's trial framework
    && find /install -depth -type d -name '__pycache__' -exec rm -rf {} + \
    && find /install -depth -type d \( -name test -o -name tests \) -exec rm -rf {} + \
    && rm -rf /install/lib/python*/site-packages/twisted/trial


FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /install /usr/local

# Server-only source subset - the server imports nothing from players/, ui/,
# vendor/ or resources/ (verified by booting from exactly this set).
WORKDIR /app
COPY syncplayServer.py .
COPY syncplay/__init__.py syncplay/ep_server.py syncplay/server.py \
     syncplay/protocols.py syncplay/constants.py syncplay/utils.py \
     syncplay/messages.py syncplay/messages_*.py syncplay/

# Run unprivileged; /config is for operator-mounted files (TLS certs, motd,
# rooms/stats databases) and is writable by the server user.
RUN adduser -S -D -H -h /config syncplay \
    && mkdir -p /config \
    && chown syncplay /config
USER syncplay
VOLUME /config

EXPOSE 8999/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8999), timeout=3).close()"

ENTRYPOINT ["python", "syncplayServer.py"]
CMD ["--port", "8999"]

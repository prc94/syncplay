# Syncplay server (https://syncplay.pl)
#
# Build:
#   docker build -t syncplay-server .
#
# Run (basic):
#   docker run -d --name syncplay -p 8999:8999 syncplay-server
#
# Run with the yap timer and a pause warning enabled:
#   docker run -d --name syncplay -p 8999:8999 syncplay-server \
#       --yap-timer --pause-warning-after 300 \
#       --pause-warning-message "Paused for {} - resume when ready!"
#
# Any syncplayServer.py argument can be appended to `docker run`.
# The server also honours these environment variables:
#   SYNCPLAY_PASSWORD  server password (same as --password)
#   SYNCPLAY_SALT      salt for managed-room passwords (same as --salt;
#                      set one so room operator passwords survive restarts)
#
# Persistent data / mounted files go under /config, e.g.:
#   docker run -d -p 8999:8999 -v syncplay-data:/config syncplay-server \
#       --rooms-db-file /config/rooms.db --motd-file /config/motd.txt \
#       --tls /config/certs

FROM python:3.12-slim

# Server-only dependencies (requirements.txt platform markers skip the
# Windows/macOS extras; the GUI requirements are not needed for the server).
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY syncplayServer.py .
COPY syncplay/ syncplay/

# Run unprivileged; /config is for operator-mounted files (TLS certs, motd,
# rooms/stats databases) and is writable by the server user.
RUN useradd --system --home-dir /config --shell /usr/sbin/nologin syncplay \
    && mkdir -p /config \
    && chown syncplay:syncplay /config
USER syncplay
VOLUME /config

EXPOSE 8999/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8999), timeout=3).close()"

ENTRYPOINT ["python", "syncplayServer.py"]
CMD ["--port", "8999"]

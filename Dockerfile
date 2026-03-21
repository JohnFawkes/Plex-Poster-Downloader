# Use an official lightweight Python image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Upgrade pip first
RUN pip install --upgrade pip

# Install dependencies directly to keep the image small.
# wheel and jaraco.context are upgraded to the latest available releases to
# address CVEs flagged by Trivy (privilege escalation and path-traversal).
# They are installed before Flask/PlexAPI so pip resolves their transitive
# dependency slots to the newer, patched versions.
# gosu is used by the entrypoint to drop from root to appuser after fixing
# bind-mount permissions on /app/config.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "wheel>=0.46.2" "jaraco.context>=6.1.0" \
    && pip install --no-cache-dir Flask PlexAPI requests cryptography

# Copy the application and entrypoint
COPY plex_poster_downloader.py .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Bind to all interfaces inside the container so Docker port mapping works.
# Debug mode is off by default; set FLASK_DEBUG=1 only in development.
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_DEBUG=0

# Run as a non-root user to limit the blast radius if the app is compromised.
# The entrypoint runs as root only long enough to fix /app/config permissions,
# then drops to appuser via gosu.
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app

# Expose the Flask port
EXPOSE 5000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "plex_poster_downloader.py"]

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
RUN pip install --no-cache-dir "wheel>=0.46.2" "jaraco.context>=6.1.0" \
    && pip install --no-cache-dir Flask PlexAPI requests cryptography

# Copy the script into the container
COPY plex_poster_downloader.py .

# Bind to all interfaces inside the container so Docker port mapping works.
# Debug mode is off by default; set FLASK_DEBUG=1 only in development.
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_DEBUG=0

# Run as a non-root user to limit the blast radius if the app is compromised
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app
USER appuser

# Expose the Flask port
EXPOSE 5000

# Define the command to run the app
CMD ["python", "plex_poster_downloader.py"]

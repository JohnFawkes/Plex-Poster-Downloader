# Use an official lightweight Python image
FROM python:3-slim

# Set the working directory
#WORKDIR /app

# Install dependencies directly to keep the image small
# We install Flask, PlexAPI, and Werkzeug (requests comes with PlexAPI usually)
#RUN pip install --no-cache-dir Flask PlexAPI requests

COPY requirements.txt requirements.txt
RUN echo "**** install system packages ****" \
 && apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && pip3 install --no-cache-dir --upgrade --requirement /requirements.txt \
 && apt-get clean \
 && apt-get update \
 && apt-get check \
 && apt-get -f install \
 && apt-get autoclean \
 && rm -rf /requirements.txt /tmp/* /var/tmp/* /var/lib/apt/lists/*

# Copy the script into the container
COPY plex_poster_downloader.py .

# Expose the Flask port
EXPOSE 5000

VOLUME /app

# Define the command to run the app
CMD ["python", "plex_poster_downloader.py"]

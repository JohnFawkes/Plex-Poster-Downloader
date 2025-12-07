# Use an official lightweight Python image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Upgrade pip first
RUN pip install --upgrade pip

# Install dependencies directly to keep the image small
RUN pip install --no-cache-dir Flask PlexAPI requests cryptography

# Copy the script into the container
COPY plex_poster_downloader.py .

# Expose the Flask port
EXPOSE 5000

# Define the command to run the app
CMD ["python", "plex_poster_downloader.py"]

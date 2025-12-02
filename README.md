## 🎬 Plex Poster Downloader & Manager

A self-hosted web application that allows you to easily browse your Plex libraries and download local poster artwork (poster.jpg) directly to your media folders.

This tool is perfect for users who want to switch to "Local Assets" agents or simply want a backup of their curated artwork stored alongside their media files.

## ✨ Features

* **Plex-Themed Web UI**: A dark, responsive interface designed to feel like home for Plex users.

* **Smart Folder Detection: Automatically saves images to poster.jpg inside the correct Movie, Show, or Season folder on your disk.

* **Download Tracking:**

    * **Green (✅):** Poster already downloaded.

    * **Yellow (⚠️):** Partial download (e.g., Show has a poster but seasons are missing, or vice versa).

    * **Standard:** Poster missing.

* **Library Management:**

    * Supports Movies and TV Shows.

    * Paginated views for large libraries.

    * **Ability to hide/ignore specific libraries via Settings.

* **Global Search:** Instantly search for movies or shows across all libraries with autocomplete suggestions.

* **Authentication:** Secure the interface with an admin username/password (can be disabled in Settings).

* **Manual Override:** Toggle items as "Complete" manually if you don't want to download a poster for them.

## **🐳 Docker Installation (Recommended)**

The easiest way to run this application is using Docker.

# **Prerequisites**

  * **Docker**

  * **Docker Compose**

**1. Create Project Directory**

Create a folder on your server and place the ``compose.yaml`` file inside it.

**2. Configure Volumes**

Open ``compose.yaml`` and ensure the volumes map to your actual media folders.

```
services:
  plex-poster-downloader:
    image: ghcr.io/johnfawkes/plex-poster-downloader:latest
    container_name: plex-poster-manager
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      # Stores config.json and download history
      - ./config:/app/config
      # If using with Kometa, change the first downloaded_posters to the asset directory for kometa
      - ./downloaded_posters:/app/downloaded_posters
    environment:
      - DATA_DIR=/app/config
```

**3. Run the Container**

```
docker-compose up -d
```

**4. Access the UI**

Open your browser and navigate to: ``http://localhost:5000``

## 🐍 **Manual Installation (Python)**

If you prefer running it directly on your host machine without Docker.

**Prerequisites**

  * Python 3.11 or higher

  * ``pip``

 **Steps**

  1. **Clone the Repository**

```
git clone https://github.com/johnfawkes/plex-poster-downloader.git
cd plex-poster-downloader
```

  2. **Install Dependencies**

```
pip install -r requirements.txt
```

***(Note: Create a virtual environment first if preferred)***

  3. **Run the Script**

```
python plex_poster_downloader.py
```

  4. **Access the UI** Open ``http://localhost:5000`` in your browser.

## **⚙️ Configuration**

On the first launch, you will be redirected to the Settings page.

  1. **Authentication:** You will be asked to create an Admin Username and Password.

  2. **Plex Connection:**

        * **Plex Server URL:** e.g., ``http://192.168.1.10:32400``

        * **Plex Token:** Your X-Plex-Token. [How to find your token.](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

  3. **Download Directory:**

       * **Docker:** Leave as downloaded_posters (it maps to the internal path or).

       * **Manual:** Set this to the root of your media drive (e.g., ``Z:\Media`` or ``/mnt/media``, or where ever you want to save the posters. If you want to use it with Kometa then put the location of the assets path for kometa).

  4. **Hidden Libraries:** You can uncheck libraries you don't want to manage (e.g., Home Videos, Music).

## 📂 **Folder Structure Logic**

The script attempts to replicate your physical folder structure based on the file paths Plex reports.
This is to ensure compatibility with Kometa for assets.

**Example Download Paths:**

  * **Movie:** ``[BaseDir]/Movies/Avatar (2009)/poster.jpg``

  * **Show:** ``[BaseDir]/TV Shows/The Office/poster.jpg``

  * **Season:** ``[BaseDir]/TV Shows/The Office/Season 01/poster.jpg``

Note: The script creates folders if they don't exist.

## 🛠️ **Troubleshooting**

**Login loop or Session errors:**
The app generates a new secret key on every restart for security. If you restart the container, you will need to log in again.

## Changelog

All notable changes to the Plex Poster Downloader project will be documented in this file.

## [v0.7.2] - Security & Encryption

## Added

* **Token Encryption**: The Plex Token is now encrypted at rest using cryptography (Fernet). The system generates a unique .secret.key file on the first run to secure your credentials.

* **UI Security:** The Plex Token input in the Settings menu is now masked ((Encrypted)). Users must click a "Change" button to unlock the field and overwrite the token, preventing accidental exposure or modification.

## Changed

* **Config Handling:** config.json now stores the Plex Token in an encrypted format. It is automatically decrypted in memory when the application runs.

* **Dependencies:** Added cryptography to the requirements/Dockerfile.

## [v0.7.1] - UX Fixes

## Changed

* **Settings UX:** The "Provider Name" dropdown is now automatically disabled and dimmed when "Random (No Uploads)" mode is selected, preventing configuration errors.

## [v0.7.0]

## Added

* **Global Verbose Logging:** Added a setting to enable detailed logging for all actions (HTTP requests, downloads, errors) to the console/docker logs.

* **Cron Day Selection:** Added ability to schedule the automated job for specific days of the week (e.g., "Every Friday") or Daily.

* **Cron Time UI:** Replaced manual text input for time with Hour/Minute/AM/PM dropdowns for better UX.

## Changed

* **Logging Configuration:** Moved logging settings from the Cron section to the main configuration area.

* **Scheduler Logic:** Scheduler now strictly skips items that already have a poster downloaded locally. It will not rotate or replace existing artwork.

## [v0.6.0] - The Automation & Backgrounds Update

## Added

* **Cron Scheduler:** Built-in background thread to automatically download posters at a set time.

* **Modes:** Random (No Uploads), Specific Provider, Random from Provider.

* Configurable Provider targeting (TMDB, TVDB, Fanart, etc.).

* Background (Fanart) Support:

* UI now has Tabs to switch between Posters and Backgrounds.

* Support for downloading background images for Movies, Shows, and Seasons.

* Updated file naming logic to support _background.jpg.

* **Provider Badges:** Added visual badges to poster cards indicating the source (TMDB, TVDB, Fanart, etc.).

* **Cron Background Support:** Added a toggle to allow the scheduler to download backgrounds in addition to posters.

## Fixed

* **Badge Positioning:** Moved provider badges to the top-left to avoid obscuring the "Current" status or download buttons.

* **URL Parsing:** Fixed an issue where the Cron job was malforming URLs for external providers.

## [v0.5.0] - The "Kometa" & Asset Structure Update

## Added

* **Asset Style Settings:** Added support for two directory structures:

    * **Asset Folders (Kometa Default):** Library/ItemName/poster.jpg

    * **No Asset Folders (Flat):** Library/ItemName.jpg

* **Migration Tool:** Added a utility in Settings to scan the local download directory and restructure files between "Asset Folder" and "Flat" styles without re-downloading.

* **Library Filtering:** Added a checklist in Settings to "Ignore" specific libraries (e.g., Home Videos) from appearing in the UI or running in the Scheduler.

## Changed

* **Migration Logic:** Migration now scans local disk structure only (removing dependency on Plex API for file moves).

## [v0.4.0] - Docker & Theme Overhaul

## Added

* **Docker Support:** Added Dockerfile and docker-compose.yml for containerized deployment.

* **Plex Theme:** Completely overhauled CSS variables to match Plex's native "True Black & Yellow" color scheme.

* **Environment Variables:** Added support for DATA_DIR mapping for Docker volumes.

## Changed

* **Home Page Layout:** Switched to a responsive Flexbox layout that centers and scales library cards dynamically.

* **Stats Display:** Added a detailed statistics table to the Home page showing Movie/Show/Episode counts (from Plex) and Downloaded counts/Disk Usage (Local).

## [v0.3.0] - Authentication & Security

## Added

* **Authentication System:** Added a secure Login system.

    * Passwords are hashed using werkzeug.security.

    * Session timeout set to 1 hour.

* **Security Settings:** Added options to Change Password or Disable Authentication entirely via the Settings page.

* **Session Security:** Script now generates a random secret_key on startup to invalidate old sessions.

## Fixed

* **Redirect Loops:** Fixed 404/Redirect loop issues when initializing a fresh install.

## [v0.2.0] - UI & Core Functionality

## Added

* **Global Search:** Added a search bar with live autocomplete suggestions for Movies and Shows.

* **Pagination:** Implemented pagination (50 items per page) to handle large libraries without timing out.

* **"Already Downloaded" Tracking:** - The UI now visually indicates which posters are currently downloaded (Green border).

    * Items are sorted/filtered based on download status (Missing vs Downloaded).

* **Self-Healing:** Added logic to detect if a user manually deleted a file from the disk, updating the UI/History accordingly.

## Changed

* **Settings Page:** Moved configuration (URL, Token, Path) from hardcoded variables to a config.json file manageable via the Web UI.

## [v0.1.0] - Initial Release

# Features

* Connect to Plex Server.

* Browse Libraries (Movies/Shows).

* View Seasons.

* Download posters to a local directory.

* Intelligent folder name detection based on Plex source file paths.

# CLAUDE.md — Plex Poster Downloader

## Project Overview

**Plex Poster Downloader** — A self-hosted Flask web application that lets users browse their Plex Media Server libraries and download poster artwork and background images locally. Designed for migrating to "Local Assets" agents or media management tools like Kometa.

## Running the App

```bash
# Recommended: Docker Compose
docker compose up -d

# Python directly
python plex_poster_downloader.py
# App runs at http://localhost:5000
```

## Key Environment Variables

All configuration is stored in `config.json` (created at first run or via the `/setup` page). Key settings:

| Setting | Default | Purpose |
|---|---|---|
| `PLEX_URL` | `http://127.0.0.1:32400` | Plex server address |
| `PLEX_TOKEN` | — | Plex auth token |
| `DOWNLOAD_BASE_DIR` | `./downloaded_posters` | Base directory for downloaded images |
| `AUTH_DISABLED` | `false` | Disable web UI authentication |
| `ASSET_STYLE` | `ASSET_FOLDERS` | Storage layout: `ASSET_FOLDERS` or `FLAT` |
| `IGNORED_LIBRARIES` | `[]` | Library names to exclude |
| `CRON_ENABLED` | `false` | Enable scheduled downloads |
| `CRON_TIME` / `CRON_DAY` | `03:00` / `DAILY` | Cron schedule |
| `DATA_DIR` | `.` | Directory for config.json and key file |

## Project Structure

```
plex_poster_downloader.py   Main application (Flask, ~2000+ lines, monolithic)
requirements.txt            Python dependencies (Flask, PlexAPI, requests, cryptography)
Dockerfile                  Python 3.11-slim image, non-root appuser
compose.yaml                Docker Compose config (pulls from ghcr.io)
changelog.md                Version history
screenshots/                UI screenshots
```

## Key Routes

| Route | Purpose |
|---|---|
| `/` | Main library browser |
| `/setup` | Initial setup / config page |
| `/login` / `/logout` | Authentication |
| `/settings` | Settings page |
| `/library/<lib_id>` | Browse a specific Plex library |
| `/api/search` | Global search with autocomplete |

## Key Internals

- **Config management:** All settings stored in encrypted `config.json` using Fernet encryption with a per-instance key
- **Cron scheduling:** Background thread checks cron schedule; optional random jitter for timing
- **Download tracking:** `download_history.json` tracks downloaded assets per item
- **Asset styles:** `ASSET_FOLDERS` creates hierarchical directories; `FLAT` uses flat naming conventions
- **Status tracking:** Items show green (all assets), yellow (partial), or missing status
- **Library controls:** Per-library visibility settings
- **Migration utility:** Convert between ASSET_FOLDERS and FLAT naming styles

## Dependencies

```bash
pip install -r requirements.txt
# Flask, PlexAPI, requests, cryptography
```

## No Tests

There is no automated test suite. CI testing validates Docker build succeeds and the web app responds with HTTP 200.

## Docker

```bash
docker build -t plex-poster-downloader .
docker compose up -d
```

- Non-root user `appuser`
- Port: 5000
- Data volume: `./data:/app/data` (config, key file, history)
- Image download volume: mount as needed

## CI/CD

GitHub Actions workflow (`.github/workflows/docker-ci.yml`):

1. **Triggers:** Push to `main` or PR against `main` (only when `plex_poster_downloader.py` or `Dockerfile` change)
2. **Build:** Docker image built with BuildX and GHA cache
3. **Test:** Container started on port 5000, HTTP 200 check on homepage with retry
4. **Version:** Auto semantic version bump on push to main
5. **Release:** GitHub Release created with changelog
6. **Security:** Trivy vulnerability scan (CRITICAL/HIGH) with SARIF upload
7. **Publish:** Image pushed to `ghcr.io/johnfawkes/plex-poster-downloader`

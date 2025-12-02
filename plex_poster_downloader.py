import os
import sys
import re
import json
import requests
import math
import shutil
from datetime import timedelta
from flask import Flask, render_template_string, request, redirect, flash, url_for, session, jsonify
from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized
from werkzeug.security import generate_password_hash, check_password_hash

# ==========================================
# CONFIGURATION MANAGEMENT
# ==========================================
# Support for Docker Volume Mapping
DATA_DIR = os.environ.get('DATA_DIR', '.')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

DEFAULT_CONFIG = {
    'PLEX_URL': 'http://127.0.0.1:32400',
    'PLEX_TOKEN': '',
    'DOWNLOAD_BASE_DIR': os.path.join(DATA_DIR, 'downloaded_posters'),
    'HISTORY_FILE': os.path.join(DATA_DIR, 'download_history.json'),
    'AUTH_DISABLED': False,
    'IGNORED_LIBRARIES': [],
    'ASSET_STYLE': 'ASSET_FOLDERS' # Options: 'ASSET_FOLDERS', 'NO_ASSET_FOLDERS'
}

def get_config():
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            # Ensure defaults for new keys
            for key, val in DEFAULT_CONFIG.items():
                if key not in cfg:
                    cfg[key] = val
            return cfg
    except:
        return DEFAULT_CONFIG

def save_config(new_config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(new_config, f, indent=2)

# ==========================================
# APP SETUP
# ==========================================
app = Flask(__name__)
# Generate a random secret key on every start to invalidate old sessions
app.secret_key = os.urandom(24)
app.permanent_session_lifetime = timedelta(hours=1)

# Global Plex Object
plex = None

def init_plex():
    """Attempts to connect to Plex using current config."""
    global plex
    cfg = get_config()
    url = cfg.get('PLEX_URL')
    token = cfg.get('PLEX_TOKEN')
    
    if not url or not token:
        plex = None
        return False
        
    try:
        plex = PlexServer(url, token)
        print(f"Connected to Plex Server: {plex.friendlyName}")
        return True
    except Exception as e:
        print(f"Error connecting to Plex: {e}")
        plex = None
        return False

# Initialize on startup
init_plex()

# Inject server name into all templates
@app.context_processor
def inject_global_vars():
    server_name = plex.friendlyName if plex else "Disconnected"
    cfg = get_config()
    auth_disabled = cfg.get('AUTH_DISABLED', False)
    return dict(server_name=server_name, auth_disabled=auth_disabled)

# ==========================================
# AUTHENTICATION MIDDLEWARE
# ==========================================
@app.before_request
def require_auth():
    if request.endpoint in ['static', 'login', 'setup', 'logout']:
        return

    cfg = get_config()
    
    if cfg.get('AUTH_DISABLED', False):
        return
    
    if 'AUTH_USER' not in cfg or not cfg['AUTH_USER']:
        return redirect(url_for('setup'))
    
    if 'user' not in session:
        return redirect(url_for('login'))
    
    session.permanent = True

# ==========================================
# HELPER: HISTORY & OVERRIDES
# ==========================================
def load_history_data():
    cfg = get_config()
    hist_file = cfg.get('HISTORY_FILE', 'download_history.json')
    # Handle case where path is relative or absolute
    if not os.path.isabs(hist_file) and DATA_DIR != '.':
         hist_file = os.path.join(DATA_DIR, os.path.basename(hist_file))

    if not os.path.exists(hist_file):
        return {"downloads": {}, "overrides": []}
    try:
        with open(hist_file, 'r') as f:
            data = json.load(f)
            if "downloads" not in data: data["downloads"] = {}
            if "overrides" not in data: data["overrides"] = []
            return data
    except:
        return {"downloads": {}, "overrides": []}

def save_history_data(data):
    cfg = get_config()
    hist_file = cfg.get('HISTORY_FILE', 'download_history.json')
    if not os.path.isabs(hist_file) and DATA_DIR != '.':
         hist_file = os.path.join(DATA_DIR, os.path.basename(hist_file))
         
    with open(hist_file, 'w') as f:
        json.dump(data, f, indent=2)

def save_download_history(rating_key, img_url):
    data = load_history_data()
    data["downloads"][str(rating_key)] = img_url
    save_history_data(data)

def get_history_url(rating_key):
    data = load_history_data()
    return data["downloads"].get(str(rating_key))

def toggle_override_status(rating_key):
    data = load_history_data()
    rk_str = str(rating_key)
    if rk_str in data["overrides"]:
        data["overrides"].remove(rk_str)
        status = False
    else:
        data["overrides"].append(rk_str)
        status = True
    save_history_data(data)
    return status

def is_overridden(rating_key):
    data = load_history_data()
    return str(rating_key) in data["overrides"]

# ==========================================
# HELPER: FILES & PATHS
# ==========================================
def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()

def get_physical_folder_name(item):
    try:
        if item.type == 'movie':
            locations = item.locations
            if locations:
                path = locations[0]
                directory = os.path.dirname(path)
                return os.path.basename(directory)
        elif item.type == 'show':
            locations = item.locations
            if locations:
                path = locations[0].rstrip(os.sep)
                return os.path.basename(path)
        elif item.type == 'season':
            episodes = item.episodes()
            if episodes:
                first_ep_path = episodes[0].locations[0]
                directory = os.path.dirname(first_ep_path)
                return os.path.basename(directory)
            else:
                return f"Season {item.index}"
    except Exception as e:
        print(f"Error resolving path for {item.title}: {e}")
        return "Unknown_Folder"
    return "Unknown_Type"

def get_target_file_path(item, lib_title=None, style=None):
    """
    Returns the FULL target path (directory + filename) for a poster
    based on the configured Asset Style.
    """
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', 'downloaded_posters')
    current_style = style if style else cfg.get('ASSET_STYLE', 'ASSET_FOLDERS')
    
    # Handle relative paths for Docker
    if not os.path.isabs(base_dir) and DATA_DIR != '.':
        base_dir = os.path.join(DATA_DIR, base_dir)
        
    if not lib_title:
        if hasattr(item, 'section'):
             lib_title = item.section().title
        elif hasattr(item, 'librarySectionID'):
             lib = plex.library.sectionByID(item.librarySectionID)
             lib_title = lib.title
        else:
             lib_title = "Unknown_Library"
             
    clean_lib = sanitize_filename(lib_title)
    
    # MOVIE
    if item.type == 'movie':
        folder_name = get_physical_folder_name(item) # e.g. "Avatar (2009)"
        
        if current_style == 'NO_ASSET_FOLDERS':
            # Flat: Library/Movie Name.jpg
            return os.path.join(base_dir, clean_lib, f"{folder_name}.jpg")
        else:
            # Asset Folders: Library/Movie Name/poster.jpg
            return os.path.join(base_dir, clean_lib, folder_name, "poster.jpg")

    # SHOW
    elif item.type == 'show':
        folder_name = get_physical_folder_name(item) # e.g. "The Office"
        
        if current_style == 'NO_ASSET_FOLDERS':
            # Flat: Library/Show Name.jpg
            return os.path.join(base_dir, clean_lib, f"{folder_name}.jpg")
        else:
            # Asset Folders: Library/Show Name/poster.jpg
            return os.path.join(base_dir, clean_lib, folder_name, "poster.jpg")

    # SEASON
    elif item.type == 'season':
        show = item.show()
        show_folder = get_physical_folder_name(show) # e.g. "The Office"
        season_idx = item.index
        # Format season number: Season01, Season00
        season_str = f"Season{season_idx:02d}"
        
        if current_style == 'NO_ASSET_FOLDERS':
            # Flat: Library/Show Name_SeasonXX.jpg
            return os.path.join(base_dir, clean_lib, f"{show_folder}_{season_str}.jpg")
        else:
            # Asset Folders (Kometa style): Library/Show Name/SeasonXX.jpg
            # Note: DOES NOT use a Season subfolder.
            return os.path.join(base_dir, clean_lib, show_folder, f"{season_str}.jpg")
            
    return None

def check_file_exists(item, lib_title=None):
    target_path = get_target_file_path(item, lib_title)
    if target_path:
        return os.path.exists(target_path)
    return False

def get_item_status(item, lib_title):
    """
    Returns item status: 'complete', 'missing', or 'partial'.
    Using accurate but slower check that loops through season objects.
    """
    if is_overridden(item.ratingKey):
        return 'complete'
    
    if item.type == 'movie':
        if check_file_exists(item, lib_title):
            return 'complete'
        return 'missing'
    
    if item.type == 'show':
        has_show_poster = check_file_exists(item, lib_title)
        
        # Original Logic: Iterate over Season objects
        seasons = item.seasons()
        total_seasons = len(seasons)
        downloaded_seasons = 0
        
        for season in seasons:
            if check_file_exists(season, lib_title):
                downloaded_seasons += 1
        
        all_seasons_done = (downloaded_seasons == total_seasons)
        
        if has_show_poster and all_seasons_done:
            return 'complete'
        elif not has_show_poster and downloaded_seasons == 0:
            return 'missing'
        else:
            return 'partial'
            
    return 'missing'

def get_poster_url(poster):
    key = getattr(poster, 'key', None)
    if not key: return ""
    if key.startswith('http') or key.startswith('https'): return key
    return plex.url(key)

# ==========================================
# MIGRATION UTILS
# ==========================================
def perform_migration(target_style):
    """
    Scans the local download directory ONLY and rearranges files based on the target style.
    Does NOT query Plex.
    """
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', 'downloaded_posters')
    # Handle Docker relative path
    if not os.path.isabs(base_dir) and DATA_DIR != '.':
        base_dir = os.path.join(DATA_DIR, base_dir)
        
    if not os.path.exists(base_dir):
        return 0, "Download directory does not exist."

    moved_count = 0
    errors = []

    try:
        # Iterate over Libraries (e.g. "Movies", "TV Shows")
        libraries = [d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
        
        for lib_name in libraries:
            lib_path = os.path.join(base_dir, lib_name)
            
            # --- STRATEGY ---
            # 1. We iterate over all contents of the library folder.
            # 2. We identify if items are FILES (Flat structure) or FOLDERS (Asset/Legacy structure).
            # 3. We determine the identity (Show Name, Season Number) from the name/path.
            # 4. We move it to the target path.
            
            lib_contents = os.listdir(lib_path)
            files = [f for f in lib_contents if os.path.isfile(os.path.join(lib_path, f)) and f.lower().endswith('.jpg')]
            dirs = [d for d in lib_contents if os.path.isdir(os.path.join(lib_path, d))]
            
            # ---------------------------
            # 1. PROCESS EXISTING FLAT FILES
            # ---------------------------
            for f in files:
                src = os.path.join(lib_path, f)
                filename_no_ext = os.path.splitext(f)[0]
                
                if target_style == 'ASSET_FOLDERS':
                    # GOAL: Convert "Show_Season01.jpg" -> "Show/Season01.jpg"
                    #       Convert "Movie.jpg" -> "Movie/poster.jpg"
                    
                    # Regex to detect Season Flat File: "ShowName_Season01"
                    # Matches end of string like _Season01 or _Specials
                    match = re.match(r"(.*)_(Season\d+|Specials)$", filename_no_ext, re.IGNORECASE)
                    
                    if match:
                        # It's a Season File
                        show_name = match.group(1)
                        season_part = match.group(2) # Season01 or Specials
                        
                        dest_dir = os.path.join(lib_path, show_name)
                        dest = os.path.join(dest_dir, f"{season_part}.jpg")
                    else:
                        # It's a Movie/Show Poster
                        dest_dir = os.path.join(lib_path, filename_no_ext)
                        dest = os.path.join(dest_dir, "poster.jpg")
                    
                    try:
                        os.makedirs(dest_dir, exist_ok=True)
                        if not os.path.exists(dest):
                            shutil.move(src, dest)
                            moved_count += 1
                        elif src != dest:
                            # Cleanup duplicate if stuck
                            pass
                    except Exception as e:
                        errors.append(f"Error moving flat file {f}: {e}")

            # ---------------------------
            # 2. PROCESS EXISTING DIRECTORIES
            # ---------------------------
            for d in dirs:
                item_dir = os.path.join(lib_path, d)
                item_contents = os.listdir(item_dir)
                
                # Check for Asset Style files inside this folder
                # (e.g. poster.jpg, Season01.jpg)
                for item_file in item_contents:
                    src = os.path.join(item_dir, item_file)
                    
                    if os.path.isdir(src):
                        continue # Skip subfolders here (handled in Legacy section)
                    if not item_file.lower().endswith('.jpg'):
                        continue

                    # If we are converting TO Flat structure
                    if target_style == 'NO_ASSET_FOLDERS':
                        if item_file.lower() == 'poster.jpg':
                            # "Item/poster.jpg" -> "Item.jpg"
                            dest = os.path.join(lib_path, f"{d}.jpg")
                            try:
                                if not os.path.exists(dest):
                                    shutil.move(src, dest)
                                    moved_count += 1
                            except Exception as e:
                                errors.append(f"Error flattening poster {d}: {e}")
                                
                        elif re.match(r"(Season\d+|Specials)\.jpg", item_file, re.IGNORECASE):
                            # "Item/Season01.jpg" -> "Item_Season01.jpg"
                            fname_no_ext = os.path.splitext(item_file)[0]
                            dest = os.path.join(lib_path, f"{d}_{fname_no_ext}.jpg")
                            try:
                                if not os.path.exists(dest):
                                    shutil.move(src, dest)
                                    moved_count += 1
                            except Exception as e:
                                errors.append(f"Error flattening season {d}: {e}")

                # Check for Legacy Subfolders (e.g. "Season 01")
                # These need to be normalized regardless of target style
                subdirs = [sd for sd in item_contents if os.path.isdir(os.path.join(item_dir, sd))]
                for sd in subdirs:
                    # Detect season number
                    season_str = None
                    if sd.lower() == 'specials':
                        season_str = 'Season00'
                    else:
                        match = re.match(r"Season\s*(\d+)", sd, re.IGNORECASE)
                        if match:
                            num = int(match.group(1))
                            season_str = f"Season{num:02d}"
                    
                    if season_str:
                        # Look for poster.jpg inside legacy folder
                        legacy_poster = os.path.join(item_dir, sd, "poster.jpg")
                        if os.path.exists(legacy_poster):
                            
                            if target_style == 'ASSET_FOLDERS':
                                # "Item/Season 01/poster.jpg" -> "Item/Season01.jpg"
                                dest = os.path.join(item_dir, f"{season_str}.jpg")
                            else:
                                # "Item/Season 01/poster.jpg" -> "Item_Season01.jpg"
                                dest = os.path.join(lib_path, f"{d}_{season_str}.jpg")
                            
                            try:
                                if not os.path.exists(dest):
                                    shutil.move(legacy_poster, dest)
                                    moved_count += 1
                                # Try to delete the legacy folder if empty
                                try: os.rmdir(os.path.join(item_dir, sd))
                                except: pass
                            except Exception as e:
                                errors.append(f"Error migrating legacy folder {d}/{sd}: {e}")

                # Cleanup: If converting to Flat, attempt to remove the Item directory if it's now empty
                if target_style == 'NO_ASSET_FOLDERS':
                    try:
                        # os.rmdir only removes empty dirs
                        os.rmdir(item_dir) 
                    except:
                        pass

    except Exception as e:
        return 0, str(e)

    return moved_count, errors

# ==========================================
# HTML LAYOUT PARTS
# ==========================================

CSS_COMMON = """
    :root { 
        --bg: #121212; 
        --nav: #232323;
        --card: #232323; 
        --text: #e5e5e5; 
        --text-muted: #a0a0a0;
        --accent: #E5A00D; 
        --primary: #E5A00D;
        --btn-text: #000000;
        --warning: #cc7b19;
        --danger: #c0392b;
        --input-bg: #111111;
        --border-color: #3a3a3a;
    }
    body { font-family: 'Poppins', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; font-weight: 300; }
    h1, h2, h3 { color: var(--text); font-weight: 600; }
    a { text-decoration: none; color: inherit; transition: 0.2s; }
    
    /* Ensure native inputs use the accent color */
    input[type="checkbox"], input[type="radio"] { accent-color: var(--accent); }
    
    .nav { 
        margin-bottom: 30px; padding: 15px 25px; 
        background: var(--nav); border-radius: 12px; 
        display: flex; justify-content: space-between; align-items: center; 
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        border-bottom: 1px solid var(--border-color);
    }
    .nav-links { display: flex; align-items: center; }
    .nav-links a { margin-right: 20px; font-weight: 600; color: var(--text); }
    .nav-links a:hover { color: var(--primary); }
    
    /* Search Bar */
    .search-box { position: relative; width: 300px; margin: 0 20px; }
    .search-input { 
        width: 100%; padding: 8px 15px; border-radius: 20px; 
        border: 1px solid var(--border-color); background: var(--input-bg); color: var(--text); 
        outline: none; transition: border-color 0.2s;
    }
    .search-input:focus { border-color: var(--primary); }
    .search-results {
        position: absolute; top: 100%; left: 0; right: 0; 
        background: var(--card); border-radius: 8px; 
        margin-top: 5px; box-shadow: 0 10px 15px rgba(0,0,0,0.5); 
        z-index: 100; max-height: 400px; overflow-y: auto;
        display: none; border: 1px solid var(--border-color);
    }
    .search-result-item {
        display: flex; align-items: center; padding: 10px; 
        border-bottom: 1px solid var(--border-color); cursor: pointer; text-decoration: none; color: var(--text);
    }
    .search-result-item:last-child { border-bottom: none; }
    .search-result-item:hover { background: #333333; }
    .search-thumb { width: 35px; height: 50px; object-fit: cover; border-radius: 4px; margin-right: 12px; background: #111; }
    .search-info { flex: 1; display: flex; flex-direction: column; }
    .search-title { font-weight: 600; font-size: 0.9em; display: block; }
    .search-meta { font-size: 0.75em; color: var(--text-muted); }
    
    .server-badge { 
        background: rgba(255,255,255,0.05); color: var(--accent); 
        padding: 6px 12px; border-radius: 6px; 
        font-size: 0.9em; font-weight: 600; 
        border: 1px solid var(--accent);
    }
    .settings-link { color: var(--text-muted); font-size: 1.2em; margin-left: 15px; }
    .settings-link:hover { color: var(--text); transform: rotate(90deg); }
    
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 25px; }
    
    .card { 
        background: var(--card); border-radius: 12px; overflow: hidden; 
        transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s; position: relative; 
        box-shadow: 0 4px 6px rgba(0,0,0,0.2);
        border: 2px solid transparent;
    }
    .card:hover { 
        transform: translateY(-5px); 
        cursor: pointer; 
        box-shadow: 0 10px 15px rgba(0,0,0,0.4); 
        border-color: var(--accent);
    }
    .card img { width: 100%; height: 300px; object-fit: cover; background: #000; }
    .card .title { padding: 15px; text-align: center; font-size: 1.1em; font-weight: 600; color: var(--text); line-height: 1.4; transition: color 0.2s; }
    .card:hover .title { color: var(--accent); }
    
    /* Homepage Card Special Styling */
    .home-card:hover { border: 2px solid var(--accent); }
    .home-card:hover .title { color: var(--accent) !important; }
    
    .poster-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 25px; }
    .poster-card { 
        background: var(--card); padding: 10px; border-radius: 12px; 
        text-align: center; position: relative; border: 4px solid transparent; 
        transition: 0.2s;
    }
    .poster-card img { width: 100%; border-radius: 8px; }
    .poster-card:hover { background: #333333; border-color: var(--accent); }
    .poster-card.selected { border-color: var(--accent); background: rgba(229, 160, 13, 0.1); }
    .selected-badge { 
        position: absolute; top: -10px; right: -10px; 
        background: var(--accent); color: var(--btn-text); 
        padding: 5px 12px; border-radius: 20px; 
        font-weight: 800; font-size: 0.8em; 
        box-shadow: 0 4px 6px rgba(0,0,0,0.5); 
    }
    
    .btn { 
        display: block; width: 100%; background: var(--primary); color: var(--btn-text); 
        padding: 12px 0; border: none; cursor: pointer; margin-top: 12px; 
        font-weight: 600; border-radius: 8px; font-family: inherit;
    }
    .btn:hover { filter: brightness(1.1); }
    .btn-danger { background: var(--danger); color: white; }
    .btn-danger:hover { background: #c0392b; }
    .btn-toggle { background: #3a3a3a; color: white; width: auto; display: inline-block; padding: 10px 20px; margin-left: 20px; }
    .btn-toggle.active { background: var(--accent); color: var(--btn-text); }
    
    .pagination { text-align: center; margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border-color); }
    .page-btn { 
        background: var(--card); color: var(--text); padding: 10px 20px; 
        text-decoration: none; border-radius: 6px; margin: 0 5px; display: inline-block;
    }
    .page-btn:hover { background: var(--primary); color: var(--btn-text); }
    .page-info { color: var(--text-muted); margin: 0 15px; }
    
    .form-group { margin-bottom: 20px; }
    .form-group label { display: block; margin-bottom: 8px; font-weight: 600; color: var(--text-muted); }
    .form-group input, .form-group select { 
        width: 100%; padding: 12px; border-radius: 8px; border: 1px solid var(--border-color); 
        background: var(--input-bg); color: white; font-family: inherit; box-sizing: border-box;
    }
    .form-group input:focus, .form-group select:focus { outline: none; border-color: var(--primary); }
    .form-group input:disabled { background: #0a0a0a; color: #555; border-color: #222; cursor: not-allowed; }
    
    .flash { background: rgba(229, 160, 13, 0.2); color: var(--accent); padding: 15px; margin-bottom: 25px; border-radius: 8px; border: 1px solid var(--accent); }
    .path-info { font-size: 0.85em; color: var(--text-muted); margin-bottom: 15px; font-family: monospace; background: rgba(0,0,0,0.3); padding: 5px 10px; border-radius: 4px; display: inline-block; }
    .section-header { margin-top: 50px; border-bottom: 2px solid var(--nav); padding-bottom: 10px; margin-bottom: 25px; display: flex; align-items: center; justify-content: space-between; }
    .section-header h2 { margin: 0; font-size: 1.4em; }
    .section-header span { font-size: 0.9em; color: var(--text-muted); background: var(--nav); padding: 4px 10px; border-radius: 20px; }
"""

HTML_TOP = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ server_name }} - Poster Manager</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🎬</text></svg>">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        """ + CSS_COMMON + """
    </style>
    <script>
    let searchTimeout;
    
    function handleSearch(query) {
        clearTimeout(searchTimeout);
        const resultsDiv = document.getElementById('search-results');
        
        if (query.length < 2) {
            resultsDiv.style.display = 'none';
            resultsDiv.innerHTML = '';
            return;
        }
        
        searchTimeout = setTimeout(() => {
            fetch(`/api/search?q=${encodeURIComponent(query)}`)
                .then(response => response.json())
                .then(data => {
                    resultsDiv.innerHTML = '';
                    if (data.length > 0) {
                        resultsDiv.style.display = 'block';
                        data.forEach(item => {
                            const div = document.createElement('a');
                            div.href = `/item/${item.ratingKey}`;
                            div.className = 'search-result-item';
                            
                            const year = item.year ? `(${item.year})` : '';
                            const type = item.type === 'show' ? '📺' : '🎬';
                            
                            div.innerHTML = `
                                <img src="${item.thumb}" class="search-thumb" onerror="this.src='https://via.placeholder.com/40x60?text=?'">
                                <div class="search-info">
                                    <span class="search-title">
                                        <span style="font-size: 1.5em; margin-right: 5px; vertical-align: middle; text-shadow: 0 0 3px var(--accent);">${type}</span>
                                        <span style="vertical-align: middle;">${item.title}</span>
                                    </span>
                                    <span class="search-meta">${year}</span>
                                </div>
                            `;
                            resultsDiv.appendChild(div);
                        });
                    } else {
                        resultsDiv.style.display = 'none';
                    }
                })
                .catch(err => console.error(err));
        }, 300);
    }
    
    function hideSearch() {
        const resultsDiv = document.getElementById('search-results');
        // Small delay to allow click events on links to register
        setTimeout(() => {
            resultsDiv.style.display = 'none';
        }, 200);
    }
    </script>
</head>
<body>

    <div class="nav">
        <div class="nav-links">
            <a href="/">Home</a>
            {% if breadcrumbs %}
                {% for name, link in breadcrumbs %}
                    &gt; <a href="{{ link }}">{{ name }}</a>
                {% endfor %}
            {% endif %}
        </div>
        
        {% if request.endpoint not in ['login', 'settings', 'setup'] %}
        <div class="search-box">
            <input type="text" class="search-input" placeholder="Search movies & shows..." oninput="handleSearch(this.value)" onblur="hideSearch()">
            <div id="search-results" class="search-results"></div>
        </div>
        {% endif %}
        
        <div style="display:flex; align-items:center;">
            <div class="server-badge">
                Plex Server: {{ server_name }}
            </div>
            <a href="/settings" class="settings-link" title="Settings">⚙️</a>
            {% if auth_disabled %}
                <a href="/setup" class="settings-link" title="Login / Enable Auth" style="font-size:0.9em; margin-left: 20px;">Login</a>
            {% else %}
                <a href="/logout" class="settings-link" title="Logout" style="font-size:0.9em; margin-left: 20px;">Logout</a>
            {% endif %}
        </div>
    </div>

    {% with messages = get_flashed_messages() %}
        {% if messages %}
            {% for message in messages %}
                <div class="flash">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    <div style="display:flex; justify-content:space-between; align-items:center;">
        <h1>{{ title }}</h1>
        {% if toggle_override %}
        <form action="{{ url_for('toggle_complete') }}" method="post" style="margin:0;">
            <input type="hidden" name="rating_key" value="{{ rating_key }}">
            <button type="submit" class="btn btn-toggle {% if is_overridden %}active{% endif %}">
                {% if is_overridden %}✓ Manually Completed{% else %}Mark as Complete{% endif %}
            </button>
        </form>
        {% endif %}
    </div>
    <!-- PAGE CONTENT STARTS HERE -->
"""

HTML_BOTTOM = """
    <!-- PAGE CONTENT ENDS HERE -->
</body>
</html>
"""

HTML_LOGIN_SETUP = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - Poster Manager</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🎬</text></svg>">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        """ + CSS_COMMON + """
        body { display: flex; align-items: center; justify-content: center; height: 100vh; padding: 0; }
        .auth-container { width: 100%; max-width: 400px; }
        .card { padding: 40px; transform: none !important; cursor: default !important; }
        .card:hover { transform: none; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    </style>
</head>
<body>
    <div class="auth-container">
        <div class="card">
            <div style="text-align: center; margin-bottom: 30px;">
                <div style="font-size: 3em;">🎬</div>
                <h2>{{ title }}</h2>
                <p style="color: var(--text-muted);">{{ subtitle }}</p>
            </div>
            
            {% with messages = get_flashed_messages() %}
                {% if messages %}
                    {% for message in messages %}
                        <div class="flash" style="text-align:center;">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            
            <form method="post">
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" name="username" required autofocus>
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" name="password" required>
                </div>
                {% if is_setup %}
                <div class="form-group">
                    <label>Confirm Password</label>
                    <input type="password" name="confirm_password" required>
                </div>
                {% endif %}
                <button type="submit" class="btn">{{ btn_text }}</button>
            </form>
        </div>
    </div>
</body>
</html>
"""

# ==========================================
# ROUTES
# ==========================================

@app.route('/api/search')
def api_search():
    if not plex: return jsonify([])
    query = request.args.get('q', '')
    if len(query) < 2: return jsonify([])
    
    try:
        # Search widely (fetch a bit more to allow for filtering)
        results = plex.search(query, limit=20)
        
        data = []
        for item in results:
            # Filter for Movies and Shows only
            if item.type not in ['movie', 'show']:
                continue
                
            thumb = item.thumbUrl if item.thumb else ''
            year = getattr(item, 'year', '')
            
            data.append({
                'title': item.title,
                'year': year,
                'ratingKey': item.ratingKey,
                'thumb': thumb,
                'type': item.type
            })
            
            # Limit the dropdown response to 10 items
            if len(data) >= 10:
                break
                
        return jsonify(data)
    except Exception as e:
        print(f"Search Error: {e}")
        return jsonify([])

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    cfg = get_config()
    if 'AUTH_USER' in cfg and cfg['AUTH_USER']:
        return redirect(url_for('login'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        confirm = request.form['confirm_password']
        
        if password != confirm:
            flash("Passwords do not match.")
        elif len(password) < 4:
            flash("Password must be at least 4 characters.")
        else:
            cfg['AUTH_USER'] = username
            cfg['AUTH_HASH'] = generate_password_hash(password)
            cfg['AUTH_DISABLED'] = False
            save_config(cfg)
            flash("Account created! Please login.")
            return redirect(url_for('login'))

    return render_template_string(HTML_LOGIN_SETUP, 
                                  title="Setup Admin", 
                                  subtitle="Create your admin account to secure access.",
                                  btn_text="Create Account",
                                  is_setup=True)

@app.route('/login', methods=['GET', 'POST'])
def login():
    cfg = get_config()
    if cfg.get('AUTH_DISABLED', False):
        return redirect(url_for('home'))

    if 'user' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        stored_user = cfg.get('AUTH_USER')
        stored_hash = cfg.get('AUTH_HASH')
        
        if username == stored_user and check_password_hash(stored_hash, password):
            session.permanent = True
            session['user'] = username
            return redirect(url_for('home'))
        else:
            flash("Invalid username or password.")

    return render_template_string(HTML_LOGIN_SETUP, 
                                  title="Login", 
                                  subtitle="Please sign in to continue.",
                                  btn_text="Sign In",
                                  is_setup=False)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    cfg = get_config()
    
    # Check if this is "first run" / unconfigured
    is_unconfigured = 'AUTH_USER' not in cfg or not cfg['AUTH_USER']
    auth_disabled = cfg.get('AUTH_DISABLED', False)
    
    # Fetch all libraries for the checkbox list
    all_libs = []
    if plex:
        try:
            all_libs = plex.library.sections()
        except:
            pass
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'update_config':
            # Merge new config with existing to preserve Auth fields
            cfg['PLEX_URL'] = request.form.get('plex_url', '').strip()
            cfg['PLEX_TOKEN'] = request.form.get('plex_token', '').strip()
            cfg['DOWNLOAD_BASE_DIR'] = request.form.get('download_dir', 'downloaded_posters').strip()
            cfg['HISTORY_FILE'] = request.form.get('history_file', 'download_history.json').strip()
            cfg['ASSET_STYLE'] = request.form.get('asset_style', 'ASSET_FOLDERS')
            
            # Update Ignored Libraries from Checkboxes
            # getlist returns a list of values from checked boxes
            ignored = request.form.getlist('ignored_libs')
            cfg['IGNORED_LIBRARIES'] = ignored
            
            save_config(cfg)
            
            # Try to reconnect
            if init_plex():
                flash("Settings saved and connected to Plex successfully!")
                return redirect(url_for('home'))
            else:
                flash("Settings saved, but could not connect to Plex. Check URL and Token.")
        
        elif action == 'migrate_assets':
            # Perform Migration
            target_style = request.form.get('target_style')
            count, error = perform_migration(target_style)
            if error and isinstance(error, list) and len(error) > 0:
                flash(f"Migrated {count} files. Errors: {len(error)} files failed.")
                print(error) # Log errors to console
            elif error:
                flash(f"Migration Failed: {error}")
            else:
                flash(f"Successfully migrated {count} files to {target_style} structure.")
            
            # Update config to match migration
            cfg['ASSET_STYLE'] = target_style
            save_config(cfg)
            
        elif action == 'change_password':
            current_pw = request.form.get('current_password')
            new_pw = request.form.get('new_password')
            confirm_pw = request.form.get('confirm_password')
            
            stored_hash = cfg.get('AUTH_HASH')
            if not stored_hash or not check_password_hash(stored_hash, current_pw):
                flash("Current password incorrect.")
            elif new_pw != confirm_pw:
                flash("New passwords do not match.")
            elif len(new_pw) < 4:
                flash("New password must be at least 4 characters.")
            else:
                cfg['AUTH_HASH'] = generate_password_hash(new_pw)
                save_config(cfg)
                flash("Password updated successfully.")
        
        elif action == 'create_account':
            username = request.form.get('new_username')
            new_pw = request.form.get('new_password')
            confirm_pw = request.form.get('confirm_password')
            
            if new_pw != confirm_pw:
                flash("Passwords do not match.")
            elif len(new_pw) < 4:
                flash("Password must be at least 4 characters.")
            else:
                cfg['AUTH_USER'] = username
                cfg['AUTH_HASH'] = generate_password_hash(new_pw)
                cfg['AUTH_DISABLED'] = False
                save_config(cfg)
                
                # Auto login after creation
                session.permanent = True
                session['user'] = username
                flash("Account created successfully!")
                return redirect(url_for('home'))
                
        elif action == 'disable_auth':
            # If unconfigured, no password needed to disable
            if is_unconfigured:
                cfg['AUTH_DISABLED'] = True
                save_config(cfg)
                flash("Authentication disabled.")
                return redirect(url_for('home'))
            else:
                # If configured, require password
                current_pw = request.form.get('current_password_disable')
                stored_hash = cfg.get('AUTH_HASH')
                if not stored_hash or not check_password_hash(stored_hash, current_pw):
                    flash("Incorrect password. Cannot disable authentication.")
                else:
                    cfg['AUTH_DISABLED'] = True
                    cfg.pop('AUTH_USER', None)
                    cfg.pop('AUTH_HASH', None)
                    save_config(cfg)
                    session.clear()
                    flash("Authentication completely disabled.")
                    return redirect(url_for('home'))
            
        return redirect(url_for('settings'))

    content = """
    <div style="max-width: 800px; margin: 0 auto;">
        
        <!-- PLEX CONFIGURATION -->
        <div class="card" style="padding: 30px; cursor: default; transform: none; box-shadow: none; margin-bottom: 30px;">
            <h2 style="margin-top:0;">Configuration</h2>
            <form method="post">
                <input type="hidden" name="action" value="update_config">
                <div class="form-group">
                    <label>Plex Server URL</label>
                    <input type="text" name="plex_url" value="{{ cfg.PLEX_URL }}" placeholder="http://localhost:32400">
                </div>
                <div class="form-group">
                    <label>Plex Token (X-Plex-Token)</label>
                    <input type="text" name="plex_token" value="{{ cfg.PLEX_TOKEN }}" placeholder="Your Plex Token">
                </div>
                <div class="form-group">
                    <label>Download Directory</label>
                    <input type="text" name="download_dir" value="{{ cfg.DOWNLOAD_BASE_DIR }}">
                </div>
                <div class="form-group">
                    <label>Asset Folder Style</label>
                    <select name="asset_style">
                        <option value="ASSET_FOLDERS" {% if cfg.ASSET_STYLE == 'ASSET_FOLDERS' %}selected{% endif %}>Asset Folders (Kometa Default)</option>
                        <option value="NO_ASSET_FOLDERS" {% if cfg.ASSET_STYLE == 'NO_ASSET_FOLDERS' %}selected{% endif %}>No Asset Folders (Flat)</option>
                    </select>
                    <small style="color:var(--text-muted); display:block; margin-top:5px;">
                        <strong>Asset Folders:</strong> Movies/Show Name/poster.jpg<br>
                        <strong>No Asset Folders:</strong> Movies/Movie Name.jpg
                    </small>
                </div>
                <div class="form-group">
                    <label>History File Name</label>
                    <input type="text" name="history_file" value="{{ cfg.HISTORY_FILE }}">
                </div>
                
                <div class="form-group">
                    <label>Manage Hidden Libraries</label>
                    {% if all_libs %}
                        <div style="max-height: 200px; overflow-y: auto; background: #141719; border: 1px solid #4b5563; border-radius: 8px; padding: 10px;">
                            {% for lib in all_libs %}
                            <div style="display:flex; align-items:center; margin-bottom:8px;">
                                <input type="checkbox" name="ignored_libs" value="{{ lib.title }}" id="lib_{{ loop.index }}" 
                                    {% if lib.title in cfg.IGNORED_LIBRARIES %}checked{% endif %} 
                                    style="width:auto; margin-right:10px;">
                                <label for="lib_{{ loop.index }}" style="margin:0; font-weight:400; color:var(--text); cursor:pointer;">{{ lib.title }}</label>
                            </div>
                            {% endfor %}
                        </div>
                        <small style="color:var(--text-muted);">Checked libraries will be HIDDEN from the home page.</small>
                    {% else %}
                        <p style="color:var(--warning); font-size:0.9em; background:rgba(245, 158, 11, 0.1); padding:10px; border-radius:6px; border:1px solid var(--warning);">
                            ⚠️ Connect to Plex to load library list.
                        </p>
                    {% endif %}
                </div>
                
                <button type="submit" class="btn">Save & Connect</button>
            </form>
        </div>
        
        <!-- MIGRATION TOOLS -->
        <div class="card" style="padding: 30px; cursor: default; transform: none; box-shadow: none; margin-bottom: 30px;">
            <h2 style="margin-top:0;">File Structure Migration</h2>
            <p style="color:var(--text-muted);">Convert existing downloaded posters to a new folder structure. This scans all items in your Plex libraries and moves local files if found.</p>
            
            <form method="post" onsubmit="return confirm('This will move/rename files in your download directory. This might take a while. Continue?');">
                <input type="hidden" name="action" value="migrate_assets">
                <div class="form-group">
                    <label>Convert To:</label>
                    <select name="target_style">
                        <option value="ASSET_FOLDERS">Asset Folders (Folders per item)</option>
                        <option value="NO_ASSET_FOLDERS">No Asset Folders (Flat in Library)</option>
                    </select>
                </div>
                <button type="submit" class="btn">Migrate Files</button>
            </form>
        </div>
        
        <!-- ACCOUNT SECURITY -->
        <div class="card" style="padding: 30px; cursor: default; transform: none; box-shadow: none;">
            <h2 style="margin-top:0; color: #fff;">Account Security</h2>
            
            {% if is_unconfigured or auth_disabled %}
                <!-- UNCONFIGURED / DISABLED STATE -->
                {% if auth_disabled %}
                    <div style="background: rgba(229, 160, 13, 0.1); padding: 15px; border-radius: 8px; border: 1px solid var(--accent); margin-bottom: 20px;">
                        <strong style="color: var(--accent);">Authentication is currently disabled.</strong>
                        <p style="margin: 5px 0 0 0; font-size: 0.9em; color: var(--text-muted);">Use the form below to re-enable authentication by creating an account.</p>
                    </div>
                {% else %}
                    <p style="color:var(--text-muted); margin-bottom:20px;">Authentication is not set up. Please create an admin account or disable authentication.</p>
                {% endif %}

                <form method="post" style="margin-bottom: 30px;">
                    <input type="hidden" name="action" value="create_account">
                    <h3 style="font-size: 1.1em; color: var(--text);">Setup Admin Account</h3>
                    <div class="form-group">
                        <label>Username</label>
                        <input type="text" name="new_username" required>
                    </div>
                    <div class="form-group">
                        <label>Password</label>
                        <input type="password" name="new_password" required>
                    </div>
                    <div class="form-group">
                        <label>Confirm Password</label>
                        <input type="password" name="confirm_password" required>
                    </div>
                    <button type="submit" class="btn">Create Account & Enable Auth</button>
                </form>
                
                {% if not auth_disabled %}
                <div style="border-top: 1px solid #4b5563; padding-top: 20px;">
                    <form method="post" onsubmit="return confirm('Are you sure? Anyone will be able to access this site.');">
                        <input type="hidden" name="action" value="disable_auth">
                        <button type="submit" class="btn btn-danger">Disable Authentication</button>
                    </form>
                </div>
                {% endif %}
                
            {% else %}
                <!-- CONFIGURED STATE -->
                <div style="margin-bottom: 30px;">
                    <form method="post">
                        <input type="hidden" name="action" value="change_password">
                        <div class="form-group">
                            <label>Username</label>
                            <input type="text" value="{{ cfg.AUTH_USER }}" disabled>
                        </div>
                        <div class="form-group">
                            <label>Current Password</label>
                            <input type="password" name="current_password" required>
                        </div>
                        <div class="form-group">
                            <label>New Password</label>
                            <input type="password" name="new_password" required>
                        </div>
                        <div class="form-group">
                            <label>Confirm New Password</label>
                            <input type="password" name="confirm_password" required>
                        </div>
                        <button type="submit" class="btn">Update Password</button>
                    </form>
                </div>
                
                <div style="border-top: 1px solid #4b5563; padding-top: 20px;">
                    <h3 style="color: var(--danger); font-size: 1.1em; margin-top: 0;">Danger Zone</h3>
                    <p style="font-size: 0.9em; color: var(--text-muted); margin-bottom: 15px;">Disabling authentication allows anyone to access this interface without logging in.</p>
                    <form method="post" onsubmit="return confirm('Are you sure you want to disable authentication? Anyone will be able to access this site.');">
                        <input type="hidden" name="action" value="disable_auth">
                        <div class="form-group">
                            <label>Enter Password to Disable Auth</label>
                            <input type="password" name="current_password_disable" required>
                        </div>
                        <button type="submit" class="btn btn-danger">Disable Authentication</button>
                    </form>
                </div>
            {% endif %}
        </div>
        
    </div>
    """
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, title="Settings", cfg=cfg, all_libs=all_libs, breadcrumbs=[('Settings', '#')], toggle_override=False, is_unconfigured=is_unconfigured, auth_disabled=auth_disabled)

@app.route('/')
def home():
    if not plex:
        flash("Please configure your Plex Server connection.")
        return redirect(url_for('settings'))
        
    try:
        libs = plex.library.sections()
    except:
        flash("Connection lost. Please check settings.")
        return redirect(url_for('settings'))

    cfg = get_config()
    ignored = cfg.get('IGNORED_LIBRARIES', [])
    
    # Filter ignored libs
    visible_libs = [lib for lib in libs if lib.title not in ignored]

    content = """
    <div class="grid">
        {% for lib in visible_libs %}
            {% set icon = '🎬' if lib.type == 'movie' else '📺' if lib.type == 'show' else '📁' %}
            <a href="/library/{{ lib.key }}" class="card home-card" style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 40px 20px; min-height: 200px; text-decoration: none;">
                <div style="font-size: 4em; margin-bottom: 15px; text-shadow: 0 4px 8px rgba(0,0,0,0.3);">{{ icon }}</div>
                <div class="title" style="font-size: 1.4em; font-weight: 700; text-align: center; color: var(--text); margin-bottom: 5px;">{{ lib.title }}</div>
                <div style="font-size: 0.85em; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; font-weight: 500;">{{ lib.type.title() }}</div>
            </a>
        {% endfor %}
    </div>
    """
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, visible_libs=visible_libs, title="Select a Library", breadcrumbs=[], toggle_override=False)

@app.route('/library/<lib_id>')
def view_library(lib_id):
    if not plex: return redirect(url_for('settings'))
    try:
        lib = plex.library.sectionByID(int(lib_id))
    except (ValueError, NotFound, Unauthorized):
        flash("Library not found or unauthorized.")
        return redirect('/')

    # PAGINATION LOGIC
    page = request.args.get('page', 1, type=int)
    per_page = 50
    offset = (page - 1) * per_page
    
    # Use search to fetch only the needed slice.
    total_items = lib.totalSize
    items = lib.search(maxresults=per_page, container_start=offset)
    
    total_pages = math.ceil(total_items / per_page)
    
    # 1. Load History (Manual Completions + Downloads)
    history = load_history_data()
    all_history_keys = list(history['downloads'].keys()) + list(history['overrides'])
    history_keys_set = set(all_history_keys)
    
    # 2. Fetch "Global Done" items from history
    valid_keys = [int(k) for k in history_keys_set if k.isdigit()]
    done_objects_from_history = []
    if valid_keys:
        try:
            done_objects_from_history = lib.search(id=valid_keys)
        except:
            pass

    done_ids_map = {item.ratingKey: item for item in done_objects_from_history}

    todo_items = []
    partial_items = []
    
    # 3. Process the CURRENT PAGE items
    for i in items:
        status = get_item_status(i, lib.title)
        
        if status == 'complete':
            if i.ratingKey not in done_ids_map:
                done_ids_map[i.ratingKey] = i
        elif status == 'partial':
            thumb = i.thumbUrl if i.thumb else ''
            partial_items.append({'title': i.title, 'ratingKey': i.ratingKey, 'thumbUrl': thumb})
        else:
            thumb = i.thumbUrl if i.thumb else ''
            todo_items.append({'title': i.title, 'ratingKey': i.ratingKey, 'thumbUrl': thumb})

    # 4. Construct the Final "Already Downloaded" list
    done_items_list = []
    for key, item in done_ids_map.items():
        thumb = item.thumbUrl if item.thumb else ''
        done_items_list.append({'title': item.title, 'ratingKey': item.ratingKey, 'thumbUrl': thumb})
        
    done_items_list.sort(key=lambda x: x['title'])

    pagination_block = """
    <div class="pagination" style="margin: 30px 0; border-top: 1px solid #444; padding-top: 20px;">
        <div style="display: flex; align-items: center; justify-content: center; gap: 15px;">
            {% if page > 1 %}
                <a href="?page={{ page - 1 }}" class="page-btn">&laquo; Prev</a>
            {% else %}
                <span class="page-btn" style="opacity:0.5; cursor:not-allowed;">&laquo; Prev</span>
            {% endif %}
            
            <form action="" method="get" style="display:flex; align-items:center; gap:10px; margin:0;">
                <label style="margin:0; color:var(--text-muted);">Page</label>
                <select name="page" onchange="this.form.submit()" style="padding: 8px; border-radius: 6px; background: var(--bg); color: var(--text); border: 1px solid #4b5563; cursor: pointer; min-width: 80px;">
                    {% for p in range(1, total_pages + 1) %}
                        <option value="{{ p }}" {% if p == page %}selected{% endif %}>{{ p }}</option>
                    {% endfor %}
                </select>
                <span style="color:var(--text-muted);">of {{ total_pages }}</span>
            </form>

            {% if page < total_pages %}
                <a href="?page={{ page + 1 }}" class="page-btn">Next &raquo;</a>
            {% else %}
                <span class="page-btn" style="opacity:0.5; cursor:not-allowed;">Next &raquo;</span>
            {% endif %}
        </div>
    </div>
    """

    content = pagination_block + """
    
    {% if todo_items %}
    <div class="section-header">
        <h2>Missing Posters</h2>
        <span>{{ todo_items|length }} on this page</span>
    </div>
    <div class="grid">
        {% for item in todo_items %}
            <a href="/item/{{ item.ratingKey }}" class="card">
                <img src="{{ item.thumbUrl }}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'">
                <div class="title">{{ item.title }}</div>
            </a>
        {% endfor %}
    </div>
    {% endif %}

    {% if partial_items %}
    <div class="section-header">
        <h2 style="color: var(--warning);">Half Missing</h2>
        <span>{{ partial_items|length }} on this page</span>
    </div>
    <div class="grid">
        {% for item in partial_items %}
            <a href="/item/{{ item.ratingKey }}" class="card" style="border: 2px solid var(--warning);">
                <img src="{{ item.thumbUrl }}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'">
                <div class="title">⚠️ {{ item.title }}</div>
            </a>
        {% endfor %}
    </div>
    {% endif %}

    {% if done_items %}
    <div class="section-header">
        <h2 style="color: var(--accent);">Already Downloaded (All Pages)</h2>
        <span>{{ done_items|length }} total</span>
    </div>
    <div class="grid">
        {% for item in done_items %}
            <a href="/item/{{ item.ratingKey }}" class="card" style="opacity: 0.7;">
                <img src="{{ item.thumbUrl }}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'">
                <div class="title">✅ {{ item.title }}</div>
            </a>
        {% endfor %}
    </div>
    {% endif %}
    
    {% if not todo_items and not partial_items and not done_items %}
        <p>No items found in this library section.</p>
    {% endif %}
    
    """ + pagination_block

    return render_template_string(HTML_TOP + content + HTML_BOTTOM, 
        todo_items=todo_items, partial_items=partial_items, done_items=done_items_list, 
        title=lib.title, page=page, total_pages=total_pages,
        breadcrumbs=[(lib.title, '#')], toggle_override=False)

@app.route('/item/<rating_key>')
def view_item(rating_key):
    if not plex: return redirect(url_for('settings'))
    try:
        item = plex.fetchItem(int(rating_key))
    except NotFound:
        return "Item not found", 404

    is_show = item.type == 'show'
    posters = item.posters()
    lib = item.section()
    
    selected_url = get_history_url(rating_key)
    override_status = is_overridden(rating_key) if is_show else False
    
    seasons = []
    if is_show:
        seasons = item.seasons()
    
    # Calculate target display path based on current settings
    target_path = get_target_file_path(item, lib.title)
    # Simplify for display (remove base dir)
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', 'downloaded_posters')
    if not os.path.isabs(base_dir) and DATA_DIR != '.':
        base_dir = os.path.join(DATA_DIR, base_dir)
        
    rel_path = os.path.relpath(target_path, base_dir) if target_path else "Unknown"

    content = """
        <div class="path-info">Target: <strong>.../{{ rel_path }}</strong></div>
        
        <div class="poster-grid">
            {% for poster in posters %}
                {% set p_url = poster_url(poster) %}
                {% set is_selected = (p_url == selected_url) %}
                
                <form action="/download" method="post" class="poster-card {% if is_selected %}selected{% endif %}">
                    {% if is_selected %}<div class="selected-badge">CURRENT</div>{% endif %}
                    <img src="{{ p_url }}" loading="lazy">
                    <input type="hidden" name="img_url" value="{{ p_url }}">
                    <input type="hidden" name="rating_key" value="{{ item.ratingKey }}">
                    <button type="submit" class="btn">{% if is_selected %}Re-Download{% else %}Download{% endif %}</button>
                </form>
            {% endfor %}
        </div>

        {% if is_show %}
            <div class="section-header">
                <h2>Seasons</h2>
                <span>{{ seasons|length }} seasons</span>
            </div>
            <div class="grid">
                {% for season in seasons %}
                    <a href="/season/{{ season.ratingKey }}" class="card">
                        <img src="{{ season_thumb(season) }}" loading="lazy">
                        <div class="title">{{ season.title }}</div>
                    </a>
                {% endfor %}
            </div>
        {% endif %}
    """
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, 
        item=item, is_show=is_show, posters=posters, seasons=seasons, 
        rel_path=rel_path, lib_title=lib.title,
        poster_url=get_poster_url, 
        season_thumb=lambda s: s.thumbUrl,
        selected_url=selected_url,
        title=item.title,
        toggle_override=is_show,
        is_overridden=override_status,
        rating_key=item.ratingKey,
        breadcrumbs=[(lib.title, f'/library/{lib.key}'), (item.title, '#')])

@app.route('/season/<rating_key>')
def view_season(rating_key):
    if not plex: return redirect(url_for('settings'))
    season = plex.fetchItem(int(rating_key))
    show = season.show()
    posters = season.posters()
    lib = show.section()
    
    selected_url = get_history_url(rating_key)

    target_path = get_target_file_path(season, lib.title)
    
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', 'downloaded_posters')
    if not os.path.isabs(base_dir) and DATA_DIR != '.':
        base_dir = os.path.join(DATA_DIR, base_dir)
    
    rel_path = os.path.relpath(target_path, base_dir) if target_path else "Unknown"

    content = """
        <div class="path-info">Target: <strong>.../{{ rel_path }}</strong></div>
        
        <div class="poster-grid">
            {% for poster in posters %}
                {% set p_url = poster_url(poster) %}
                {% set is_selected = (p_url == selected_url) %}
                
                <form action="/download" method="post" class="poster-card {% if is_selected %}selected{% endif %}">
                    {% if is_selected %}<div class="selected-badge">CURRENT</div>{% endif %}
                    <img src="{{ p_url }}" loading="lazy">
                    <input type="hidden" name="img_url" value="{{ p_url }}">
                    <input type="hidden" name="rating_key" value="{{ season.ratingKey }}">
                    <button type="submit" class="btn">{% if is_selected %}Re-Download{% else %}Download{% endif %}</button>
                </form>
            {% endfor %}
        </div>
    """
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, 
        show=show, season=season, posters=posters, 
        rel_path=rel_path, lib_title=lib.title,
        poster_url=get_poster_url,
        selected_url=selected_url,
        title=f"{show.title} - {season.title}",
        toggle_override=False,
        breadcrumbs=[(lib.title, f'/library/{lib.key}'), (show.title, f'/item/{show.ratingKey}'), (season.title, '#')])

@app.route('/download', methods=['POST'])
def download():
    if not plex: return redirect(url_for('settings'))
    img_url = request.form.get('img_url')
    rating_key = request.form.get('rating_key')
    
    if not rating_key:
        flash("Error: Missing Media ID")
        return redirect(request.referrer)

    try:
        item = plex.fetchItem(int(rating_key))
        lib_title = item.section().title
        
        # New: Get the exact target file path (directory + filename)
        save_path = get_target_file_path(item, lib_title)
        
        if not save_path:
            flash("Error: Could not determine save path.")
            return redirect(request.referrer)

        # Create directories
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        r = requests.get(img_url, stream=True)
        if r.status_code == 200:
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(1024):
                    f.write(chunk)
            
            save_download_history(rating_key, img_url)
            flash(f"Saved poster to: {save_path}")
        else:
            flash("Failed to download image from Plex.")
            
    except Exception as e:
        flash(f"Error processing download: {e}")

    return redirect(request.referrer)

@app.route('/toggle_complete', methods=['POST'])
def toggle_complete():
    rating_key = request.form.get('rating_key')
    if rating_key:
        new_status = toggle_override_status(rating_key)
        if new_status:
            flash("Show marked as Complete (Manual Override)")
        else:
            flash("Override removed")
            
    return redirect(request.referrer)

if __name__ == '__main__':
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', 'downloaded_posters')
    
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)
    
    print(f"Starting WebUI on http://0.0.0.0:5000")
    print(f"Go to Settings to configure Plex connection.")
    app.run(host='0.0.0.0', port=5000, debug=True)

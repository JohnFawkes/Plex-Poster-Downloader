import os
import sys
import re
import json
import requests
import math
import shutil
import threading
import time
import random
import datetime
from datetime import timedelta
from flask import Flask, render_template_string, request, redirect, flash, url_for, session, jsonify
from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet

# ==========================================
# 1. CONFIGURATION MANAGEMENT
# ==========================================
DATA_DIR = os.environ.get('DATA_DIR', '.')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
KEY_FILE = os.path.join(DATA_DIR, '.secret.key')

DEFAULT_CONFIG = {
    'PLEX_URL': 'http://127.0.0.1:32400',
    'PLEX_TOKEN': '',
    'DOWNLOAD_BASE_DIR': os.path.join(DATA_DIR, 'downloaded_posters'),
    'HISTORY_FILE': os.path.join(DATA_DIR, 'download_history.json'),
    'AUTH_DISABLED': False,
    'IGNORED_LIBRARIES': [],
    'ASSET_STYLE': 'ASSET_FOLDERS',
    'CRON_ENABLED': False,
    'CRON_TIME': '03:00',
    'CRON_MODE': 'RANDOM',
    'CRON_PROVIDER': 'tmdb',
    'CRON_DOWNLOAD_BACKGROUNDS': False,
    'VERBOSE_LOGGING': False,
    'CRON_LIBRARIES': []
}

# --- Encryption Helpers ---
def get_encryption_key():
    """Gets or creates a symmetric encryption key."""
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as f:
            f.write(key)
        return key

def encrypt_val(value):
    """Encrypts a string value."""
    if not value: return ""
    f = Fernet(get_encryption_key())
    return f.encrypt(value.encode()).decode()

def decrypt_val(token):
    """Decrypts a string value."""
    if not token: return ""
    try:
        f = Fernet(get_encryption_key())
        return f.decrypt(token.encode()).decode()
    except:
        # Fallback: return as is (useful for migration from plain text)
        return token

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
            
            # Decrypt Token on Load
            if cfg['PLEX_TOKEN']:
                cfg['PLEX_TOKEN'] = decrypt_val(cfg['PLEX_TOKEN'])
                
            return cfg
    except:
        return DEFAULT_CONFIG

def save_config(new_config):
    # Deep copy to avoid modifying the runtime dict which might be used elsewhere
    cfg_to_save = new_config.copy()
    
    # Encrypt Token before Save
    if cfg_to_save['PLEX_TOKEN']:
        # If it looks like it's already encrypted (Fernet tokens are long), check
        # But for safety, we assume input is plain text from UI or memory
        cfg_to_save['PLEX_TOKEN'] = encrypt_val(cfg_to_save['PLEX_TOKEN'])

    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg_to_save, f, indent=2)

def log_verbose(msg):
    """Global logging helper."""
    cfg = get_config()
    if cfg.get('VERBOSE_LOGGING', False):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {msg}")

# ==========================================
# 2. APP SETUP
# ==========================================
app = Flask(__name__)
app.secret_key = os.urandom(24)
app.permanent_session_lifetime = timedelta(hours=1)

# Global Plex Object
plex = None

def init_plex():
    global plex
    cfg = get_config()
    url = cfg.get('PLEX_URL')
    token = cfg.get('PLEX_TOKEN')
    if not url or not token:
        plex = None
        log_verbose("Plex not configured.")
        return False
    try:
        plex = PlexServer(url, token)
        print(f"Connected to Plex Server: {plex.friendlyName}")
        log_verbose(f"Successfully connected to {plex.friendlyName} at {url}")
        return True
    except Exception as e:
        print(f"Error connecting to Plex: {e}")
        log_verbose(f"Connection Error: {e}")
        plex = None
        return False

init_plex()

# ==========================================
# 3. HELPER FUNCTIONS (CORE)
# ==========================================

def format_provider(provider_str):
    if not provider_str: return "Upload"
    p = str(provider_str).lower()
    if 'themoviedb' in p or 'tmdb' in p: return "TMDB"
    if 'thetvdb' in p or 'tvdb' in p: return "TVDB"
    if 'imdb' in p: return "IMDB"
    if 'fanart' in p: return "Fanart.tv"
    if 'gracenote' in p: return "Plex/Gracenote"
    if 'local' in p: return "Local"
    if 'movieposterdb' in p: return "MoviePosterDB"
    if '.' in p: return p.split('.')[-1].title()
    return p.title()

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
        log_verbose(f"Error resolving path for {item.title}: {e}")
        return "Unknown_Folder"
    return "Unknown_Type"

def get_target_file_path(item, lib_title=None, style=None, img_type='poster'):
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', 'downloaded_posters')
    current_style = style if style else cfg.get('ASSET_STYLE', 'ASSET_FOLDERS')
    
    if not os.path.isabs(base_dir) and DATA_DIR != '.':
        base_dir = os.path.join(DATA_DIR, base_dir)
        
    if not lib_title:
        if hasattr(item, 'section'): lib_title = item.section().title
        elif hasattr(item, 'librarySectionID'):
             lib = plex.library.sectionByID(item.librarySectionID)
             lib_title = lib.title
        else: lib_title = "Unknown_Library"
             
    clean_lib = sanitize_filename(lib_title)
    filename = "poster.jpg" if img_type == 'poster' else "background.jpg"
    
    if item.type == 'movie':
        folder_name = get_physical_folder_name(item) 
        if current_style == 'NO_ASSET_FOLDERS':
            suffix = "" if img_type == 'poster' else "_background"
            return os.path.join(base_dir, clean_lib, f"{folder_name}{suffix}.jpg")
        else:
            return os.path.join(base_dir, clean_lib, folder_name, filename)

    elif item.type == 'show':
        folder_name = get_physical_folder_name(item)
        if current_style == 'NO_ASSET_FOLDERS':
            suffix = "" if img_type == 'poster' else "_background"
            return os.path.join(base_dir, clean_lib, f"{folder_name}{suffix}.jpg")
        else:
            return os.path.join(base_dir, clean_lib, folder_name, filename)

    elif item.type == 'season':
        show = item.show()
        show_folder = get_physical_folder_name(show)
        season_idx = item.index
        season_str = f"Season{season_idx:02d}"
        if current_style == 'NO_ASSET_FOLDERS':
            suffix = "" if img_type == 'poster' else "_background"
            return os.path.join(base_dir, clean_lib, f"{show_folder}_{season_str}{suffix}.jpg")
        else:
            name = f"{season_str}.jpg" if img_type == 'poster' else f"{season_str}_background.jpg"
            return os.path.join(base_dir, clean_lib, show_folder, name)
    return None

def check_file_exists(item, lib_title=None, img_type='poster'):
    target_path = get_target_file_path(item, lib_title, img_type=img_type)
    if target_path:
        return os.path.exists(target_path)
    return False

def get_poster_url(poster):
    key = getattr(poster, 'key', None)
    if not key: return ""
    if key.startswith('http') or key.startswith('https'): return key
    return plex.url(key)

# ==========================================
# 4. HISTORY MANAGEMENT
# ==========================================
def load_history_data():
    cfg = get_config()
    hist_file = cfg.get('HISTORY_FILE', 'download_history.json')
    if not os.path.isabs(hist_file) and DATA_DIR != '.':
         hist_file = os.path.join(DATA_DIR, os.path.basename(hist_file))
    if not os.path.exists(hist_file): return {"downloads": {}, "overrides": []}
    try:
        with open(hist_file, 'r') as f:
            data = json.load(f)
            if "downloads" not in data: data["downloads"] = {}
            if "overrides" not in data: data["overrides"] = []
            return data
    except: return {"downloads": {}, "overrides": []}

def save_history_data(data):
    cfg = get_config()
    hist_file = cfg.get('HISTORY_FILE', 'download_history.json')
    if not os.path.isabs(hist_file) and DATA_DIR != '.':
         hist_file = os.path.join(DATA_DIR, os.path.basename(hist_file))
    with open(hist_file, 'w') as f: json.dump(data, f, indent=2)

def save_download_history(rating_key, img_url, img_type='poster'):
    data = load_history_data()
    key = str(rating_key) if img_type == 'poster' else f"{rating_key}_bg"
    data["downloads"][key] = img_url
    save_history_data(data)

def get_history_url(rating_key, img_type='poster'):
    data = load_history_data()
    key = str(rating_key) if img_type == 'poster' else f"{rating_key}_bg"
    return data["downloads"].get(key)

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

def get_item_status(item, lib_title):
    if is_overridden(item.ratingKey): return 'complete'
    if item.type == 'movie':
        return 'complete' if check_file_exists(item, lib_title) else 'missing'
    if item.type == 'show':
        has_show_poster = check_file_exists(item, lib_title)
        seasons = item.seasons()
        total = len(seasons)
        downloaded = 0
        for season in seasons:
            if check_file_exists(season, lib_title): downloaded += 1
        if has_show_poster and downloaded == total: return 'complete'
        elif not has_show_poster and downloaded == 0: return 'missing'
        else: return 'partial'
    return 'missing'

# ==========================================
# 5. STATS
# ==========================================
def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])

def get_library_stats(lib):
    stats = {
        'type': lib.type,
        'content_str': "0 Items",
        'downloaded_count': 0,
        'bg_downloaded_count': 0,
        'disk_size': 0,
        'size_str': "0 B"
    }
    try:
        if lib.type == 'movie':
            count = lib.totalSize
            stats['content_str'] = f"{count} Movies"
        elif lib.type == 'show':
            show_count = lib.totalSize
            ep_count = 0
            try:
                key = f'/library/sections/{lib.key}/all?type=4&X-Plex-Container-Start=0&X-Plex-Container-Size=0'
                container = plex.query(key)
                ep_count = int(container.attrib.get('totalSize', 0))
            except: pass
            stats['content_str'] = f"{show_count} Shows, {ep_count} Episodes"
    except: pass
    
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', 'downloaded_posters')
    if not os.path.isabs(base_dir) and DATA_DIR != '.':
        base_dir = os.path.join(DATA_DIR, base_dir)
    
    clean_lib = sanitize_filename(lib.title)
    lib_dir = os.path.join(base_dir, clean_lib)
    
    file_count = 0
    bg_count = 0
    total_size = 0
    
    if os.path.exists(lib_dir):
        for root, dirs, files in os.walk(lib_dir):
            for f in files:
                if f.lower().endswith('.jpg') or f.lower().endswith('.png'):
                    fp = os.path.join(root, f)
                    total_size += os.path.getsize(fp)
                    
                    # Detect backgrounds
                    if 'background' in f.lower():
                        bg_count += 1
                    else:
                        file_count += 1
    
    stats['downloaded_count'] = file_count
    stats['bg_downloaded_count'] = bg_count
    stats['disk_size'] = total_size
    stats['size_str'] = format_size(total_size)
    return stats

# ==========================================
# 6. CRON SCHEDULER (Threaded)
# ==========================================
def run_cron_job():
    if not plex: return
    cfg = get_config()
    
    log_verbose("Starting automated download cycle (Cron)...")
    mode = cfg.get('CRON_MODE', 'RANDOM')
    target_provider = cfg.get('CRON_PROVIDER', '').lower()
    dl_bgs = cfg.get('CRON_DOWNLOAD_BACKGROUNDS', False)
    cron_libs = cfg.get('CRON_LIBRARIES', [])
    ignored = cfg.get('IGNORED_LIBRARIES', [])
    
    try:
        libraries = plex.library.sections()
        processed = 0
        skipped = 0
        
        for lib in libraries:
            if cron_libs and lib.title not in cron_libs: continue
            if lib.title in ignored: continue
            if lib.type not in ['movie', 'show']: continue
            
            log_verbose(f"Cron: Processing Library '{lib.title}'...")
            items = lib.all()
            
            for item in items:
                tasks = [('poster', 'posters')]
                if dl_bgs: tasks.append(('background', 'arts'))
                
                for img_type, method in tasks:
                    if check_file_exists(item, lib.title, img_type):
                        skipped += 1
                        continue
                    
                    try: candidates = getattr(item, method)()
                    except: continue
                    if not candidates: continue
                    
                    selected_img = None
                    valid = [p for p in candidates if p.provider]
                    if not valid: continue

                    if mode == 'RANDOM':
                        selected_img = random.choice(valid)
                    elif mode in ['SPECIFIC_PROVIDER', 'RANDOM_PROVIDER']:
                        matching = [p for p in valid if target_provider in str(p.provider).lower()]
                        if matching:
                            if mode == 'SPECIFIC_PROVIDER': selected_img = matching[0]
                            else: selected_img = random.choice(matching)
                    
                    if selected_img:
                        try:
                            lib_title = item.section().title
                            save_path = get_target_file_path(item, lib_title, img_type=img_type)
                            if save_path:
                                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                                key = selected_img.key
                                url = key if key.startswith('http') else plex.url(key)
                                
                                log_verbose(f"Cron: Downloading {img_type} for {item.title} (Provider: {selected_img.provider})")
                                r = requests.get(url, stream=True)
                                if r.status_code == 200:
                                    with open(save_path, 'wb') as f:
                                        for chunk in r.iter_content(1024): f.write(chunk)
                                    save_download_history(item.ratingKey, url, img_type)
                                    processed += 1
                        except Exception as e:
                            log_verbose(f"Cron Error saving {item.title}: {e}")
        log_verbose(f"Cron Finished. Downloaded: {processed}, Skipped: {skipped}")
    except Exception as e:
        log_verbose(f"Cron Job Failed: {e}")

def scheduler_loop():
    last_run_date = None
    while True:
        cfg = get_config()
        if cfg.get('CRON_ENABLED'):
            target_time = cfg.get('CRON_TIME', '03:00')
            target_day = cfg.get('CRON_DAY', 'DAILY') # DAILY, MONDAY, etc.
            
            now = datetime.datetime.now()
            current_time = now.strftime("%H:%M")
            current_day_name = now.strftime("%A").upper()
            current_date = now.date()
            
            # Check Day of Week
            day_match = (target_day == 'DAILY') or (target_day == current_day_name)
            
            # Check Time + Day + Not already run today
            if day_match and current_time == target_time and last_run_date != current_date:
                run_cron_job()
                last_run_date = current_date
                time.sleep(60)
        time.sleep(30)

cron_thread = threading.Thread(target=scheduler_loop, daemon=True)
cron_thread.start()

# ==========================================
# 7. TEMPLATES & ROUTES
# ==========================================

@app.before_request
def require_auth():
    log_verbose(f"Request: {request.method} {request.path} from {request.remote_addr}")
    if request.endpoint in ['static', 'login', 'setup', 'logout', 'settings']: return
    cfg = get_config()
    if cfg.get('AUTH_DISABLED', False): return
    if 'AUTH_USER' not in cfg or not cfg['AUTH_USER']: return redirect(url_for('settings'))
    if 'user' not in session: return redirect(url_for('login'))
    session.permanent = True

@app.errorhandler(404)
def page_not_found(e):
    return render_template_string(HTML_TOP + """
        <div style="text-align:center; padding: 50px;">
            <h1>404</h1>
            <p>Page not found. <a href="/">Go Home</a></p>
        </div>
    """ + HTML_BOTTOM, title="404 Not Found", breadcrumbs=[]), 404

@app.context_processor
def inject_global_vars():
    server_name = plex.friendlyName if plex else "Disconnected"
    cfg = get_config()
    return dict(server_name=server_name, auth_disabled=cfg.get('AUTH_DISABLED', False), format_provider=format_provider)

CSS_COMMON = """
    :root { --bg: #121212; --nav: #232323; --card: #232323; --text: #e5e5e5; --text-muted: #a0a0a0; --accent: #E5A00D; --primary: #E5A00D; --btn-text: #000000; --warning: #cc7b19; --danger: #c0392b; --input-bg: #111111; --border-color: #3a3a3a; }
    body { font-family: 'Poppins', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; font-weight: 300; }
    h1, h2, h3 { color: var(--text); font-weight: 600; }
    a { text-decoration: none; color: inherit; transition: 0.2s; }
    input[type="checkbox"], input[type="radio"] { accent-color: var(--accent); }
    .nav { margin-bottom: 30px; padding: 15px 25px; background: var(--nav); border-radius: 12px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 6px rgba(0,0,0,0.3); border-bottom: 1px solid var(--border-color); }
    .nav-links { display: flex; align-items: center; }
    .nav-links a { margin-right: 20px; font-weight: 600; color: var(--text); }
    .nav-links a:hover { color: var(--primary); }
    .search-box { position: relative; width: 300px; margin: 0 20px; }
    .search-input { width: 100%; padding: 8px 15px; border-radius: 20px; border: 1px solid var(--border-color); background: var(--input-bg); color: var(--text); outline: none; transition: border-color 0.2s; }
    .search-input:focus { border-color: var(--primary); }
    .search-results { position: absolute; top: 100%; left: 0; right: 0; background: var(--card); border-radius: 8px; margin-top: 5px; box-shadow: 0 10px 15px rgba(0,0,0,0.5); z-index: 100; max-height: 400px; overflow-y: auto; display: none; border: 1px solid var(--border-color); }
    .search-result-item { display: flex; align-items: center; padding: 10px; border-bottom: 1px solid var(--border-color); cursor: pointer; text-decoration: none; color: var(--text); }
    .search-result-item:last-child { border-bottom: none; }
    .search-result-item:hover { background: #333333; }
    .search-thumb { width: 35px; height: 50px; object-fit: cover; border-radius: 4px; margin-right: 12px; background: #111; }
    .search-info { flex: 1; display: flex; flex-direction: column; }
    .search-title { font-weight: 600; font-size: 0.9em; display: block; }
    .search-meta { font-size: 0.75em; color: var(--text-muted); }
    .server-badge { background: rgba(255,255,255,0.05); color: var(--accent); padding: 6px 12px; border-radius: 6px; font-size: 0.9em; font-weight: 600; border: 1px solid var(--accent); }
    .settings-link { color: var(--text-muted); font-size: 1.2em; margin-left: 15px; }
    .settings-link:hover { color: var(--text); transform: rotate(90deg); }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 25px; }
    .home-grid { display: flex; flex-wrap: wrap; justify-content: center; gap: 25px; }
    .card { background: var(--card); border-radius: 12px; overflow: hidden; transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s; position: relative; box-shadow: 0 4px 6px rgba(0,0,0,0.2); border: 2px solid transparent; flex: 1 1 300px; max-width: 400px; min-width: 250px; }
    .card:hover { transform: translateY(-5px); cursor: pointer; box-shadow: 0 10px 15px rgba(0,0,0,0.4); border-color: var(--accent); }
    .card img { width: 100%; height: 300px; object-fit: cover; background: #000; }
    .card .title { padding: 15px; text-align: center; font-size: 1.1em; font-weight: 600; color: var(--text); line-height: 1.4; transition: color 0.2s; }
    .card:hover .title { color: var(--accent); }
    .home-card:hover { border: 2px solid var(--accent); }
    .home-card:hover .title { color: var(--accent) !important; }
    .poster-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 25px; }
    .background-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 25px; }
    .poster-card { background: var(--card); padding: 10px; border-radius: 12px; text-align: center; position: relative; border: 4px solid transparent; transition: 0.2s; }
    .img-container { position: relative; overflow: hidden; border-radius: 8px; }
    .poster-card img { width: 100%; border-radius: 8px; }
    .poster-card:hover { background: #333333; border-color: var(--accent); }
    .poster-card.selected { border-color: var(--accent); background: rgba(229, 160, 13, 0.1); }
    .background-card { background: var(--card); padding: 10px; border-radius: 12px; text-align: center; position: relative; border: 4px solid transparent; transition: 0.2s; }
    .background-card img { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 8px; }
    .background-card:hover { background: #333333; border-color: var(--accent); }
    .background-card.selected { border-color: var(--accent); background: rgba(229, 160, 13, 0.1); }
    .selected-badge { position: absolute; top: 8px; right: 8px; background: var(--accent); color: var(--btn-text); padding: 5px 12px; border-radius: 20px; font-weight: 800; font-size: 0.8em; box-shadow: 0 4px 6px rgba(0,0,0,0.5); }
    .provider-badge { position: absolute; top: 8px; left: 8px; background: rgba(0, 0, 0, 0.8); color: rgba(255, 255, 255, 0.8); padding: 3px 6px; border-radius: 4px; font-size: 0.75em; backdrop-filter: blur(2px); border: 1px solid rgba(255,255,255,0.1); pointer-events: none; }
    .btn { display: block; width: 100%; background: var(--primary); color: var(--btn-text); padding: 12px 0; border: none; cursor: pointer; margin-top: 12px; font-weight: 600; border-radius: 8px; font-family: inherit; }
    .btn:hover { filter: brightness(1.1); }
    .btn-danger { background: var(--danger); color: white; }
    .btn-danger:hover { background: #c0392b; }
    .btn-toggle { background: #3a3a3a; color: white; width: auto; display: inline-block; padding: 10px 20px; margin-left: 20px; }
    .btn-toggle.active { background: var(--accent); color: var(--btn-text); }
    .pagination { text-align: center; margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border-color); }
    .page-btn { background: var(--card); color: var(--text); padding: 10px 20px; text-decoration: none; border-radius: 6px; margin: 0 5px; display: inline-block; }
    .page-btn:hover { background: var(--primary); color: var(--btn-text); }
    .page-info { color: var(--text-muted); margin: 0 15px; }
    .form-group { margin-bottom: 20px; }
    .form-group label { display: block; margin-bottom: 8px; font-weight: 600; color: var(--text-muted); }
    .form-group input, .form-group select { width: 100%; padding: 12px; border-radius: 8px; border: 1px solid var(--border-color); background: var(--input-bg); color: white; font-family: inherit; box-sizing: border-box; }
    .form-group input:focus, .form-group select:focus { outline: none; border-color: var(--primary); }
    .form-group input:disabled { background: #0a0a0a; color: #555; border-color: #222; cursor: not-allowed; }
    .flash { background: rgba(229, 160, 13, 0.2); color: var(--accent); padding: 15px; margin-bottom: 25px; border-radius: 8px; border: 1px solid var(--accent); }
    .path-info { font-size: 0.85em; color: var(--text-muted); margin-bottom: 15px; font-family: monospace; background: rgba(0,0,0,0.3); padding: 5px 10px; border-radius: 4px; display: inline-block; }
    .section-header { margin-top: 50px; border-bottom: 2px solid var(--nav); padding-bottom: 10px; margin-bottom: 25px; display: flex; align-items: center; justify-content: space-between; }
    .section-header h2 { margin: 0; font-size: 1.4em; }
    .section-header span { font-size: 0.9em; color: var(--text-muted); background: var(--nav); padding: 4px 10px; border-radius: 20px; }
    .stats-container { margin-top: 60px; border-top: 1px solid var(--border-color); padding-top: 30px; }
    .stats-table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.2); }
    .stats-table th, .stats-table td { padding: 15px; text-align: left; border-bottom: 1px solid var(--border-color); }
    .stats-table th { background: rgba(255,255,255,0.05); font-weight: 600; color: var(--text-muted); text-transform: uppercase; font-size: 0.85em; letter-spacing: 1px; }
    .stats-table tr:last-child td { border-bottom: none; }
    .stats-table tr:hover { background: rgba(255,255,255,0.02); }
    .stat-number { font-family: monospace; color: var(--accent); font-weight: bold; }
    .tabs { display: flex; border-bottom: 1px solid var(--border-color); margin-bottom: 20px; }
    .tab-btn { background: transparent; border: none; padding: 15px 25px; font-size: 1.1em; font-weight: 600; color: var(--text-muted); cursor: pointer; border-bottom: 3px solid transparent; }
    .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
    .tab-btn:hover { color: var(--text); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
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
        if (query.length < 2) { resultsDiv.style.display = 'none'; resultsDiv.innerHTML = ''; return; }
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
                    } else { resultsDiv.style.display = 'none'; }
                }).catch(err => console.error(err));
        }, 300);
    }
    function hideSearch() { setTimeout(() => { document.getElementById('search-results').style.display = 'none'; }, 200); }
    function switchTab(tabId) {
        document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
        document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
        document.getElementById(tabId).style.display = 'block';
        document.querySelector(`button[onclick="switchTab('${tabId}')"]`).classList.add('active');
    }
    
    function updateCronUI() {
        const mode = document.querySelector('select[name="cron_mode"]').value;
        const providerDiv = document.getElementById('cron_provider_div');
        const providerInput = document.querySelector('select[name="cron_provider"]');
        
        if (mode === 'RANDOM') {
            providerInput.disabled = true;
            providerDiv.style.opacity = '0.4';
        } else {
            providerInput.disabled = false;
            providerDiv.style.opacity = '1.0';
        }
    }
    
    window.addEventListener('DOMContentLoaded', () => {
        if(document.querySelector('select[name="cron_mode"]')) {
            updateCronUI();
        }
    });
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
            <div class="server-badge">Plex Server: {{ server_name }}</div>
            <a href="/settings" class="settings-link" title="Settings">⚙️</a>
            {% if not auth_disabled %}
                <a href="/logout" class="settings-link" title="Logout" style="font-size:0.9em; margin-left: 20px;">Logout</a>
            {% endif %}
        </div>
    </div>
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            {% for message in messages %}<div class="flash">{{ message }}</div>{% endfor %}
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
"""

HTML_BOTTOM = "</body></html>"
HTML_LOGIN_SETUP = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{{ title }} - Poster Manager</title><link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🎬</text></svg>"><link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600&display=swap" rel="stylesheet"><style>""" + CSS_COMMON + """body{display:flex;align-items:center;justify-content:center;height:100vh;padding:0}.auth-container{width:100%;max-width:400px}.card{padding:40px;transform:none!important;cursor:default!important}.card:hover{transform:none;box-shadow:0 4px 6px rgba(0,0,0,0.1)}</style></head><body><div class="auth-container"><div class="card"><div style="text-align:center;margin-bottom:30px"><div style="font-size:3em">🎬</div><h2>{{ title }}</h2><p style="color:var(--text-muted)">{{ subtitle }}</p></div>{% with messages = get_flashed_messages() %}{% if messages %}{% for message in messages %}<div class="flash" style="text-align:center">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}<form method="post"><div class="form-group"><label>Username</label><input type="text" name="username" required autofocus></div><div class="form-group"><label>Password</label><input type="password" name="password" required></div>{% if is_setup %}<div class="form-group"><label>Confirm Password</label><input type="password" name="confirm_password" required></div>{% endif %}<button type="submit" class="btn">{{ btn_text }}</button></form></div></div></body></html>"""

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
    visible_libs = [lib for lib in libs if lib.title not in ignored]
    
    lib_stats = []
    for lib in visible_libs:
        stats = get_library_stats(lib)
        lib_stats.append({
            'title': lib.title,
            'content': stats['content_str'],
            'posters': stats['downloaded_count'],
            'backgrounds': stats['bg_downloaded_count'],
            'size': stats['size_str']
        })

    content = """
    <div class="home-grid">
        {% for lib in visible_libs %}
            {% set icon = '🎬' if lib.type == 'movie' else '📺' if lib.type == 'show' else '📁' %}
            <a href="/library/{{ lib.key }}" class="card home-card" style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 40px 20px; min-height: 200px; text-decoration: none;">
                <div style="font-size: 4em; margin-bottom: 15px; text-shadow: 0 0 10px var(--accent);">{{ icon }}</div>
                <div class="title" style="font-size: 1.4em; font-weight: 700; text-align: center; color: var(--text); margin-bottom: 5px;">{{ lib.title }}</div>
                <div style="font-size: 0.85em; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; font-weight: 500;">{{ lib.type.title() }}</div>
            </a>
        {% endfor %}
    </div>
    <div class="stats-container">
        <div class="section-header" style="margin-top:0;"><h2>Library Statistics</h2></div>
        <table class="stats-table">
            <thead><tr><th>Library</th><th>Content</th><th>Posters</th><th>Backgrounds</th><th>Disk Usage</th></tr></thead>
            <tbody>
                {% for stat in lib_stats %}
                <tr>
                    <td style="font-weight:600; color:var(--text);">{{ stat.title }}</td>
                    <td class="stat-number">{{ stat.content }}</td>
                    <td class="stat-number" style="color:var(--accent);">{{ stat.posters }}</td>
                    <td class="stat-number" style="color:var(--text-muted);">{{ stat.backgrounds }}</td>
                    <td class="stat-number" style="color:var(--text-muted);">{{ stat.size }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    """
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, visible_libs=visible_libs, lib_stats=lib_stats, title="Select a Library", breadcrumbs=[], toggle_override=False)

@app.route('/api/search')
def api_search():
    if not plex: return jsonify([])
    query = request.args.get('q', '')
    if len(query) < 2: return jsonify([])
    try:
        results = plex.search(query, limit=20)
        data = []
        for item in results:
            if item.type not in ['movie', 'show']: continue
            thumb = item.thumbUrl if item.thumb else ''
            year = getattr(item, 'year', '')
            data.append({
                'title': item.title,
                'year': year,
                'ratingKey': item.ratingKey,
                'thumb': thumb,
                'type': item.type
            })
            if len(data) >= 10: break
        return jsonify(data)
    except: return jsonify([])

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    cfg = get_config()
    if 'AUTH_USER' in cfg and cfg['AUTH_USER']: return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        confirm = request.form['confirm_password']
        if password != confirm: flash("Passwords do not match.")
        elif len(password) < 4: flash("Password must be at least 4 characters.")
        else:
            cfg['AUTH_USER'] = username
            cfg['AUTH_HASH'] = generate_password_hash(password)
            cfg['AUTH_DISABLED'] = False
            save_config(cfg)
            flash("Account created! Please login.")
            return redirect(url_for('login'))
    return render_template_string(HTML_LOGIN_SETUP, title="Setup Admin", subtitle="Create your admin account to secure access.", btn_text="Create Account", is_setup=True)

@app.route('/login', methods=['GET', 'POST'])
def login():
    cfg = get_config()
    if cfg.get('AUTH_DISABLED', False): return redirect(url_for('home'))
    if 'user' in session: return redirect(url_for('home'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        stored_user = cfg.get('AUTH_USER')
        stored_hash = cfg.get('AUTH_HASH')
        if username == stored_user and check_password_hash(stored_hash, password):
            session.permanent = True
            session['user'] = username
            return redirect(url_for('home'))
        else: flash("Invalid username or password.")
    return render_template_string(HTML_LOGIN_SETUP, title="Login", subtitle="Please sign in to continue.", btn_text="Sign In", is_setup=False)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    cfg = get_config()
    is_unconfigured = 'AUTH_USER' not in cfg or not cfg['AUTH_USER']
    auth_disabled = cfg.get('AUTH_DISABLED', False)
    all_libs = []
    if plex:
        try: all_libs = plex.library.sections()
        except: pass
    
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update_config':
            cfg['PLEX_URL'] = request.form.get('plex_url', '').strip()
            # Handle Token Update: Only update if not the placeholder
            token_input = request.form.get('plex_token', '').strip()
            if token_input:
                cfg['PLEX_TOKEN'] = token_input
            
            cfg['DOWNLOAD_BASE_DIR'] = request.form.get('download_dir', 'downloaded_posters').strip()
            cfg['HISTORY_FILE'] = request.form.get('history_file', 'download_history.json').strip()
            cfg['ASSET_STYLE'] = request.form.get('asset_style', 'ASSET_FOLDERS')
            cfg['CRON_ENABLED'] = (request.form.get('cron_enabled') == 'on')
            
            # Time Handling
            day = request.form.get('cron_day', 'DAILY')
            # 12h to 24h Conversion
            h_12 = int(request.form.get('cron_hour', '12'))
            m = int(request.form.get('cron_minute', '00'))
            ampm = request.form.get('cron_ampm', 'AM')
            
            h_24 = h_12
            if ampm == 'PM' and h_12 != 12:
                h_24 += 12
            elif ampm == 'AM' and h_12 == 12:
                h_24 = 0
            
            cfg['CRON_DAY'] = day
            cfg['CRON_TIME'] = f"{h_24:02d}:{m:02d}"

            cfg['CRON_MODE'] = request.form.get('cron_mode', 'RANDOM')
            cfg['CRON_PROVIDER'] = request.form.get('cron_provider', '').strip()
            cfg['CRON_DOWNLOAD_BACKGROUNDS'] = (request.form.get('cron_download_backgrounds') == 'on')
            cfg['VERBOSE_LOGGING'] = (request.form.get('cron_logging') == 'on')
            cfg['IGNORED_LIBRARIES'] = request.form.getlist('ignored_libs')
            cfg['CRON_LIBRARIES'] = request.form.getlist('cron_libs')
            save_config(cfg)
            if init_plex(): flash("Settings saved and connected!")
            else: flash("Settings saved but connection failed.")
            return redirect(url_for('home'))
        elif action == 'migrate_assets':
            target_style = request.form.get('target_style')
            count, error = perform_migration(target_style)
            if error: flash(f"Migration error: {error}")
            else: flash(f"Migrated {count} files.")
            cfg['ASSET_STYLE'] = target_style
            save_config(cfg)
        elif action == 'change_password':
            current_pw = request.form.get('current_password')
            new_pw = request.form.get('new_password')
            confirm_pw = request.form.get('confirm_password')
            stored_hash = cfg.get('AUTH_HASH')
            if not stored_hash or not check_password_hash(stored_hash, current_pw): flash("Current password incorrect.")
            elif new_pw != confirm_pw: flash("New passwords do not match.")
            elif len(new_pw) < 4: flash("Password too short.")
            else:
                cfg['AUTH_HASH'] = generate_password_hash(new_pw)
                save_config(cfg)
                flash("Password updated.")
        elif action == 'create_account':
            username = request.form.get('new_username')
            new_pw = request.form.get('new_password')
            confirm_pw = request.form.get('confirm_password')
            if new_pw != confirm_pw: flash("Passwords do not match.")
            elif len(new_pw) < 4: flash("Password too short.")
            else:
                cfg['AUTH_USER'] = username
                cfg['AUTH_HASH'] = generate_password_hash(new_pw)
                cfg['AUTH_DISABLED'] = False
                save_config(cfg)
                session.permanent = True
                session['user'] = username
                flash("Account created!")
                return redirect(url_for('home'))
        elif action == 'disable_auth':
            if is_unconfigured:
                cfg['AUTH_DISABLED'] = True
                save_config(cfg)
                return redirect(url_for('home'))
            else:
                current_pw = request.form.get('current_password_disable')
                stored_hash = cfg.get('AUTH_HASH')
                if not stored_hash or not check_password_hash(stored_hash, current_pw): flash("Incorrect password.")
                else:
                    cfg['AUTH_DISABLED'] = True
                    cfg.pop('AUTH_USER', None)
                    cfg.pop('AUTH_HASH', None)
                    save_config(cfg)
                    session.clear()
                    return redirect(url_for('home'))
        return redirect(url_for('settings'))

    # Helper for Time Selects (24h -> 12h)
    cron_time = cfg.get('CRON_TIME', '03:00')
    try:
        h_24_str, m_str = cron_time.split(':')
        h_24 = int(h_24_str)
        c_minute = m_str
        c_ampm = 'AM' if h_24 < 12 else 'PM'
        c_hour_12 = h_24
        if h_24 == 0: c_hour_12 = 12
        elif h_24 > 12: c_hour_12 = h_24 - 12
        c_hour = f"{c_hour_12:02d}"
    except:
        c_hour, c_minute, c_ampm = '03', '00', 'AM'

    # Prepare config for display (mask token)
    display_cfg = cfg.copy()
    if display_cfg['PLEX_TOKEN']:
        display_cfg['PLEX_TOKEN'] = '(Encrypted)'

    content = """
    <div style="max-width: 800px; margin: 0 auto;">
        <div class="card" style="padding: 30px; cursor: default; transform: none; box-shadow: none; margin-bottom: 30px;">
            <h2 style="margin-top:0;">Configuration</h2>
            <form method="post">
                <input type="hidden" name="action" value="update_config">
                <div class="form-group"><label>Plex Server URL</label><input type="text" name="plex_url" value="{{ cfg.PLEX_URL }}" placeholder="http://localhost:32400"></div>
                
                <div class="form-group">
                    <label>Plex Token (X-Plex-Token)</label>
                    {% if cfg.PLEX_TOKEN == '(Encrypted)' %}
                        <div id="token-view" style="display:flex; gap:10px;">
                            <input type="text" value="(Encrypted)" disabled style="background:#2a2a2a; border:1px solid #444; color:#888;">
                            <button type="button" class="btn" style="width:auto; margin:0; padding:10px 20px;" onclick="document.getElementById('token-view').style.display='none'; document.getElementById('token-edit').style.display='block';">Change</button>
                        </div>
                        <div id="token-edit" style="display:none;">
                            <input type="text" name="plex_token" placeholder="Enter new X-Plex-Token">
                        </div>
                    {% else %}
                        <input type="text" name="plex_token" value="{{ cfg.PLEX_TOKEN }}" placeholder="Your Plex Token">
                    {% endif %}
                </div>

                <div class="form-group"><label>Download Directory</label><input type="text" name="download_dir" value="{{ cfg.DOWNLOAD_BASE_DIR }}"></div>
                <div class="form-group"><label>Asset Folder Style</label>
                    <select name="asset_style">
                        <option value="ASSET_FOLDERS" {% if cfg.ASSET_STYLE == 'ASSET_FOLDERS' %}selected{% endif %}>Asset Folders (Kometa Default)</option>
                        <option value="NO_ASSET_FOLDERS" {% if cfg.ASSET_STYLE == 'NO_ASSET_FOLDERS' %}selected{% endif %}>No Asset Folders (Flat)</option>
                    </select>
                    <small style="color:var(--text-muted); display:block; margin-top:5px;">
                        <strong>Asset Folders:</strong> Movies/Show Name/poster.jpg<br>
                        <strong>No Asset Folders:</strong> Movies/Movie Name.jpg
                    </small>
                </div>
                <div class="form-group"><label>History File Name</label><input type="text" name="history_file" value="{{ cfg.HISTORY_FILE }}"></div>
                
                <div class="form-group" style="display:flex; align-items:center; gap:10px; margin-top: 15px;">
                    <input type="checkbox" name="cron_logging" id="cron_logging" style="width:auto;" {% if cfg.VERBOSE_LOGGING %}checked{% endif %}>
                    <label for="cron_logging" style="margin:0; font-weight: 400;">Enable Global Verbose Logging</label>
                </div>

                <div class="form-group"><label>Manage Hidden Libraries</label>
                    {% if all_libs %}
                        <div style="max-height: 200px; overflow-y: auto; background: #141719; border: 1px solid #4b5563; border-radius: 8px; padding: 10px;">
                            {% for lib in all_libs %}
                            <div style="display:flex; align-items:center; margin-bottom:8px;">
                                <input type="checkbox" name="ignored_libs" value="{{ lib.title }}" id="lib_{{ loop.index }}" {% if lib.title in cfg.IGNORED_LIBRARIES %}checked{% endif %} style="width:auto; margin-right:10px;">
                                <label for="lib_{{ loop.index }}" style="margin:0; font-weight:400; color:var(--text); cursor:pointer;">{{ lib.title }}</label>
                            </div>
                            {% endfor %}
                        </div>
                    {% else %}<p>Connect Plex to see libraries.</p>{% endif %}
                </div>
                <div style="margin-top: 30px; border-top: 1px solid #4b5563; padding-top: 20px;">
                    <h3 style="margin-top:0;">Automated Downloads (Cron)</h3>
                    <div class="form-group" style="display:flex; align-items:center; gap:10px;">
                        <input type="checkbox" name="cron_enabled" id="cron_enabled" style="width:auto;" {% if cfg.CRON_ENABLED %}checked{% endif %}>
                        <label for="cron_enabled" style="margin:0;">Enable Schedule</label>
                    </div>
                    <div class="form-group" style="display:flex; align-items:center; gap:10px;">
                        <input type="checkbox" name="cron_download_backgrounds" id="cron_download_backgrounds" style="width:auto;" {% if cfg.CRON_DOWNLOAD_BACKGROUNDS %}checked{% endif %}>
                        <label for="cron_download_backgrounds" style="margin:0;">Download Backgrounds</label>
                    </div>

                    <div class="form-group" style="display: flex; gap: 20px;">
                        <div style="flex: 1;">
                            <label>Run On Day</label>
                            <select name="cron_day">
                                <option value="DAILY" {% if cfg.CRON_DAY == 'DAILY' %}selected{% endif %}>Every Day</option>
                                <option value="MONDAY" {% if cfg.CRON_DAY == 'MONDAY' %}selected{% endif %}>Monday</option>
                                <option value="TUESDAY" {% if cfg.CRON_DAY == 'TUESDAY' %}selected{% endif %}>Tuesday</option>
                                <option value="WEDNESDAY" {% if cfg.CRON_DAY == 'WEDNESDAY' %}selected{% endif %}>Wednesday</option>
                                <option value="THURSDAY" {% if cfg.CRON_DAY == 'THURSDAY' %}selected{% endif %}>Thursday</option>
                                <option value="FRIDAY" {% if cfg.CRON_DAY == 'FRIDAY' %}selected{% endif %}>Friday</option>
                                <option value="SATURDAY" {% if cfg.CRON_DAY == 'SATURDAY' %}selected{% endif %}>Saturday</option>
                                <option value="SUNDAY" {% if cfg.CRON_DAY == 'SUNDAY' %}selected{% endif %}>Sunday</option>
                            </select>
                        </div>
                        <div style="flex: 1;">
                            <label>Run At</label>
                            <div style="display:flex; gap:10px;">
                                <select name="cron_hour" style="flex:1;">
                                    {% for h in range(1, 13) %}
                                        <option value="{{ '%02d' % h }}" {% if ('%02d' % h) == c_hour %}selected{% endif %}>{{ h }}</option>
                                    {% endfor %}
                                </select>
                                <select name="cron_minute" style="flex:1;">
                                    {% for m in range(0, 60) %}
                                        <option value="{{ '%02d' % m }}" {% if ('%02d' % m) == c_minute %}selected{% endif %}>{{ '%02d' % m }}</option>
                                    {% endfor %}
                                </select>
                                <select name="cron_ampm" style="flex:1;">
                                    <option value="AM" {% if c_ampm == 'AM' %}selected{% endif %}>AM</option>
                                    <option value="PM" {% if c_ampm == 'PM' %}selected{% endif %}>PM</option>
                                </select>
                            </div>
                        </div>
                    </div>

                    <div class="form-group"><label>Libraries to Run On</label>
                        {% if all_libs %}
                            <div style="max-height: 150px; overflow-y: auto; background: #141719; border: 1px solid #4b5563; border-radius: 8px; padding: 10px;">
                                {% for lib in all_libs %}
                                <div style="display:flex; align-items:center; margin-bottom:8px;">
                                    <input type="checkbox" name="cron_libs" value="{{ lib.title }}" id="cron_lib_{{ loop.index }}" {% if lib.title in cfg.CRON_LIBRARIES %}checked{% endif %} style="width:auto; margin-right:10px;">
                                    <label for="cron_lib_{{ loop.index }}" style="margin:0; font-weight:400; color:var(--text); cursor:pointer;">{{ lib.title }}</label>
                                </div>
                                {% endfor %}
                            </div>
                        {% endif %}
                    </div>
                    <div class="form-group"><label>Selection Mode</label>
                        <select name="cron_mode" onchange="updateCronUI()">
                            <option value="RANDOM" {% if cfg.CRON_MODE == 'RANDOM' %}selected{% endif %}>Random (No Uploads)</option>
                            <option value="SPECIFIC_PROVIDER" {% if cfg.CRON_MODE == 'SPECIFIC_PROVIDER' %}selected{% endif %}>First from Provider</option>
                            <option value="RANDOM_PROVIDER" {% if cfg.CRON_MODE == 'RANDOM_PROVIDER' %}selected{% endif %}>Random from Provider</option>
                        </select>
                    </div>
                    <div class="form-group" id="cron_provider_div"><label>Provider Name</label>
                        <select name="cron_provider">
                            <option value="tmdb" {% if cfg.CRON_PROVIDER == 'tmdb' %}selected{% endif %}>TMDB</option>
                            <option value="tvdb" {% if cfg.CRON_PROVIDER == 'tvdb' %}selected{% endif %}>TVDB</option>
                            <option value="fanart" {% if cfg.CRON_PROVIDER == 'fanart' %}selected{% endif %}>Fanart.tv</option>
                            <option value="gracenote" {% if cfg.CRON_PROVIDER == 'gracenote' %}selected{% endif %}>Gracenote</option>
                            <option value="movieposterdb" {% if cfg.CRON_PROVIDER == 'movieposterdb' %}selected{% endif %}>MoviePosterDB</option>
                            <option value="local" {% if cfg.CRON_PROVIDER == 'local' %}selected{% endif %}>Local</option>
                        </select>
                    </div>
                </div>
                <button type="submit" class="btn">Save & Connect</button>
            </form>
        </div>
        <div class="card" style="padding: 30px; cursor: default; transform: none; box-shadow: none; margin-bottom: 30px;">
            <h2 style="margin-top:0;">File Migration</h2>
            <form method="post" onsubmit="return confirm('Migrate files?');">
                <input type="hidden" name="action" value="migrate_assets">
                <div class="form-group"><label>Convert To:</label>
                    <select name="target_style">
                        <option value="ASSET_FOLDERS">Asset Folders</option>
                        <option value="NO_ASSET_FOLDERS">No Asset Folders</option>
                    </select>
                </div>
                <button type="submit" class="btn">Migrate Files</button>
            </form>
        </div>
        <div class="card" style="padding: 30px; cursor: default; transform: none; box-shadow: none;">
            <h2 style="margin-top:0;">Security</h2>
            {% if is_unconfigured or auth_disabled %}
                {% if auth_disabled %}<p>Auth disabled. Use form to enable.</p>{% else %}<p>Auth not set up.</p>{% endif %}
                <form method="post" style="margin-bottom:30px">
                    <input type="hidden" name="action" value="create_account">
                    <div class="form-group"><label>Username</label><input type="text" name="new_username" required></div>
                    <div class="form-group"><label>Password</label><input type="password" name="new_password" required></div>
                    <div class="form-group"><label>Confirm</label><input type="password" name="confirm_password" required></div>
                    <button type="submit" class="btn">Create Account</button>
                </form>
                {% if not auth_disabled %}
                <form method="post" onsubmit="return confirm('Disable auth?');">
                    <input type="hidden" name="action" value="disable_auth">
                    <button type="submit" class="btn btn-danger">Disable Auth</button>
                </form>
                {% endif %}
            {% else %}
                <form method="post" style="margin-bottom:30px">
                    <input type="hidden" name="action" value="change_password">
                    <div class="form-group"><label>Username</label><input type="text" value="{{ cfg.AUTH_USER }}" disabled></div>
                    <div class="form-group"><label>Current Password</label><input type="password" name="current_password" required></div>
                    <div class="form-group"><label>New Password</label><input type="password" name="new_password" required></div>
                    <div class="form-group"><label>Confirm</label><input type="password" name="confirm_password" required></div>
                    <button type="submit" class="btn">Update Password</button>
                </form>
                <form method="post" onsubmit="return confirm('Disable auth?');">
                    <input type="hidden" name="action" value="disable_auth">
                    <div class="form-group"><label>Password</label><input type="password" name="current_password_disable" required></div>
                    <button type="submit" class="btn btn-danger">Disable Auth</button>
                </form>
            {% endif %}
        </div>
    </div>
    """
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, title="Settings", cfg=display_cfg, all_libs=all_libs, c_hour=c_hour, c_minute=c_minute, c_ampm=c_ampm, breadcrumbs=[('Settings', '#')], toggle_override=False, is_unconfigured=is_unconfigured, auth_disabled=auth_disabled)

@app.route('/library/<lib_id>')
def view_library(lib_id):
    if not plex: return redirect(url_for('settings'))
    try: lib = plex.library.sectionByID(int(lib_id))
    except: return redirect('/')
    
    page = request.args.get('page', 1, type=int)
    per_page = 50
    offset = (page - 1) * per_page
    total_items = lib.totalSize
    items = lib.search(maxresults=per_page, container_start=offset)
    total_pages = math.ceil(total_items / per_page)
    
    history = load_history_data()
    all_keys = list(history['downloads'].keys()) + list(history['overrides'])
    valid_keys = [int(k) for k in set(all_keys) if k.isdigit()]
    
    done_objs = []
    if valid_keys:
        try: done_objs = lib.search(id=valid_keys)
        except: pass
    
    done_ids_map = {item.ratingKey: item for item in done_objs}
    
    # Self Healing
    keys_rm = []
    for key, item in list(done_ids_map.items()):
        if get_item_status(item, lib.title) != 'complete': keys_rm.append(key)
    if keys_rm:
        for k in keys_rm:
            del done_ids_map[k]
            if str(k) in history['downloads']: del history['downloads'][str(k)]
            if str(k) in history['overrides']: history['overrides'].remove(str(k))
        save_history_data(history)

    todo_items = []
    partial_items = []
    new_found = []
    
    for i in items:
        status = get_item_status(i, lib.title)
        if status == 'complete':
            if i.ratingKey not in done_ids_map:
                done_ids_map[i.ratingKey] = i
                new_found.append(i.ratingKey)
        elif status == 'partial':
            thumb = i.thumbUrl if i.thumb else ''
            partial_items.append({'title': i.title, 'ratingKey': i.ratingKey, 'thumbUrl': thumb})
        else:
            thumb = i.thumbUrl if i.thumb else ''
            todo_items.append({'title': i.title, 'ratingKey': i.ratingKey, 'thumbUrl': thumb})

    if new_found:
        for k in new_found: history['downloads'][str(k)] = "restored"
        save_history_data(history)

    done_items_list = []
    for key, item in done_ids_map.items():
        thumb = item.thumbUrl if item.thumb else ''
        done_items_list.append({'title': item.title, 'ratingKey': item.ratingKey, 'thumbUrl': thumb})
    done_items_list.sort(key=lambda x: x['title'])

    pagination_block = """<div class="pagination" style="margin:30px 0;border-top:1px solid #444;padding-top:20px"><div style="display:flex;align-items:center;justify-content:center;gap:15px">"""
    if page > 1: pagination_block += f'<a href="?page={page-1}" class="page-btn">&laquo; Prev</a>'
    else: pagination_block += '<span class="page-btn" style="opacity:0.5;cursor:not-allowed">&laquo; Prev</span>'
    
    pagination_block += f"""<form action="" method="get" style="display:flex;align-items:center;gap:10px;margin:0"><label style="margin:0;color:var(--text-muted)">Page</label><select name="page" onchange="this.form.submit()" style="padding:8px;border-radius:6px;background:var(--bg);color:var(--text);border:1px solid #4b5563;cursor:pointer;min-width:80px">"""
    for p in range(1, total_pages + 1):
        sel = 'selected' if p == page else ''
        pagination_block += f'<option value="{p}" {sel}>{p}</option>'
    pagination_block += f"""</select><span style="color:var(--text-muted)">of {total_pages}</span></form>"""
    
    if page < total_pages: pagination_block += f'<a href="?page={page+1}" class="page-btn">Next &raquo;</a>'
    else: pagination_block += '<span class="page-btn" style="opacity:0.5;cursor:not-allowed">Next &raquo;</span>'
    pagination_block += "</div></div>"

    content = pagination_block
    if todo_items:
        content += f"""<div class="section-header"><h2>Missing Posters</h2><span>{len(todo_items)} on page</span></div><div class="grid">"""
        for i in todo_items: content += f"""<a href="/item/{i['ratingKey']}" class="card"><img src="{i['thumbUrl']}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'"><div class="title">{i['title']}</div></a>"""
        content += "</div>"
    if partial_items:
        content += f"""<div class="section-header"><h2 style="color:var(--warning)">Half Missing</h2><span>{len(partial_items)} on page</span></div><div class="grid">"""
        for i in partial_items: content += f"""<a href="/item/{i['ratingKey']}" class="card" style="border:2px solid var(--warning)"><img src="{i['thumbUrl']}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'"><div class="title">⚠️ {i['title']}</div></a>"""
        content += "</div>"
    if done_items_list:
        content += f"""<div class="section-header"><h2 style="color:var(--accent)">Already Downloaded</h2><span>{len(done_items_list)} total</span></div><div class="grid">"""
        for i in done_items_list: content += f"""<a href="/item/{i['ratingKey']}" class="card" style="opacity:0.7"><img src="{i['thumbUrl']}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'"><div class="title">✅ {i['title']}</div></a>"""
        content += "</div>"
    if not todo_items and not partial_items and not done_items_list: content += "<p>No items found.</p>"
    content += pagination_block

    return render_template_string(HTML_TOP + content + HTML_BOTTOM, title=lib.title, breadcrumbs=[(lib.title, '#')], toggle_override=False)

@app.route('/item/<rating_key>')
def view_item(rating_key):
    if not plex: return redirect(url_for('settings'))
    try: item = plex.fetchItem(int(rating_key))
    except: return "Not Found", 404
    
    is_show = item.type == 'show'
    posters = item.posters()
    backgrounds = item.arts()
    folder_name = get_physical_folder_name(item)
    lib = item.section()
    
    sel_poster = get_history_url(rating_key, 'poster')
    sel_bg = get_history_url(rating_key, 'background')
    
    if sel_poster and not check_file_exists(item, lib.title, 'poster'): sel_poster = None
    if sel_bg and not check_file_exists(item, lib.title, 'background'): sel_bg = None
    
    seasons = item.seasons() if is_show else []
    target_path = get_target_file_path(item, lib.title)
    
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', '')
    if not os.path.isabs(base_dir) and DATA_DIR != '.': base_dir = os.path.join(DATA_DIR, base_dir)
    rel_path = os.path.relpath(os.path.dirname(target_path), base_dir) if target_path else "Unknown"

    content = f"""
    <div class="path-info">Target Folder: <strong>.../{rel_path}/</strong></div>
    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('tab-posters')">Posters</button>
        <button class="tab-btn" onclick="switchTab('tab-backgrounds')">Backgrounds</button>
    </div>
    
    <div id="tab-posters" class="tab-content active"><div class="poster-grid">"""
    for p in posters:
        p_url = get_poster_url(p)
        sel_class = 'selected' if p_url == sel_poster else ''
        badge = f'<div class="selected-badge">CURRENT</div>' if sel_class else ''
        content += f"""
        <form action="/download" method="post" class="poster-card {sel_class}">
            <div class="img-container">
                {badge}
                <img src="{p_url}" loading="lazy">
                <div class="provider-badge">{format_provider(p.provider)}</div>
            </div>
            <input type="hidden" name="img_url" value="{p_url}">
            <input type="hidden" name="rating_key" value="{item.ratingKey}">
            <input type="hidden" name="img_type" value="poster">
            <button type="submit" class="btn">Download</button>
        </form>"""
    content += "</div></div>"
    
    content += """<div id="tab-backgrounds" class="tab-content"><div class="background-grid">"""
    for bg in backgrounds:
        b_url = get_poster_url(bg)
        sel_class = 'selected' if b_url == sel_bg else ''
        badge = f'<div class="selected-badge">CURRENT</div>' if sel_class else ''
        content += f"""
        <form action="/download" method="post" class="background-card {sel_class}">
            <div class="img-container">
                {badge}
                <img src="{b_url}" loading="lazy">
                <div class="provider-badge">{format_provider(bg.provider)}</div>
            </div>
            <input type="hidden" name="img_url" value="{b_url}">
            <input type="hidden" name="rating_key" value="{item.ratingKey}">
            <input type="hidden" name="img_type" value="background">
            <button type="submit" class="btn">Download</button>
        </form>"""
    content += "</div></div>"
    
    if is_show:
        content += f"""<div class="section-header"><h2>Seasons</h2></div><div class="grid">"""
        for s in seasons:
            thumb = s.thumbUrl if s.thumb else ''
            content += f"""<a href="/season/{s.ratingKey}" class="card"><img src="{thumb}" loading="lazy"><div class="title">{s.title}</div></a>"""
        content += "</div>"
        
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, title=item.title, breadcrumbs=[(lib.title, f'/library/{lib.key}'), (item.title, '#')], 
                                  rating_key=item.ratingKey, toggle_override=is_show, is_overridden=is_overridden(item.ratingKey))

@app.route('/season/<rating_key>')
def view_season(rating_key):
    if not plex: return redirect(url_for('settings'))
    season = plex.fetchItem(int(rating_key))
    show = season.show()
    posters = season.posters()
    backgrounds = season.arts()
    lib = show.section()
    
    sel_poster = get_history_url(rating_key, 'poster')
    sel_bg = get_history_url(rating_key, 'background')
    
    if sel_poster and not check_file_exists(season, lib.title, 'poster'): sel_poster = None
    if sel_bg and not check_file_exists(season, lib.title, 'background'): sel_bg = None
    
    target_path = get_target_file_path(season, lib.title)
    cfg = get_config()
    base_dir = cfg.get('DOWNLOAD_BASE_DIR', '')
    if not os.path.isabs(base_dir) and DATA_DIR != '.': base_dir = os.path.join(DATA_DIR, base_dir)
    rel_path = os.path.relpath(os.path.dirname(target_path), base_dir) if target_path else "Unknown"
    
    content = f"""
    <div class="path-info">Target: <strong>.../{rel_path}/</strong></div>
    <div class="tabs"><button class="tab-btn active" onclick="switchTab('tab-posters')">Posters</button><button class="tab-btn" onclick="switchTab('tab-backgrounds')">Backgrounds</button></div>
    
    <div id="tab-posters" class="tab-content active"><div class="poster-grid">"""
    for p in posters:
        p_url = get_poster_url(p)
        sel_class = 'selected' if p_url == sel_poster else ''
        badge = f'<div class="selected-badge">CURRENT</div>' if sel_class else ''
        content += f"""
        <form action="/download" method="post" class="poster-card {sel_class}">
            <div class="img-container">{badge}<img src="{p_url}" loading="lazy"><div class="provider-badge">{format_provider(p.provider)}</div></div>
            <input type="hidden" name="img_url" value="{{ p_url }}">
            <input type="hidden" name="rating_key" value="{{ season.ratingKey }}">
            <input type="hidden" name="img_type" value="poster">
            <button type="submit" class="btn">Download</button>
        </form>"""
    content += "</div></div>"
    
    content += """<div id="tab-backgrounds" class="tab-content"><div class="background-grid">"""
    for bg in backgrounds:
        b_url = get_poster_url(bg)
        sel_class = 'selected' if b_url == sel_bg else ''
        badge = f'<div class="selected-badge">CURRENT</div>' if sel_class else ''
        content += f"""
        <form action="/download" method="post" class="background-card {sel_class}">
            <div class="img-container">{badge}<img src="{b_url}" loading="lazy"><div class="provider-badge">{format_provider(bg.provider)}</div></div>
            <input type="hidden" name="img_url" value="{{ b_url }}">
            <input type="hidden" name="rating_key" value="{{ season.ratingKey }}">
            <input type="hidden" name="img_type" value="background">
            <button type="submit" class="btn">Download</button>
        </form>"""
    content += "</div></div>"
    
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, title=f"{show.title} - {season.title}", breadcrumbs=[(lib.title, f'/library/{lib.key}'), (show.title, f'/item/{show.ratingKey}'), (season.title, '#')], toggle_override=False)

@app.route('/download', methods=['POST'])
def download():
    if not plex: return redirect(url_for('settings'))
    img_url = request.form.get('img_url')
    rating_key = request.form.get('rating_key')
    img_type = request.form.get('img_type', 'poster')
    try:
        item = plex.fetchItem(int(rating_key))
        lib_title = item.section().title
        save_path = get_target_file_path(item, lib_title, img_type=img_type)
        if save_path:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            r = requests.get(img_url, stream=True)
            if r.status_code == 200:
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(1024): f.write(chunk)
                save_download_history(rating_key, img_url, img_type)
                flash(f"Saved {img_type}!")
            else: flash("Download failed.")
    except Exception as e: flash(f"Error: {e}")
    return redirect(request.referrer)

@app.route('/toggle_complete', methods=['POST'])
def toggle_complete():
    rating_key = request.form.get('rating_key')
    if rating_key:
        toggle_override_status(rating_key)
        flash("Status toggled.")
    return redirect(request.referrer)

if __name__ == '__main__':
    if not os.path.exists(CONFIG_FILE): save_config(DEFAULT_CONFIG)
    if not os.path.exists(DEFAULT_CONFIG['DOWNLOAD_BASE_DIR']): os.makedirs(DEFAULT_CONFIG['DOWNLOAD_BASE_DIR'])
    app.run(host='0.0.0.0', port=5000, debug=True)

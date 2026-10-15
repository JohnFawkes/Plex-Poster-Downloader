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
import ipaddress
import socket
import hashlib
import secrets
from PIL import Image
from urllib.parse import urlparse
from markupsafe import escape, Markup
# Try to import ZoneInfo for timezone support (Python 3.9+)
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback/Mock if missing (though standard in 3.11)
    ZoneInfo = None

from datetime import timedelta
from flask import Flask, render_template_string, request, redirect, flash, url_for, session, jsonify
from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized, PlexApiException
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
from artwork_common import Artwork, ArtworkContext

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
    'HIDE_UPLOADED_POSTERS': False,
    'TMDB_API_KEY': '',
    'TVDB_API_KEY': '',
    'TVDB_PIN': '',
    'FANART_API_KEY': '',
    'MAL_CLIENT_ID': '',
    'ANIDB_CLIENT': '',
    'ANIDB_CLIENT_VERSION': '1',
    'ANIME_LIBRARIES': [],
    'ANIME_AUTO_MAPPING': True,
    'ARTWORK_ID_OVERRIDES': {},
    'CRON_ENABLED': False,
    'CRON_TIME': '03:00',
    'CRON_DAY': 'DAILY',
    'CRON_TZ': 'Local', # New Timezone Setting
    'CRON_MODE': 'RANDOM',
    'CRON_PROVIDER': 'tmdb',
    'CRON_DOWNLOAD_BACKGROUNDS': False,
    'CRON_DOWNLOAD_SEASONS': False,
    'CRON_DOWNLOAD_TITLECARDS': False,
    'CRON_DOWNLOAD_CLEARLOGOS': False,
    'VERBOSE_LOGGING': False,
    'CRON_LIBRARIES': []
}

# --- Encryption Helpers ---
SECRET_CONFIG_KEYS = ('PLEX_TOKEN', 'TMDB_API_KEY', 'TVDB_API_KEY', 'TVDB_PIN', 'FANART_API_KEY', 'MAL_CLIENT_ID')

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
        return token

def get_config():
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            for key, val in DEFAULT_CONFIG.items():
                if key not in cfg:
                    cfg[key] = val
            
            for key in SECRET_CONFIG_KEYS:
                if cfg.get(key): cfg[key] = decrypt_val(cfg[key])
                
            return cfg
    except:
        return DEFAULT_CONFIG

def save_config(new_config):
    cfg_to_save = new_config.copy()
    for key in SECRET_CONFIG_KEYS:
        if cfg_to_save.get(key): cfg_to_save[key] = encrypt_val(cfg_to_save[key])

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
# Enable Jinja2 HTML autoescaping globally so {{ var }} expressions in every
# render_template_string call are safe from XSS without explicit |e filters.
app.jinja_env.autoescape = True

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

    custom_id = os.environ.get('PLEXAPI_HEADER_IDENTIFIER')
    if custom_id:
        import plexapi
        plexapi.X_PLEX_IDENTIFIER = custom_id

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


def artwork_badge(artwork):
    name = format_provider(getattr(artwork, 'provider', None))
    if getattr(artwork, 'origin', None) == 'direct': return artwork.provider
    if name == 'Upload': return 'upload_plex'
    return name.lower() + '_plex'

ARTWORK_ID_FIELDS = ('tmdb', 'tvdb', 'imdb', 'anidb', 'anilist', 'mal')
_artwork_cache = {}
_artwork_cache_lock = threading.RLock()

def valid_artwork_id(provider, value):
    pattern = r'tt\d{7,10}' if provider == 'imdb' else r'[1-9]\d{0,11}'
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None

def artwork_context(item, cfg):
    root = item.show() if item.type in ('season', 'episode') else item
    ids = {}
    aliases = {'themoviedb': 'tmdb', 'thetvdb': 'tvdb', 'myanimelist': 'mal'}
    for guid in list(getattr(root, 'guids', []) or []) + [getattr(root, 'guid', '')]:
        token = getattr(guid, 'id', guid)
        if not isinstance(token, str): continue
        provider, _, value = token.partition('://')
        provider = aliases.get(provider.rsplit('.', 1)[-1], provider.rsplit('.', 1)[-1])
        value = value.partition('?')[0]
        if provider in ARTWORK_ID_FIELDS and valid_artwork_id(provider, value): ids[provider] = value
    overrides = cfg.get('ARTWORK_ID_OVERRIDES', {})
    ids.update({k: v for k, v in overrides.get(str(root.ratingKey), {}).items()
                if k in ARTWORK_ID_FIELDS and valid_artwork_id(k, v)})
    if item.type in ('season', 'episode'):
        # Anime services use distinct entries per season; never inherit a whole-show anime ID.
        for provider in ('anidb', 'anilist', 'mal'): ids.pop(provider, None)
        ids.update({k: v for k, v in overrides.get(str(item.ratingKey), {}).items()
                    if k in ARTWORK_ID_FIELDS and valid_artwork_id(k, v)})
    return ArtworkContext(media_type=item.type, ids=ids,
                          season_number=item.index if item.type == 'season' else getattr(item, 'parentIndex', None) if item.type == 'episode' else None,
                          episode_number=item.index if item.type == 'episode' else None,
                          is_anime=root.section().title in cfg.get('ANIME_LIBRARIES', []))

def direct_artwork(item):
    cfg = get_config()
    context = artwork_context(item, cfg)
    if not context.is_anime and not any(cfg.get(k) for k in ('TMDB_API_KEY', 'TVDB_API_KEY', 'FANART_API_KEY')):
        return [], []
    cache_fields = SECRET_CONFIG_KEYS + ('ANIDB_CLIENT', 'ANIDB_CLIENT_VERSION', 'ANIME_AUTO_MAPPING')
    cache_data = (context.__dict__, {k: cfg.get(k) for k in cache_fields})
    key = hashlib.sha256(json.dumps(cache_data, sort_keys=True).encode()).hexdigest()
    with _artwork_cache_lock:
        cached = _artwork_cache.get(key)
        if cached and cached[0] > time.monotonic(): return cached[1], cached[2]
    from artwork_providers import fetch_artwork as fetch_standard
    from anime_providers import fetch_artwork as fetch_anime
    standard, notices = fetch_standard(context, cfg)
    anime, anime_notices = fetch_anime(context, cfg)
    images = standard + anime
    notices = notices + anime_notices
    with _artwork_cache_lock:
        if len(_artwork_cache) >= 128: _artwork_cache.pop(next(iter(_artwork_cache)))
        _artwork_cache[key] = (time.monotonic() + (60 if notices else 300), images, notices)
    return images, notices

def artwork_choices(item):
    choices = {'poster': [], 'background': [], 'clearlogo': []}
    notices = []
    methods = {'poster': 'posters'} if item.type == 'episode' else {'poster': 'posters', 'background': 'arts', 'clearlogo': 'logos'}
    for kind, method in methods.items():
        try: choices[kind] = list(getattr(item, method, lambda: [])())
        except (PlexApiException, requests.RequestException):
            notices.append(f'Unable to load Plex {kind} choices.')
    images, direct_notices = direct_artwork(item)
    for image in images:
        if image.asset_type in methods: choices[image.asset_type].append(image)
    if get_config().get('HIDE_UPLOADED_POSTERS', False):
        choices['poster'] = [p for p in choices['poster'] if format_provider(getattr(p, 'provider', None)) != 'Upload']
    return choices, notices + direct_notices

def render_artwork_picker(item, lib):
    choices, notices = artwork_choices(item)
    cfg = get_config()
    selected = {kind: get_history_url(item.ratingKey, kind) if check_file_exists(item, lib.title, kind) else None for kind in choices}
    kinds = [('poster', 'Titlecards')] if item.type == 'episode' else [('poster', 'Posters'), ('background', 'Backgrounds'), ('clearlogo', 'Clearlogos')]
    content = """
    {% for notice in notices %}<div class="flash">{{ notice }}</div>{% endfor %}
    <div class="path-info">Poster target: <strong>{{ target_path }}</strong></div>
    {% if item.type != 'episode' %}
    <details style="margin-bottom:20px;"><summary>Artwork IDs</summary>
        <p>Detected IDs: {{ context.ids }}. TVDB/TMDB IDs refer to the movie or parent series. Anime IDs refer to this exact show/movie or season entry. Blank overrides restore automatic matching.</p>
        <form method="post" action="{{ url_for('save_artwork_mapping', rating_key=item.ratingKey) }}">
            {% for provider in id_fields %}<div class="form-group"><label>{{ provider }} ID</label><input name="{{ provider }}" value="{{ overrides.get(provider, '') }}" autocomplete="off"></div>{% endfor %}
            <button type="submit" class="btn">Save Artwork IDs</button>
        </form>
    </details>
    {% endif %}
    <div class="tabs">
        {% for kind, label in kinds %}<button class="tab-btn{% if loop.first %} active{% endif %}" onclick="switchTab('tab-{{ kind }}')">{{ label }}</button>{% endfor %}
    </div>
    {% for kind, label in kinds %}
    <div id="tab-{{ kind }}" class="tab-content{% if loop.first %} active{% endif %}">
        <div class="{% if kind == 'poster' and item.type != 'episode' %}poster-grid{% else %}background-grid{% endif %}">
            {% for image in choices[kind] %}
            {% set image_url = get_poster_url(image) %}
            <form action="{{ url_for('download') }}" method="post" class="{% if kind == 'poster' and item.type != 'episode' %}poster-card{% else %}background-card{% endif %}{% if selected[kind] == image_url %} selected{% endif %}">
                <div class="img-container">
                    {% if selected[kind] == image_url %}<div class="selected-badge">CURRENT</div>{% endif %}
                    <img src="{{ image_url }}" loading="lazy" alt="{{ label }} for {{ item.title }}"{% if kind == 'clearlogo' %} style="object-fit:contain; background:#333;"{% endif %}>
                    <div class="provider-badge">{{ artwork_badge(image) }}</div>
                </div>
                <input type="hidden" name="poster_key" value="{{ image.key }}">
                <input type="hidden" name="rating_key" value="{{ item.ratingKey }}">
                <input type="hidden" name="img_type" value="{{ kind }}">
                <button type="submit" class="btn">Download{% if item.type == 'episode' %} Titlecard{% endif %}</button>
            </form>
            {% else %}<p>No {{ label|lower }} available. Uploaded artwork may be hidden in Settings; configure provider credentials or Artwork IDs for additional choices.</p>{% endfor %}
        </div>
    </div>
    {% endfor %}
    """
    return render_template_string(content, item=item, choices=choices, kinds=kinds, notices=notices,
                                  selected=selected, target_path=get_target_file_path(item, lib.title),
                                  id_fields=ARTWORK_ID_FIELDS, context=artwork_context(item, cfg),
                                  overrides=cfg.get('ARTWORK_ID_OVERRIDES', {}).get(str(item.ratingKey), {}),
                                  get_poster_url=get_poster_url, artwork_badge=artwork_badge)  # nosemgrep: render-template-string

def item_navigation(item, lib):
    previous_item = next_item = None
    navigation_error = False
    endpoint = 'view_item'
    order_label = 'Library title order'
    try:
        if item.type == 'season':
            items = sorted(item.show().seasons(), key=lambda s: s.index)
            endpoint, order_label = 'view_season', 'Season order'
        elif item.type == 'episode':
            items = sorted(item.season().episodes(), key=lambda e: e.index)
            endpoint, order_label = 'view_episode', 'Episode order within season'
        else:
            items = lib.search(sort='titleSort', libtype=item.type)
        for index, candidate in enumerate(items):
            if str(candidate.ratingKey) == str(item.ratingKey):
                if index > 0: previous_item = items[index - 1]
                if index + 1 < len(items): next_item = items[index + 1]
                break
    except (requests.RequestException, NotFound, Unauthorized) as exc:
        log_verbose(f"Unable to load item navigation: {exc}")
        navigation_error = True
    return render_template_string("""
    <nav aria-label="Item navigation" style="display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:10px; margin-bottom:20px;">
        {% if previous_item %}
            <a class="page-btn" href="{{ url_for(endpoint, rating_key=previous_item.ratingKey) }}" rel="prev" title="{{ previous_item.title }}">&larr; Previous</a>
        {% else %}
            <span class="page-btn" style="opacity:0.5; cursor:not-allowed;" aria-disabled="true">&larr; Previous</span>
        {% endif %}
        <span style="color:var(--text-muted);">{% if navigation_error %}Unable to load item navigation{% else %}{{ order_label }}{% endif %}</span>
        {% if next_item %}
            <a class="page-btn" href="{{ url_for(endpoint, rating_key=next_item.ratingKey) }}" rel="next" title="{{ next_item.title }}">Next &rarr;</a>
        {% else %}
            <span class="page-btn" style="opacity:0.5; cursor:not-allowed;" aria-disabled="true">Next &rarr;</span>
        {% endif %}
    </nav>
    """, previous_item=previous_item, next_item=next_item, navigation_error=navigation_error,
        endpoint=endpoint, order_label=order_label)  # nosemgrep: render-template-string

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
                return "Season " + str(item.index)
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
    if img_type not in ('poster', 'background', 'clearlogo'): return None
    extension = '.png' if img_type == 'clearlogo' else '.jpg'
    suffix = '' if img_type == 'poster' else f'_{img_type}'
    if item.type in ('movie', 'show'):
        folder = get_physical_folder_name(item)
        if current_style == 'NO_ASSET_FOLDERS':
            return os.path.join(base_dir, clean_lib, f'{folder}{suffix}{extension}')
        return os.path.join(base_dir, clean_lib, folder, f'{img_type}{extension}')
    if item.type not in ('season', 'episode'): return None
    folder = get_physical_folder_name(item.show())
    name = f'Season{item.index:02d}' if item.type == 'season' else f'S{item.parentIndex:02d}E{item.index:02d}'
    if current_style == 'NO_ASSET_FOLDERS':
        return os.path.join(base_dir, clean_lib, f'{folder}_{name}{suffix}{extension}')
    return os.path.join(base_dir, clean_lib, folder, f'{name}{suffix}{extension}')

def check_file_exists(item, lib_title=None, img_type='poster'):
    target_path = get_target_file_path(item, lib_title, img_type=img_type)
    if target_path:
        return os.path.exists(target_path)
    return False

def safe_html(value):
    """Escape a value for safe interpolation into HTML strings rendered via
    render_template_string.  Prevents both XSS (HTML special chars) and SSTI
    (Jinja2 delimiter injection) by escaping HTML entities and then replacing
    any remaining { / } characters so Jinja2 never sees them as template tags.
    Returns a Markup object so the value won't be double-escaped if Jinja2
    also processes the surrounding template.
    """
    text = str(escape(value))                        # HTML-escape < > & " '
    text = text.replace('{', '&#123;').replace('}', '&#125;')  # kill {{ }}
    return Markup(text)  # nosemgrep: explicit-unescape-with-markup


# Whitelist of routes that safe_referrer_redirect() is allowed to return to.
# Each entry is (compiled regex, flask endpoint name, url_for kwarg name or None).
# The redirect URL is always built with url_for() so no user-supplied bytes
# ever reach flask.redirect() directly (resolves CodeQL py/url-redirection).
_SAFE_REDIRECT_ROUTES = [
    (re.compile(r'^/library/(\d+)$'), 'view_library', 'lib_id'),
    (re.compile(r'^/item/(\d+)$'),    'view_item',    'rating_key'),
    (re.compile(r'^/season/(\d+)$'),  'view_season',  'rating_key'),
    (re.compile(r'^/episode/(\d+)$'), 'view_episode', 'rating_key'),
    (re.compile(r'^/settings$'),      'settings',     None),
    (re.compile(r'^/$'),              'home',          None),
]

def safe_referrer_redirect(fallback='home'):
    """Redirect to the referring page using a strict whitelist of known routes.

    Implements the "maintain a list of authorized redirects on the server"
    strategy from CodeQL's py/url-redirection recommendation.  The referrer
    is used only to *identify* which whitelisted route to return to; the
    actual redirect URL is always generated by url_for() so no user-supplied
    data ever reaches flask.redirect() directly.
    """
    referrer = request.referrer
    if referrer:
        parsed = urlparse(referrer)
        # Only consider referrers from the current host (or relative URLs).
        if not parsed.netloc or parsed.netloc == request.host:
            path = parsed.path or '/'
            for pattern, endpoint, key_name in _SAFE_REDIRECT_ROUTES:
                m = pattern.match(path)
                if m:
                    if key_name:
                        # int() ensures only a numeric value flows into url_for().
                        return redirect(url_for(endpoint, **{key_name: int(m.group(1))}))
                    return redirect(url_for(endpoint))
    return redirect(url_for(fallback))


def validate_image_url(url):
    """Validate a URL to prevent SSRF attacks.

    Allows http/https URLs that are either:
    - The configured Plex server host (may be localhost/private IP by design), or
    - A public (non-private, non-reserved) IP/hostname.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False

        # Resolve to IP for validation
        try:
            resolved_ip = socket.getaddrinfo(hostname, None)[0][4][0]
        except socket.gaierror:
            return False
        addr = ipaddress.ip_address(resolved_ip)

        # Always allow the configured Plex server host so poster downloads work
        # even when Plex lives on localhost or a private network address.
        cfg = get_config()
        plex_url = cfg.get('PLEX_URL', '')
        plex_host = urlparse(plex_url).hostname if plex_url else None
        if plex_host:
            try:
                plex_ip = socket.getaddrinfo(plex_host, None)[0][4][0]
                if resolved_ip == plex_ip:
                    return True
            except socket.gaierror:
                pass

        # For all other hosts, block private/loopback/reserved addresses.
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            return False

        return True
    except Exception:
        return False

def get_poster_url(poster):
    if isinstance(poster, Artwork): return poster.url
    key = getattr(poster, 'key', None)
    if not key: return ""
    if key.startswith('http') or key.startswith('https'): return key
    return plex.url(key)

def save_image_response(response, path, img_type):
    if response.status_code != 200: return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = None
    descriptor = None
    try:
        try:
            existing_mode = os.stat(path).st_mode & 0o777
        except FileNotFoundError:
            existing_mode = None
        candidate = os.path.join(os.path.dirname(path), '.artwork-' + secrets.token_hex(16))
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        temporary = candidate
        output = os.fdopen(descriptor, 'wb')
        descriptor = None
        with output:
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 30 * 1024 * 1024: raise ValueError('Image exceeds download size limit')
                output.write(chunk)
        if not size: raise ValueError('Empty image response')
        if img_type == 'clearlogo':
            with Image.open(temporary) as image:
                if image.width * image.height > 25000000: raise ValueError('Clearlogo dimensions are too large')
                normalized = image.convert('RGBA')
            normalized.save(temporary, format='PNG')
        if existing_mode is not None:
            os.chmod(temporary, existing_mode)
        os.replace(temporary, path)
        temporary = None
        return True
    finally:
        if descriptor is not None: os.close(descriptor)
        if temporary and os.path.exists(temporary): os.unlink(temporary)

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
    suffix = {'poster': '', 'background': '_bg', 'clearlogo': '_logo'}[img_type]
    key = f'{rating_key}{suffix}'
    data["downloads"][key] = img_url
    save_history_data(data)

def get_history_url(rating_key, img_type='poster'):
    data = load_history_data()
    suffix = {'poster': '', 'background': '_bg', 'clearlogo': '_logo'}[img_type]
    key = f'{rating_key}{suffix}'
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
        'clearlogo_count': 0,
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
    logo_count = 0
    total_size = 0
    
    if os.path.exists(lib_dir):
        for root, dirs, files in os.walk(lib_dir):
            for f in files:
                if f.lower().endswith('.jpg') or f.lower().endswith('.png'):
                    fp = os.path.join(root, f)
                    total_size += os.path.getsize(fp)
                    
                    # Detect backgrounds
                    if 'clearlogo' in f.lower():
                        logo_count += 1
                    elif 'background' in f.lower():
                        bg_count += 1
                    else:
                        file_count += 1
    
    stats['downloaded_count'] = file_count
    stats['bg_downloaded_count'] = bg_count
    stats['clearlogo_count'] = logo_count
    stats['disk_size'] = total_size
    stats['size_str'] = format_size(total_size)
    return stats

# ==========================================
# 6. CRON SCHEDULER (Threaded)
# ==========================================
def scheduled_items(items, cfg):
    for item in items:
        yield item
        if item.type != 'show': continue
        if not (cfg.get('CRON_DOWNLOAD_SEASONS', False) or cfg.get('CRON_DOWNLOAD_TITLECARDS', False)):
            continue
        try: seasons = item.seasons()
        except Exception as exc:
            log_verbose(f"Cron: Unable to load seasons for {item.title}: {exc}")
            continue
        for season in seasons:
            if cfg.get('CRON_DOWNLOAD_SEASONS', False): yield season
            if cfg.get('CRON_DOWNLOAD_TITLECARDS', False):
                try: episodes = season.episodes()
                except Exception as exc:
                    log_verbose(f"Cron: Unable to load episodes for {season.title}: {exc}")
                    continue
                yield from episodes

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
            
            for item in scheduled_items(items, cfg):
                tasks = [('poster', 'posters')]
                if dl_bgs and item.type != 'episode': tasks.append(('background', 'arts'))
                if cfg.get('CRON_DOWNLOAD_CLEARLOGOS', False) and item.type != 'episode': tasks.append(('clearlogo', 'logos'))
                all_choices = None
                
                for img_type, method in tasks:
                    if check_file_exists(item, lib.title, img_type):
                        skipped += 1
                        continue
                    
                    if all_choices is None:
                        try: all_choices, notices = artwork_choices(item)
                        except Exception:
                            log_verbose(f'Cron: Unable to load artwork for {item.title}')
                            break
                        for notice in notices: log_verbose(notice)
                    candidates = all_choices[img_type]
                    if not candidates: continue
                    
                    selected_img = None
                    valid = [p for p in candidates if format_provider(getattr(p, 'provider', None)) != 'Upload']
                    if not valid: continue

                    if mode == 'RANDOM':
                        selected_img = random.choice(valid)
                    elif mode in ['SPECIFIC_PROVIDER', 'RANDOM_PROVIDER']:
                        matching = [p for p in valid if target_provider in str(p.provider).lower()
                                    or target_provider == format_provider(p.provider).lower()
                                    or target_provider == artwork_badge(p)]
                        if matching:
                            if mode == 'SPECIFIC_PROVIDER': selected_img = matching[0]
                            else: selected_img = random.choice(matching)
                    
                    if selected_img:
                        try:
                            lib_title = item.section().title
                            save_path = get_target_file_path(item, lib_title, img_type=img_type)
                            if save_path:
                                url = get_poster_url(selected_img)

                                if not validate_image_url(url):
                                    log_verbose(f"Cron: Skipping {item.title} — URL failed SSRF validation")
                                    continue
                                log_verbose(f"Cron: Downloading {img_type} for {item.title} (Provider: {artwork_badge(selected_img)})")
                                r = requests.get(url, stream=True, timeout=(5, 30), allow_redirects=False)
                                try:
                                    if save_image_response(r, save_path, img_type):
                                        save_download_history(item.ratingKey, url, img_type)
                                        processed += 1
                                finally:
                                    close = getattr(r, 'close', None)
                                    if callable(close): close()
                        except Exception:
                            log_verbose(f"Cron Error saving {item.title}")
        log_verbose(f"Cron Finished. Downloaded: {processed}, Skipped: {skipped}")
    except Exception as e:
        log_verbose(f"Cron Job Failed: {e}")

def scheduler_loop():
    last_run_date = None
    while True:
        cfg = get_config()
        if cfg.get('CRON_ENABLED'):
            target_time = cfg.get('CRON_TIME', '03:00')
            target_day = cfg.get('CRON_DAY', 'DAILY')
            target_tz = cfg.get('CRON_TZ', 'Local')
            
            # Determine "Now" based on Timezone
            try:
                if target_tz and target_tz != 'Local' and ZoneInfo:
                    now = datetime.datetime.now(ZoneInfo(target_tz))
                else:
                    now = datetime.datetime.now()
            except Exception:
                # Fallback to local if TZ is invalid
                now = datetime.datetime.now()

            current_time = now.strftime("%H:%M")
            current_day_name = now.strftime("%A").upper()
            current_date = now.date()
            
            day_match = (target_day == 'DAILY') or (target_day == current_day_name)
            
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
    # Fix: Added 'settings' to the allow list to prevent redirect loops during setup
    if request.endpoint in ['static', 'login', 'setup', 'logout', 'settings']:
        return

    cfg = get_config()
    
    if cfg.get('AUTH_DISABLED', False):
        return
    
    if 'AUTH_USER' not in cfg or not cfg['AUTH_USER']:
        return redirect(url_for('settings'))
    
    if 'user' not in session:
        return redirect(url_for('login'))
    
    session.permanent = True

@app.errorhandler(404)
def page_not_found(e):
    # nosemgrep: render-template-string
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
    .poster-card img { width: 100%; height: auto; aspect-ratio: 2/3; object-fit: cover; display: block; border-radius: 8px; }
    .poster-card:hover { background: #333333; border-color: var(--accent); }
    .poster-card.selected { border-color: var(--accent); background: rgba(229, 160, 13, 0.1); }
    .background-card { background: var(--card); padding: 10px; border-radius: 12px; text-align: center; position: relative; border: 4px solid transparent; transition: 0.2s; }
    .background-card img { width: 100%; height: auto; aspect-ratio: 16/9; object-fit: cover; display: block; border-radius: 8px; }
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
    .path-info { font-size: 0.85em; color: var(--text-muted); margin-bottom: 15px; font-family: monospace; background: rgba(0,0,0,0.3); padding: 5px 10px; border-radius: 4px; display: inline-block; max-width:100%; box-sizing:border-box; overflow-wrap:anywhere; }
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
    .tabs { display: flex; flex-wrap:wrap; border-bottom: 1px solid var(--border-color); margin-bottom: 20px; }
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
    return render_template_string(HTML_LOGIN_SETUP, title="Setup Admin", subtitle="Create your admin account to secure access.", btn_text="Create Account", is_setup=True)  # nosemgrep: render-template-string

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
    return render_template_string(HTML_LOGIN_SETUP, title="Login", subtitle="Please sign in to continue.", btn_text="Sign In", is_setup=False)  # nosemgrep: render-template-string

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
        # Require authentication for all POST actions unless the system has no account yet.
        # create_account is additionally restricted to only work when unconfigured.
        if not is_unconfigured and not auth_disabled and 'user' not in session:
            return redirect(url_for('login'))

        action = request.form.get('action')
        if action == 'create_account' and not is_unconfigured:
            flash("An account already exists. Use change_password to update credentials.")
            return redirect(url_for('settings'))
        if action == 'update_config':
            cfg['PLEX_URL'] = request.form.get('plex_url', '').strip()
            # Handle Token Update: Only update if not the placeholder
            token_input = request.form.get('plex_token', '').strip()
            if token_input and token_input != '(Encrypted)':
                cfg['PLEX_TOKEN'] = token_input
            for key in SECRET_CONFIG_KEYS:
                if key == 'PLEX_TOKEN': continue
                field = key.lower()
                if request.form.get(f'clear_{field}') == 'on':
                    cfg[key] = ''
                elif request.form.get(field, '').strip():
                    cfg[key] = request.form[field].strip()
            cfg['ANIDB_CLIENT'] = request.form.get('anidb_client', cfg.get('ANIDB_CLIENT', '')).strip()
            version = request.form.get('anidb_client_version', cfg.get('ANIDB_CLIENT_VERSION', '1')).strip()
            if not re.fullmatch(r'[1-9]\d{0,3}', version):
                flash('AniDB client version must be a positive integer.')
                return redirect(url_for('settings'))
            cfg['ANIDB_CLIENT_VERSION'] = version
            cfg['ANIME_LIBRARIES'] = request.form.getlist('anime_libs')
            cfg['ANIME_AUTO_MAPPING'] = request.form.get('anime_auto_mapping') == 'on'
            
            cfg['DOWNLOAD_BASE_DIR'] = request.form.get('download_dir', 'downloaded_posters').strip()
            cfg['HISTORY_FILE'] = request.form.get('history_file', 'download_history.json').strip()
            cfg['ASSET_STYLE'] = request.form.get('asset_style', 'ASSET_FOLDERS')
            cfg['HIDE_UPLOADED_POSTERS'] = (request.form.get('hide_uploaded_posters') == 'on')
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
            
            cfg['CRON_TZ'] = request.form.get('cron_tz', 'Local').strip()

            cfg['CRON_MODE'] = request.form.get('cron_mode', 'RANDOM')
            cfg['CRON_PROVIDER'] = request.form.get('cron_provider', '').strip()
            cfg['CRON_DOWNLOAD_BACKGROUNDS'] = (request.form.get('cron_download_backgrounds') == 'on')
            cfg['CRON_DOWNLOAD_SEASONS'] = (request.form.get('cron_download_seasons') == 'on')
            cfg['CRON_DOWNLOAD_TITLECARDS'] = (request.form.get('cron_download_titlecards') == 'on')
            cfg['CRON_DOWNLOAD_CLEARLOGOS'] = (request.form.get('cron_download_clearlogos') == 'on')
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
    for key in SECRET_CONFIG_KEYS:
        if display_cfg.get(key): display_cfg[key] = '(Encrypted)'

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
                <h3>Direct Artwork Providers</h3>
                <p style="color:var(--text-muted);">Optional credentials are encrypted on disk. Blank fields keep existing values. Plex sources remain available without these keys.</p>
                {% for key, label in [('TMDB_API_KEY', 'TMDB API key or read access token'), ('TVDB_API_KEY', 'TVDB v4 API key'), ('TVDB_PIN', 'TVDB subscriber PIN (if required)'), ('FANART_API_KEY', 'fanart.tv API key'), ('MAL_CLIENT_ID', 'MyAnimeList Client ID')] %}
                <div class="form-group">
                    <label for="{{ key|lower }}">{{ label }}</label>
                    <input type="password" name="{{ key|lower }}" id="{{ key|lower }}" autocomplete="new-password" placeholder="{% if cfg[key] %}Configured — leave blank to keep{% else %}Not configured{% endif %}">
                    <div style="display:flex; gap:8px; align-items:center; margin-top:5px;">
                        <input type="checkbox" name="clear_{{ key|lower }}" id="clear_{{ key|lower }}" style="width:auto;">
                        <label for="clear_{{ key|lower }}" style="margin:0; font-weight:400;">Remove saved credential</label>
                    </div>
                </div>
                {% endfor %}
                <h3>Anime Artwork</h3>
                <p style="color:var(--text-muted);">AniList needs no key. MyAnimeList uses the Client ID above. AniDB requires your registered/approved HTTP API client; it is not an API key.</p>
                <div class="form-group"><label>AniDB client name</label><input name="anidb_client" value="{{ cfg.ANIDB_CLIENT }}"></div>
                <div class="form-group"><label>AniDB client version</label><input name="anidb_client_version" value="{{ cfg.ANIDB_CLIENT_VERSION }}" type="number" min="1" max="9999"></div>
                <div class="form-group">
                    <label>Anime Libraries</label>
                    {% for lib in all_libs %}
                    <div style="display:flex; gap:8px; align-items:center;">
                        <input type="checkbox" name="anime_libs" value="{{ lib.title }}" id="anime_lib_{{ loop.index }}" style="width:auto;" {% if lib.title in cfg.ANIME_LIBRARIES %}checked{% endif %}>
                        <label for="anime_lib_{{ loop.index }}" style="margin:0; font-weight:400;">{{ lib.title }}</label>
                    </div>
                    {% else %}<p>Connect Plex to select anime libraries.</p>{% endfor %}
                </div>
                <div class="form-group" style="display:flex; gap:8px; align-items:center;">
                    <input type="checkbox" name="anime_auto_mapping" id="anime_auto_mapping" style="width:auto;" {% if cfg.ANIME_AUTO_MAPPING %}checked{% endif %}>
                    <label for="anime_auto_mapping" style="margin:0; font-weight:400;">Use automatic anime ID mappings when unambiguous</label>
                </div>
                <p style="color:var(--text-muted);">Use Artwork IDs on a show or season to correct matches. Anime season IDs are separate from the TVDB/TMDB series ID; ambiguous matches are not guessed.</p>
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
                <div class="form-group">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <input type="checkbox" name="hide_uploaded_posters" id="hide_uploaded_posters" style="width:auto;" {% if cfg.HIDE_UPLOADED_POSTERS %}checked{% endif %}>
                        <label for="hide_uploaded_posters" style="margin:0; font-weight:400;">Hide uploaded posters</label>
                    </div>
                    <small style="color:var(--text-muted);">Hide choices labeled Upload for movies, shows, seasons, and episode titlecards. Backgrounds and existing files are unchanged.</small>
                </div>
                
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
                    <div class="form-group" style="display:flex; align-items:center; gap:10px;">
                        <input type="checkbox" name="cron_download_seasons" id="cron_download_seasons" style="width:auto;" {% if cfg.CRON_DOWNLOAD_SEASONS %}checked{% endif %}>
                        <label for="cron_download_seasons" style="margin:0;">Download Season Posters</label>
                    </div>
                    <div class="form-group" style="display:flex; align-items:center; gap:10px;">
                        <input type="checkbox" name="cron_download_titlecards" id="cron_download_titlecards" style="width:auto;" {% if cfg.CRON_DOWNLOAD_TITLECARDS %}checked{% endif %}>
                        <label for="cron_download_titlecards" style="margin:0;">Download Episode Titlecards</label>
                    </div>
                    <div class="form-group" style="display:flex; align-items:center; gap:10px;">
                        <input type="checkbox" name="cron_download_clearlogos" id="cron_download_clearlogos" style="width:auto;" {% if cfg.CRON_DOWNLOAD_CLEARLOGOS %}checked{% endif %}>
                        <label for="cron_download_clearlogos" style="margin:0;">Download Clearlogos</label>
                    </div>
                    <small style="color:var(--text-muted);">Child artwork is opt-in and uses the same provider selection. Existing files are skipped. Backgrounds also apply to enabled seasons, not episodes.</small>

                    <div class="form-group" style="display: flex; gap: 20px; flex-wrap: wrap;">
                        <div style="flex: 1; min-width: 150px;">
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
                        <div style="flex: 1; min-width: 220px;">
                            <label>Run At</label>
                            <div style="display:flex; gap:10px;">
                                <select name="cron_hour" style="flex:1; min-width: 60px;">
                                    {% for h in range(1, 13) %}
                                        <option value="{{ '%02d' % h }}" {% if ('%02d' % h) == c_hour %}selected{% endif %}>{{ h }}</option>
                                    {% endfor %}
                                </select>
                                <select name="cron_minute" style="flex:1; min-width: 60px;">
                                    {% for m in range(0, 60) %}
                                        <option value="{{ '%02d' % m }}" {% if ('%02d' % m) == c_minute %}selected{% endif %}>{{ '%02d' % m }}</option>
                                    {% endfor %}
                                </select>
                                <select name="cron_ampm" style="flex:1; min-width: 60px;">
                                    <option value="AM" {% if c_ampm == 'AM' %}selected{% endif %}>AM</option>
                                    <option value="PM" {% if c_ampm == 'PM' %}selected{% endif %}>PM</option>
                                </select>
                            </div>
                        </div>
                        <div style="flex: 1; min-width: 100px;">
                            <label>Timezone</label>
                            <input type="text" name="cron_tz" value="{{ cfg.CRON_TZ }}" placeholder="Local">
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
                            {% for provider in ['anidb', 'anilist', 'mal', 'tmdb_plex', 'tvdb_plex', 'fanart.tv_plex'] %}
                            <option value="{{ provider }}" {% if cfg.CRON_PROVIDER == provider %}selected{% endif %}>{{ provider }}</option>
                            {% endfor %}
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
    return render_template_string(HTML_TOP + content + HTML_BOTTOM, title="Settings", cfg=display_cfg, all_libs=all_libs, c_hour=c_hour, c_minute=c_minute, c_ampm=c_ampm, breadcrumbs=[('Settings', '#')], toggle_override=False, is_unconfigured=is_unconfigured, auth_disabled=auth_disabled)  # nosemgrep: render-template-string

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

    # Use a pure Jinja2 template so all item data is rendered via {{ }} with
    # autoescape — no user-derived content is concatenated into the template
    # string itself (eliminates CodeQL SSTI and XSS findings).
    LIBRARY_TPL = """
<div class="pagination" style="margin:30px 0;border-top:1px solid #444;padding-top:20px">
  <div style="display:flex;align-items:center;justify-content:center;gap:15px">
    {% if page > 1 %}<a href="?page={{ page - 1 }}" class="page-btn">&laquo; Prev</a>
    {% else %}<span class="page-btn" style="opacity:0.5;cursor:not-allowed">&laquo; Prev</span>{% endif %}
    <form action="" method="get" style="display:flex;align-items:center;gap:10px;margin:0">
      <label style="margin:0;color:var(--text-muted)">Page</label>
      <select name="page" onchange="this.form.submit()" style="padding:8px;border-radius:6px;background:var(--bg);color:var(--text);border:1px solid #4b5563;cursor:pointer;min-width:80px">
        {% for p in range(1, total_pages + 1) %}<option value="{{ p }}"{% if p == page %} selected{% endif %}>{{ p }}</option>{% endfor %}
      </select>
      <span style="color:var(--text-muted)">of {{ total_pages }}</span>
    </form>
    {% if page < total_pages %}<a href="?page={{ page + 1 }}" class="page-btn">Next &raquo;</a>
    {% else %}<span class="page-btn" style="opacity:0.5;cursor:not-allowed">Next &raquo;</span>{% endif %}
  </div>
</div>
{% if todo_items %}
<div class="section-header"><h2>Missing Posters</h2><span>{{ todo_items|length }} on page</span></div>
<div class="grid">{% for i in todo_items %}<a href="/item/{{ i.ratingKey }}" class="card"><img src="{{ i.thumbUrl }}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'"><div class="title">{{ i.title }}</div></a>{% endfor %}</div>
{% endif %}
{% if partial_items %}
<div class="section-header"><h2 style="color:var(--warning)">Half Missing</h2><span>{{ partial_items|length }} on page</span></div>
<div class="grid">{% for i in partial_items %}<a href="/item/{{ i.ratingKey }}" class="card" style="border:2px solid var(--warning)"><img src="{{ i.thumbUrl }}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'"><div class="title">{{ i.title }}</div></a>{% endfor %}</div>
{% endif %}
{% if done_items_list %}
<div class="section-header"><h2 style="color:var(--accent)">Already Downloaded</h2><span>{{ done_items_list|length }} total</span></div>
<div class="grid">{% for i in done_items_list %}<a href="/item/{{ i.ratingKey }}" class="card" style="opacity:0.7"><img src="{{ i.thumbUrl }}" loading="lazy" onerror="this.src='https://via.placeholder.com/200x300?text=No+Img'"><div class="title">{{ i.title }}</div></a>{% endfor %}</div>
{% endif %}
{% if not todo_items and not partial_items and not done_items_list %}<p>No items found.</p>{% endif %}
<div class="pagination" style="margin:30px 0;border-top:1px solid #444;padding-top:20px">
  <div style="display:flex;align-items:center;justify-content:center;gap:15px">
    {% if page > 1 %}<a href="?page={{ page - 1 }}" class="page-btn">&laquo; Prev</a>
    {% else %}<span class="page-btn" style="opacity:0.5;cursor:not-allowed">&laquo; Prev</span>{% endif %}
    <form action="" method="get" style="display:flex;align-items:center;gap:10px;margin:0">
      <label style="margin:0;color:var(--text-muted)">Page</label>
      <select name="page" onchange="this.form.submit()" style="padding:8px;border-radius:6px;background:var(--bg);color:var(--text);border:1px solid #4b5563;cursor:pointer;min-width:80px">
        {% for p in range(1, total_pages + 1) %}<option value="{{ p }}"{% if p == page %} selected{% endif %}>{{ p }}</option>{% endfor %}
      </select>
      <span style="color:var(--text-muted)">of {{ total_pages }}</span>
    </form>
    {% if page < total_pages %}<a href="?page={{ page + 1 }}" class="page-btn">Next &raquo;</a>
    {% else %}<span class="page-btn" style="opacity:0.5;cursor:not-allowed">Next &raquo;</span>{% endif %}
  </div>
</div>
"""
    return render_template_string(HTML_TOP + LIBRARY_TPL + HTML_BOTTOM,  # nosemgrep: render-template-string
                                  title=lib.title, breadcrumbs=[(lib.title, '#')],
                                  toggle_override=False,
                                  todo_items=todo_items, partial_items=partial_items,
                                  done_items_list=done_items_list,
                                  page=page, total_pages=total_pages)

@app.route('/item/<rating_key>')
def view_item(rating_key):
    if not plex: return redirect(url_for('settings'))
    try: item = plex.fetchItem(int(rating_key))
    except: return "Not Found", 404
    
    is_show = item.type == 'show'
    lib = item.section()
    seasons = item.seasons() if is_show else []
    content = item_navigation(item, lib) + render_artwork_picker(item, lib)

    if is_show:
        content += f"""<div class="section-header"><h2>Seasons</h2></div><div class="grid">"""
        for s in seasons:
            thumb = s.thumbUrl if s.thumb else ''
            content += f"""<a href="/season/{s.ratingKey}" class="card"><img src="{safe_html(thumb)}" loading="lazy"><div class="title">{safe_html(s.title)}</div></a>"""
        content += "</div>"
        
    return render_template_string(HTML_TOP + "{{ page_content }}" + HTML_BOTTOM, page_content=Markup(content), title=item.title, breadcrumbs=[(lib.title, f'/library/{lib.key}'), (item.title, '#')],  # nosemgrep: render-template-string,explicit-unescape-with-markup
                                  rating_key=item.ratingKey, toggle_override=is_show, is_overridden=is_overridden(item.ratingKey))

@app.route('/season/<rating_key>')
def view_season(rating_key):
    if not plex: return redirect(url_for('settings'))
    try: season = plex.fetchItem(int(rating_key))
    except (ValueError, NotFound): return "Not Found", 404
    if season.type != 'season': return "Not Found", 404
    show = season.show()
    lib = show.section()
    content = item_navigation(season, lib) + render_artwork_picker(season, lib)
    
    content += render_template_string("""
    <div class="section-header"><h2>Episode Titlecards</h2></div>
    <div class="background-grid">
        {% for episode in episodes %}
        <a href="{{ url_for('view_episode', rating_key=episode.ratingKey) }}" class="background-card">
            {% if episode.thumb %}<img src="{{ episode.thumbUrl }}" loading="lazy" alt="{{ episode.title }}">{% endif %}
            <div class="title">E{{ '%02d' % episode.index }} &mdash; {{ episode.title }}</div>
        </a>
        {% else %}<p>No episodes found.</p>{% endfor %}
    </div>
    """, episodes=sorted(season.episodes(), key=lambda e: e.index))  # nosemgrep: render-template-string
    return render_template_string(HTML_TOP + "{{ page_content }}" + HTML_BOTTOM, page_content=Markup(content), title=f"{show.title} - {season.title}", breadcrumbs=[(lib.title, f'/library/{lib.key}'), (show.title, f'/item/{show.ratingKey}'), (season.title, '#')], toggle_override=False)  # nosemgrep: render-template-string,explicit-unescape-with-markup

@app.route('/episode/<rating_key>')
def view_episode(rating_key):
    if not plex: return redirect(url_for('settings'))
    try: episode = plex.fetchItem(int(rating_key))
    except (ValueError, NotFound): return "Not Found", 404
    if episode.type != 'episode': return "Not Found", 404
    show = episode.show()
    season = episode.season()
    lib = show.section()
    content = item_navigation(episode, lib) + render_artwork_picker(episode, lib)
    return render_template_string(HTML_TOP + '{{ page_content }}' + HTML_BOTTOM,
                                  title=f'{show.title} - S{episode.parentIndex:02d}E{episode.index:02d} - {episode.title}',
                                  breadcrumbs=[(lib.title, f'/library/{lib.key}'), (show.title, f'/item/{show.ratingKey}'),
                                               (season.title, f'/season/{season.ratingKey}'), (episode.title, '#')],
                                  toggle_override=False, page_content=Markup(content))  # nosemgrep: render-template-string,explicit-unescape-with-markup

@app.route('/artwork-mapping/<rating_key>', methods=['POST'])
def save_artwork_mapping(rating_key):
    if not plex: return redirect(url_for('settings'))
    try: item = plex.fetchItem(int(rating_key))
    except (ValueError, NotFound): return 'Not Found', 404
    if item.type not in ('movie', 'show', 'season'): return 'Invalid mapping target', 400
    mapping = {}
    for provider in ARTWORK_ID_FIELDS:
        value = request.form.get(provider, '')
        if not value: continue
        if not valid_artwork_id(provider, value): return f'Invalid {provider} ID', 400
        mapping[provider] = value
    cfg = get_config()
    overrides = dict(cfg.get('ARTWORK_ID_OVERRIDES', {}))
    if mapping: overrides[str(item.ratingKey)] = mapping
    else: overrides.pop(str(item.ratingKey), None)
    cfg['ARTWORK_ID_OVERRIDES'] = overrides
    save_config(cfg)
    flash('Artwork ID overrides saved.')
    return redirect(url_for('view_season' if item.type == 'season' else 'view_item', rating_key=item.ratingKey))

@app.route('/download', methods=['POST'])
def download():
    if not plex: return redirect(url_for('settings'))
    rating_key   = request.form.get('rating_key')
    img_type     = request.form.get('img_type', 'poster')
    if img_type not in ('poster', 'background', 'clearlogo'): return 'Invalid image type', 400
    poster_key   = request.form.get('poster_key', '')
    if not poster_key:
        flash("Missing poster key.")
        return safe_referrer_redirect()
    try:
        item = plex.fetchItem(int(rating_key))
        # Whitelist approach: fetch sources from the Plex API and find the one
        # whose key matches the submitted value.  img_url is derived from the
        # matched Plex API object (not from user input), breaking the SSRF
        # taint chain that CodeQL would otherwise flag.
        choices, _ = artwork_choices(item)
        sources = choices[img_type]
        matched = next((s for s in sources if getattr(s, 'key', None) == poster_key), None)
        if matched is None:
            flash("Poster not found.")
            return safe_referrer_redirect()
        img_url = get_poster_url(matched)
        if not img_url or not validate_image_url(img_url):
            flash("Blocked: image URL failed security validation.")
            return safe_referrer_redirect()
        lib_title = item.section().title
        save_path = get_target_file_path(item, lib_title, img_type=img_type)
        if save_path:
            r = requests.get(img_url, stream=True, timeout=(5, 30), allow_redirects=False)
            try:
                if save_image_response(r, save_path, img_type):
                    save_download_history(rating_key, img_url, img_type)
                    flash(f"Saved {img_type}!")
                else: flash("Download failed.")
            finally:
                close = getattr(r, 'close', None)
                if callable(close): close()
    except Exception:
        flash('Unable to download artwork. Check the source and provider settings.')
    return safe_referrer_redirect()

@app.route('/toggle_complete', methods=['POST'])
def toggle_complete():
    rating_key = request.form.get('rating_key')
    if rating_key:
        toggle_override_status(rating_key)
        flash("Status toggled.")
    return safe_referrer_redirect()

if __name__ == '__main__':
    if not os.path.exists(CONFIG_FILE): save_config(DEFAULT_CONFIG)
    if not os.path.exists(DEFAULT_CONFIG['DOWNLOAD_BASE_DIR']): os.makedirs(DEFAULT_CONFIG['DOWNLOAD_BASE_DIR'])
    _host  = os.environ.get('FLASK_RUN_HOST', '0.0.0.0')
    _port  = int(os.environ.get('FLASK_RUN_PORT', '5000'))
    _debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host=_host, port=_port, debug=_debug)

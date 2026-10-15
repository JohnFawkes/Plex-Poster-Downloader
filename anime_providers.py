"""Official anime poster adapters and conservative per-entry ID resolution.

References:
https://wiki.anidb.net/HTTP_API_Definition
https://docs.anilist.co/guide/graphql/
https://docs.anilist.co/guide/rate-limiting
https://docs.anilist.co/guide/terms-of-use
https://myanimelist.net/apiconfig/references/api/v2
https://myanimelist.net/static/apiagreement.html
https://github.com/Anime-Lists/anime-lists#format
https://github.com/Fribb/anime-lists
https://github.com/cedya77/anime-offline-database
https://github.com/manami-project/anime-offline-database

The ID bridge derives from Fribb's reduction of the anime-offline-database
continuation; credit Fribb, cedya77 and manami-project when redistributing
mapping data, and preserve its ODbL/DbCL terms. Artwork rights are separate.
Cache only requested public metadata: never crawl the anime APIs.

Anime services describe individual entries, not Plex franchises. A season's
anime IDs must describe that exact item, never be inherited from its show.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from collections import OrderedDict
import xml.etree.ElementTree as ET

import requests

from artwork_common import Artwork, ProviderError

ANILIST_URL = 'https://graphql.anilist.co'
TIMEOUT = (5, 20)
METADATA_LIMIT = 1024 * 1024
MAPPING_LIMIT = 16 * 1024 * 1024
_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST = {}
_COOLDOWNS = {}


def _cached(config, key, ttl, loader, failure_ttl=60):
    """Cache public metadata only; credential-dependent keys are opaque hashes.

    AniDB successes live for seven days, failures for one day. Disk caching
    survives normal app restarts; a read-only DATA_DIR falls back to memory.
    """
    directory = Path(config.get('DATA_DIR') or os.environ.get('DATA_DIR', '.')) / '.anime-provider-cache'
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    path = directory / (digest + '.json')
    cache_key = str(path.absolute())
    with _CACHE_LOCK:
        record = _CACHE.get(cache_key)
        if record is None:
            try:
                if path.stat().st_size <= MAPPING_LIMIT:
                    record = json.loads(path.read_bytes())
            except (OSError, ValueError, TypeError, RecursionError):
                record = None
        if not isinstance(record, dict) or not isinstance(record.get('expires'), (int, float)) \
                or record['expires'] <= time.time():
            try:
                record = {'value': loader(), 'expires': time.time() + ttl}
            except ProviderError as error:
                record = {'error': str(error), 'expires': time.time() + failure_ttl}
            try:
                directory.mkdir(parents=True, exist_ok=True)
                temporary = directory / (digest + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_text(json.dumps(record), encoding='utf-8')
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
        _CACHE[cache_key] = record
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > 256:
            _CACHE.popitem(last=False)
        if record.get('error'):
            raise ProviderError(record['error'])
        return record.get('value')


def _pace(provider):
    # AniList currently advertises 30/min, not its normal 90/min. Avoid bursts.
    interval = {'AniDB': 2.1, 'AniList': 2.1, 'MyAnimeList': 1.0}.get(provider, 0)
    if not interval:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        if _COOLDOWNS.get(provider, 0) > now:
            raise ProviderError(f'{provider}: rate limited or temporarily blocked; retry later.')
        wait = _NEXT_REQUEST.get(provider, 0) - now
        if wait > 0:
            time.sleep(wait)
        _NEXT_REQUEST[provider] = time.monotonic() + interval


def _cooldown(provider, seconds):
    with _RATE_LOCK:
        _COOLDOWNS[provider] = max(_COOLDOWNS.get(provider, 0), time.monotonic() + seconds)


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return text if re.fullmatch(r'[1-9][0-9]{0,9}', text) else None


def _request(session, method, url, provider, limit=METADATA_LIMIT, **kwargs):
    """Read bounded responses; never expose exception text, URLs or bodies."""
    response = None
    try:
        _pace(provider)
        response = getattr(session, method)(url, timeout=TIMEOUT,
                                            allow_redirects=False, stream=True, **kwargs)
        if response.status_code == 429:
            retry = response.headers.get('Retry-After', '60')
            seconds = int(retry) if str(retry).isdigit() else 60
            _cooldown(provider, max(1, min(seconds, 86400)))
            raise ProviderError(f'{provider}: rate limited; retry later.')
        if response.headers.get('X-RateLimit-Remaining') == '0':
            reset = response.headers.get('X-RateLimit-Reset')
            seconds = max(1, min(int(reset) - time.time(), 86400)) if str(reset).isdigit() else 60
            _cooldown(provider, seconds)
        if response.status_code != 200:
            raise ProviderError(f'{provider}: metadata request failed; retry later.')
        length = response.headers.get('Content-Length')
        if length and int(length) > limit:
            raise ProviderError(f'{provider}: metadata response exceeds the size limit.')
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > limit:
                raise ProviderError(f'{provider}: metadata response exceeds the size limit.')
        return bytes(body)
    except (requests.RequestException, ValueError, TypeError, OSError):
        raise ProviderError(f'{provider}: metadata unavailable; retry later.') from None
    finally:
        if response is not None:
            response.close()


def _json_request(session, method, url, provider, **kwargs):
    try:
        value = json.loads(_request(session, method, url, provider, **kwargs))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, RecursionError):
        raise ProviderError(f'{provider}: invalid metadata response.') from None


def _anilist(entry_id, session, config, *, by_mal=False):
    selector = 'idMal' if by_mal else 'id'
    media = _cached(config, ['anilist', selector, entry_id], 3600,
                    lambda: _anilist_uncached(entry_id, session, by_mal=by_mal))
    if by_mal and _positive_id(media.get('id')):
        _cached(config, ['anilist', 'id', str(media['id'])], 3600, lambda: media)
    return media


def _anilist_uncached(entry_id, session, *, by_mal=False):
    selector = 'idMal' if by_mal else 'id'
    query = ('query ($' + selector + ': Int) { Media(' + selector + ': $' + selector +
             ', type: ANIME) { id idMal coverImage { extraLarge large medium } } }')
    result = _json_request(session, 'post', ANILIST_URL, 'AniList',
                           json={'query': query, 'variables': {selector: int(entry_id)}})
    if result.get('errors'):
        raise ProviderError('AniList: metadata unavailable; retry later.')
    data = result.get('data')
    media = data.get('Media') if isinstance(data, dict) else None
    if not isinstance(media, dict) or _positive_id(media.get(selector)) != entry_id \
            or not _positive_id(media.get('id')):
        raise ProviderError('AniList: exact anime entry not found; set ID overrides.')
    return {key: media.get(key) for key in ('id', 'idMal', 'coverImage')}


def _xml(body, provider):
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ProviderError(f'{provider}: invalid XML metadata.')
    try:
        return ET.fromstring(body)
    except (ET.ParseError, ValueError, RecursionError):
        raise ProviderError(f'{provider}: invalid XML metadata.') from None


def _mal(entry_id, config, session):
    client_id = config.get('MAL_CLIENT_ID')
    if not isinstance(client_id, str) or not client_id or '\n' in client_id or '\r' in client_id:
        raise ProviderError('MyAnimeList: configure MAL_CLIENT_ID from a registered API application.')
    result = _json_request(session, 'get', f'https://api.myanimelist.net/v2/anime/{entry_id}',
                           'MyAnimeList', headers={'X-MAL-CLIENT-ID': client_id},
                           params={'fields': 'id,main_picture'})
    if _positive_id(result.get('id')) != entry_id:
        raise ProviderError('MyAnimeList: exact anime entry not found; set ID overrides.')
    return result


def _anidb(entry_id, config, session):
    client, version = config.get('ANIDB_CLIENT'), _positive_id(config.get('ANIDB_CLIENT_VERSION'))
    if not isinstance(client, str) or not re.fullmatch(r'[a-z0-9]+', client) or not version:
        raise ProviderError('AniDB: configure a registered HTTP API ANIDB_CLIENT and '
                            'ANIDB_CLIENT_VERSION; your account name is not a client identifier.')
    key = ['anidb', entry_id, client, version]
    return _cached(config, key, 7 * 86400,
                   lambda: _anidb_uncached(entry_id, client, version, session), failure_ttl=86400)


def _anidb_uncached(entry_id, client, version, session):
    # AniDB documents this HTTP-only endpoint, not HTTPS on port 9001.
    body = _request(session, 'get', 'http://api.anidb.net:9001/httpapi', 'AniDB',
                    params={'request': 'anime', 'client': client, 'clientver': int(version),
                            'protover': 1, 'aid': int(entry_id)})
    root = _xml(body, 'AniDB')
    if root.tag != 'anime' or _positive_id(root.get('id')) != entry_id:
        if root.tag == 'error':
            # Do not probe other IDs after a ban or rejected client response.
            _cooldown('AniDB', 86400)
        raise ProviderError('AniDB: entry unavailable or client not approved; check registration and retry later.')
    picture = root.findtext('picture', default='')
    if not re.fullmatch(r'[0-9]+\.(?:jpg|jpeg|png|webp)', picture):
        raise ProviderError('AniDB: no valid poster for this anime entry.')
    return {'picture': picture}


def _cover(media, provider):
    if provider == 'anidb':
        return Artwork('https://cdn.anidb.net/images/main/' + media['picture'], provider)
    picture = media.get('main_picture' if provider == 'mal' else 'coverImage')
    if isinstance(picture, dict):
        for name in ('extraLarge', 'large', 'medium'):
            if picture.get(name):
                return Artwork(picture[name], provider)
    raise ProviderError(f'{provider}: no poster for this anime entry.')


XML_MAPPING_URL = 'https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list.xml'
ID_BRIDGE_URL = ('https://raw.githubusercontent.com/Fribb/anime-lists/master/'
                 'anime-offline-database-reduced.json')
_OVERRIDE_NOTICE = 'Anime mapping is missing or ambiguous; set ID overrides for this exact item.'


def _auto_mapping(config):
    return config.get('ANIME_AUTO_MAPPING', True) not in (False, None, 'false', 'False', '0')


def _crosswalk(config, session):
    def load():
        root = _xml(_request(session, 'get', XML_MAPPING_URL, 'Anime-Lists',
                             limit=MAPPING_LIMIT), 'Anime-Lists')
        if root.tag != 'anime-list' or len(root) > 100000:
            raise ProviderError('Anime-Lists: invalid mapping data; set ID overrides.')
        return [dict(node.attrib, mappings=[dict(m.attrib, text=m.text or '')
                                          for m in node.findall('mapping-list/mapping')])
                for node in root.findall('anime') if _positive_id(node.get('anidbid'))]
    return _cached(config, ['anime-lists-xml', 1], 86400, load)


def _bridge(config, session):
    def load():
        try:
            data = json.loads(_request(session, 'get', ID_BRIDGE_URL, 'Anime ID crosswalk',
                                       limit=MAPPING_LIMIT))
            if not isinstance(data, list) or len(data) > 100000:
                raise ValueError()
            rows = []
            for row in data:
                if not isinstance(row, dict):
                    raise ValueError()
                ids = {key: _positive_id(row.get(field)) for key, field in
                       (('anidb', 'anidb_id'), ('anilist', 'anilist_id'), ('mal', 'mal_id'))}
                rows.append({key: value for key, value in ids.items() if value})
            return rows
        except (ValueError, TypeError, RecursionError):
            raise ProviderError('Anime ID crosswalk: invalid mapping data; set ID overrides.') from None
    return _cached(config, ['anime-entry-bridge', 1], 86400, load)


def _mapped_aid(rows, ids, season_number, media_type):
    candidates, absolute = set(), False
    target = str(season_number) if season_number is not None else None
    for row in rows:
        aid = row['anidbid']
        if media_type == 'movie':
            if ((_positive_id(ids.get('tmdb')) and str(ids['tmdb']) in row.get('tmdbid', '').split(','))
                    or (ids.get('imdb') and ids['imdb'] in row.get('imdbid', '').split(','))):
                candidates.add(aid)
            continue
        for namespace, idfield, seasonfield in (('tvdb', 'tvdbid', 'defaulttvdbseason'),
                                                ('tmdb', 'tmdbtv', 'tmdbseason')):
            show_id = _positive_id(ids.get(namespace))
            if not show_id or row.get(idfield) != show_id:
                continue
            default = row.get(seasonfield)
            if default == 'a':
                absolute = True
                continue
            if target is None or default == target:
                candidates.add(aid)
            for mapping in row['mappings']:
                # An individual target episode 0 means 'not mapped', not S00E00.
                has_episodes = ('start' in mapping and 'end' in mapping) or bool(
                    re.search(r'-[1-9][0-9]*(?:[+;]|$)', mapping.get('text', '')))
                if mapping.get(namespace + 'season') == target and has_episodes:
                    candidates.add(aid)
    if absolute or len(candidates) != 1:
        raise ProviderError(_OVERRIDE_NOTICE)
    return next(iter(candidates))


def resolve_anime_ids(ids, season_number, config, session=None, *, media_type=None):
    """Resolve exact anime IDs; callers retain ownership of supplied sessions.

    Pass media_type='movie' for TMDB movie IDs. Without it, IDs are TV-show
    identifiers and season_number selects the provider's own season order.
    """
    if session is not None:
        return _resolve_anime_ids(ids, season_number, config, session, media_type=media_type)
    client = requests.Session()
    try:
        return _resolve_anime_ids(ids, season_number, config, client, media_type=media_type)
    finally:
        client.close()


def _resolve_anime_ids(ids, season_number, config, session, *, media_type=None):
    """Resolve exact entries without guessing franchise order or split cours.

    TVDB and TMDB season numbering are kept separate. With season_number=None,
    media_type='movie' is required to interpret a TMDB ID as a movie; otherwise
    it is a TV ID. IMDb is only mapped for movies: anime-lists documents IMDb
    IDs for standalone entries, not an IMDb-show+season crosswalk.

    Existing anime keys (even malformed overrides) are never replaced. Explicit
    exact-item IDs take precedence over parent show TVDB/TMDB relationships.
    """
    result, notices = dict(ids), []
    if not _auto_mapping(config):
        return result, notices
    for key in ('anidb', 'anilist', 'mal'):
        if key in ids and not _positive_id(ids[key]):
            return result, ['Invalid anime ID; set ID overrides for this exact item.']
    if all(key in result for key in ('anidb', 'anilist', 'mal')):
        return result, notices
    if season_number is not None and (isinstance(season_number, bool)
            or not re.fullmatch(r'0|[1-9][0-9]{0,3}', str(season_number))):
        return result, [_OVERRIDE_NOTICE]
    client = session
    anchors = {key: _positive_id(result.get(key)) for key in ('anidb', 'anilist', 'mal')
               if _positive_id(result.get(key))}
    try:
        if 'anilist' in anchors and 'mal' not in result or 'mal' in anchors and 'anilist' not in result:
            try:
                source = 'anilist' if 'anilist' in anchors else 'mal'
                media = _anilist(anchors[source], client, config, by_mal=source == 'mal')
                for key, field in (('anilist', 'id'), ('mal', 'idMal')):
                    value = _positive_id(media.get(field))
                    if key not in result and value:
                        result[key] = value
                        anchors[key] = value
            except ProviderError as error:
                notices.append(str(error))
        if not anchors:
            if media_type == 'season' and season_number is None:
                return result, [_OVERRIDE_NOTICE]
            external = any(_positive_id(ids.get(key)) for key in ('tvdb', 'tmdb'))
            movie_imdb = media_type == 'movie' and isinstance(ids.get('imdb'), str) and bool(
                re.fullmatch(r'tt[0-9]{7,9}', ids['imdb']))
            if not external and not movie_imdb:
                return result, [_OVERRIDE_NOTICE]
            result['anidb'] = _mapped_aid(_crosswalk(config, client), ids, season_number, media_type)
            anchors = {'anidb': result['anidb']}
        rows = [row for row in _bridge(config, client)
                if all(row.get(key) in (None, value) for key, value in anchors.items())
                and any(row.get(key) == value for key, value in anchors.items())]
        if len(rows) != 1:
            notices.append(_OVERRIDE_NOTICE)
        else:
            for key, value in rows[0].items():
                if key not in result:
                    result[key] = value
    except ProviderError as error:
        notices.append(str(error))
    return result, notices


def fetch_artwork(context, config, session=None):
    """Return exact-entry posters; never convert anime covers to titlecards."""
    if not context.is_anime or context.media_type not in ('movie', 'show', 'season'):
        return [], []
    if session is not None:
        return _fetch_artwork(context, config, session)
    client = requests.Session()
    try:
        return _fetch_artwork(context, config, client)
    finally:
        client.close()


def _fetch_artwork(context, config, session):
    """Return anime entry posters and safe, credential-free notices."""
    client = session
    ids, notices = resolve_anime_ids(context.ids, context.season_number, config, client,
                                    media_type=context.media_type)
    artwork = []
    for provider in ('anilist', 'mal', 'anidb'):
        entry_id = _positive_id(ids.get(provider))
        if not entry_id:
            if provider in ids:
                notices.append(f'{provider}: invalid anime ID; set ID overrides for this exact item.')
            continue
        try:
            if provider == 'anilist':
                media = _anilist(entry_id, client, config)
                official_mal = _positive_id(media.get('idMal'))
                if official_mal and _positive_id(ids.get('mal')) \
                        and official_mal != _positive_id(ids['mal']):
                    # Do not let a stale/merged crosswalk choose another entry's
                    # cover. Independent explicit overrides remain authoritative.
                    if 'mal' not in context.ids:
                        ids.pop('mal', None)
                    if 'anilist' not in context.ids:
                        raise ProviderError(_OVERRIDE_NOTICE)
                    notices.append(_OVERRIDE_NOTICE)
            elif provider == 'mal':
                media = _mal(entry_id, config, client)
            else:
                media = _anidb(entry_id, config, client)
            artwork.append(_cover(media, provider))
        except ProviderError as error:
            notices.append(str(error))
        except (ValueError, TypeError, AttributeError):
            notices.append(f'{provider}: invalid poster metadata.')
    return artwork, notices

"""Direct artwork providers, isolated from Flask and Plex state.

Official contracts:
https://developer.themoviedb.org/reference/movie-images
https://developer.themoviedb.org/reference/tv-season-images
https://developer.themoviedb.org/reference/tv-episode-images
https://developer.themoviedb.org/reference/find-by-id
https://developer.themoviedb.org/reference/tv-series-external-ids
https://developer.themoviedb.org/docs/authentication-application
https://developer.themoviedb.org/docs/image-basics
https://github.com/thetvdb/v4-api/blob/main/docs/swagger.yml
https://github.com/fanart-tv/fanart.tv-api
https://github.com/fanart-tv/fanart.tv-api/blob/main/src/index.d.ts

Contract notes (example field shapes, NOT recorded live authenticated responses):
* TMDB /movie/{id}/images and /tv/{id}/images have posters[], backdrops[],
  logos[]; /tv/{id}/season/{n}/images has posters[] only;
  /tv/{id}/season/{n}/episode/{e}/images has stills[]. Image entries include
  file_path, width, height, aspect_ratio, iso_639_1, vote_average, vote_count.
  These image methods have no page parameter. No language filter is applied.
  Original URLs are https://image.tmdb.org/t/p/original{file_path}.
  TMDB_API_KEY accepts a 32-hex v3 api_key or a read access Bearer token.
  /find/{external_id}?external_source=imdb_id|tvdb_id returns movie_results[]
  and tv_results[]; TVDB find is NOT supported for movie IDs. Show external_ids
  has tvdb_id and imdb_id; movie external_ids has imdb_id, not tvdb_id.
* TVDB POST /login sends {apikey, pin?}, returns {data: {token}}; omit empty
  pin (subscriber PIN is needed only for user-supported project keys). Bearer
  tokens are documented as valid for a month; this module retains none between
  fetches. GET /series/{id}/extended and /movies/{id}/extended return
  {status: 'success', data: {artworks: [...], remoteIds: [...], ...}}.
  Artwork entries: id, type, image, thumbnail, language, width, height, score,
  includesText. Season /seasons/{seasonId}/extended uses data.artwork (singular).
  /artwork/types is the authoritative type catalog (id, name, recordType,
  imageFormat). /search/remoteid/{IMDb ID} returns data[{series|movie: {id}}].
  remoteIds[] has id, type, sourceName; /sources/types catalogs source types.
  Series episode listing /series/{id}/episodes/official accepts page (zero
  based), season, episodeNumber; data.episodes[] has image, imageType,
  seasonNumber, number, seriesId. Any links.next is treated only as a flag;
  at most five fixed-endpoint pages are requested, never a supplied URL.
* fanart.tv GET /v3/movies/{TMDB-or-IMDb-ID}, /v3/tv/{TVDB-ID}; api_key is a
  PROJECT key. Personal client_key is an additional credential, not a substitute
  for the project key; this module's config does not expose client_key. Detail
  responses are unpaginated type-named arrays, with id, url, lang, likes and
  season where applicable. v3.1 adds added; v3.2 adds width/height/image_count.
  Original URLs are on assets.fanart.tv under /fanart/, not /preview/.
  No fanart.tv episode artwork endpoint is implemented or substituted.

Returned image URLs preserve original formats (including transparent PNG logos);
this module does not download or convert bytes. Season/episode contexts MUST use
parent-series IDs in context.ids and official/aired season/episode numbers.
There is no title-search fallback, alternate-order conversion, persistent token
cache, or inferred season artwork. The application can cache the returned values.
"""
import re

import requests

from artwork_common import Artwork, ArtworkContext, ProviderError


_TIMEOUT = (3.05, 12)
_MAX_ITEMS = 1000
_MAX_PAGES = 5
_TMDB = 'https://api.themoviedb.org/3'
_TVDB = 'https://api4.thetvdb.com/v4'
_FANART = 'https://webservice.fanart.tv/v3'


def _numeric(value):
    text = str(value) if value is not None and not isinstance(value, bool) else ''
    return text if re.fullmatch(r'[1-9][0-9]{0,11}', text) else None


def _number(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100000


def _credential(config, key):
    value = config.get(key, '')
    return value.strip() if isinstance(value, str) else ''


def _records(value):
    return (row for row in value[:_MAX_ITEMS] if isinstance(row, dict)) if isinstance(value, list) else ()


class _Client:
    """One-call memoization only: no cross-user credential or metadata cache."""
    def __init__(self, session):
        self.session = session
        self.cache = {}

    def json(self, method, base, path, *, params=None, headers=None, payload=None):
        # Paths are constructed internally from validated numeric/IMDb IDs.
        if base not in (_TMDB, _TVDB, _FANART) or not path.startswith('/'):
            raise ProviderError('Invalid provider endpoint.')
        cache_key = (method, base, path, repr(params), repr(headers), repr(payload))
        if cache_key not in self.cache:
            kwargs = dict(params=params or {}, headers=headers or {},
                          timeout=_TIMEOUT, allow_redirects=False)
            if payload is not None:
                kwargs['json'] = payload
            response = self.session.request(method, base + path, **kwargs)
            if response.status_code != 200:
                raise ProviderError('Provider request failed.')
            result = response.json()
            if not isinstance(result, dict):
                raise ProviderError('Invalid provider response.')
            self.cache[cache_key] = result
        return self.cache[cache_key]


def _image(result, url, provider, kind):
    if not isinstance(url, str):
        return
    # Old API records sometimes contain HTTP CDN URLs; upgrade, never downgrade.
    if url.startswith('http://'):
        url = 'https://' + url[7:]
    try:
        image = Artwork(url, provider, kind)
    except (ValueError, TypeError):
        return
    if len(result) < _MAX_ITEMS and image.key not in {a.key for a in result}:
        result.append(image)


def _tmdb_request(client, key, path, params=None):
    params = dict(params or {})
    headers = {'Accept': 'application/json'}
    if re.fullmatch(r'[0-9a-fA-F]{32}', key):
        params['api_key'] = key
    else:
        headers['Authorization'] = 'Bearer ' + key.removeprefix('Bearer ')
    return client.json('GET', _TMDB, path, params=params, headers=headers)


def _tmdb_id(client, key, ids, movie):
    if ids.get('tmdb'):
        return ids['tmdb']
    external, source = (ids.get('imdb'), 'imdb_id') if ids.get('imdb') else (None, None)
    if not movie and not external and ids.get('tvdb'):
        external, source = ids['tvdb'], 'tvdb_id'
    if external:
        data = _tmdb_request(client, key, '/find/' + external, {'external_source': source})
        rows = list(_records(data.get('movie_results' if movie else 'tv_results')))
        # Never silently select among ambiguous identities.
        if len(rows) == 1:
            ids['tmdb'] = _numeric(rows[0].get('id'))
    return ids.get('tmdb')


def _tmdb_artwork(client, key, context, ids):
    movie = context.media_type == 'movie'
    media_id = _tmdb_id(client, key, ids, movie)
    if not media_id:
        raise ProviderError('TMDB: no matching provider ID.')
    path = ('/movie/' if movie else '/tv/') + media_id
    mapping = {'posters': 'poster', 'backdrops': 'background', 'logos': 'clearlogo'}
    if context.media_type in ('season', 'episode'):
        path += '/season/' + str(context.season_number)
        mapping = {'posters': 'poster'}
        if context.media_type == 'episode':
            path += '/episode/' + str(context.episode_number)
            # Plex episode thumbnails are the episode's poster slot.
            mapping = {'stills': 'poster'}
    data = _tmdb_request(client, key, path + '/images')
    result = []
    for field, kind in mapping.items():
        for row in _records(data.get(field)):
            file_path = row.get('file_path')
            # TMDB documents a PNG counterpart for each SVG logo; Pillow cannot
            # rasterize SVG safely, so offer that counterpart instead.
            if kind == 'clearlogo' and isinstance(file_path, str) and file_path.endswith('.svg'):
                file_path = file_path[:-4] + '.png'
            if isinstance(file_path, str) and re.fullmatch(r'/[A-Za-z0-9_.-]+\.(?:jpg|jpeg|png|webp)', file_path):
                _image(result, 'https://image.tmdb.org/t/p/original' + file_path, 'tmdb', kind)
    return result


def _tmdb_external_ids(client, key, context, ids):
    if not ids.get('tmdb'):
        return
    path = ('/movie/' if context.media_type == 'movie' else '/tv/') + ids['tmdb'] + '/external_ids'
    data = _tmdb_request(client, key, path)
    if context.media_type != 'movie' and not ids.get('tvdb'):
        ids['tvdb'] = _numeric(data.get('tvdb_id'))
    imdb = data.get('imdb_id')
    if not ids.get('imdb') and isinstance(imdb, str) and re.fullmatch(r'tt[0-9]{1,12}', imdb):
        ids['imdb'] = imdb


def _fanart_artwork(client, key, context, ids):
    # fanart.tv has no episode still endpoint.
    if context.media_type == 'episode':
        return []
    movie = context.media_type == 'movie'
    media_id = (ids.get('tmdb') or ids.get('imdb')) if movie else ids.get('tvdb')
    if not media_id:
        raise ProviderError('fanart.tv: no matching provider ID.')
    data = client.json('GET', _FANART, ('/movies/' if movie else '/tv/') + media_id,
                       params={'api_key': key})
    if movie:
        mapping = {'movieposter': 'poster', 'moviebackground': 'background',
                   'movie4kbackground': 'background', 'hdmovielogo': 'clearlogo', 'movielogo': 'clearlogo'}
    elif context.media_type == 'season':
        mapping = {'seasonposter': 'poster', 'seasonthumb': 'background',
                   'showbackground': 'background', 'show4kbackground': 'background'}
    else:
        mapping = {'tvposter': 'poster', 'showbackground': 'background',
                   'show4kbackground': 'background', 'hdtvlogo': 'clearlogo', 'clearlogo': 'clearlogo'}
    result = []
    for field, kind in mapping.items():
        for row in _records(data.get(field)):
            if context.media_type == 'season' and str(row.get('season')) != str(context.season_number):
                continue
            _image(result, row.get('url'), 'fanart.tv', kind)
    return result


# Type IDs verified against TheTVDB's own artwork upload links (not v3 keyTypes):
# https://thetvdb.com/series/breaking-bad
# https://thetvdb.com/movies/fight-club
# https://thetvdb.com/series/breaking-bad/seasons/official/1
# Runtime catalog: GET /artwork/types; fields id, name, recordType, imageFormat.
_TVDB_TYPES = {
    'show': {2: 'poster', 3: 'background', 23: 'clearlogo'},
    'movie': {14: 'poster', 15: 'background', 25: 'clearlogo'},
    'season': {7: 'poster', 8: 'background'},
}


def _tvdb_request(client, token, path, params=None):
    result = client.json('GET', _TVDB, path, params=params,
                         headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    if result.get('status', 'success') != 'success' or 'data' not in result:
        raise ProviderError('Invalid provider response.')
    return result


def _tvdb_episode(client, token, context, media_id, notices):
    result = []
    path = '/series/' + media_id + '/episodes/official'
    for page in range(_MAX_PAGES):
        data = _tvdb_request(client, token, path, {
            'page': page, 'season': context.season_number, 'episodeNumber': context.episode_number})
        payload = data.get('data')
        if not isinstance(payload, dict):
            raise ProviderError('Invalid provider response.')
        for row in _records(payload.get('episodes')):
            if (row.get('seasonNumber') == context.season_number
                    and row.get('number') == context.episode_number
                    and (row.get('seriesId') is None or str(row['seriesId']) == media_id)):
                _image(result, row.get('image'), 'tvdb', 'poster')
        # Only inspect existence, NEVER request the supplied next URL.
        links = data.get('links')
        if not isinstance(links, dict) or not links.get('next'):
            break
    else:
        notices.append('TVDB: episode pagination limit reached.')
    return result


def _tvdb_artwork(client, key, pin, context, ids, notices):
    payload = {'apikey': key}
    if pin:
        payload['pin'] = pin
    auth = client.json('POST', _TVDB, '/login', payload=payload)
    token = auth.get('data', {}).get('token') if isinstance(auth.get('data'), dict) else None
    if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
        raise ProviderError('Invalid provider response.')
    movie = context.media_type == 'movie'
    if not ids.get('tvdb') and ids.get('imdb'):
        data = _tvdb_request(client, token, '/search/remoteid/' + ids['imdb'])
        record_type = 'movie' if movie else 'series'
        matches = {candidate for row in _records(data.get('data'))
                   if isinstance(row.get(record_type), dict)
                   if (candidate := _numeric(row[record_type].get('id')))}
        if len(matches) == 1:
            ids['tvdb'] = matches.pop()
    if not ids.get('tvdb'):
        raise ProviderError('TVDB: no matching provider ID.')
    media_id = ids['tvdb']
    if context.media_type == 'episode':
        return _tvdb_episode(client, token, context, media_id, notices)
    data = _tvdb_request(client, token, ('/movies/' if movie else '/series/') + media_id + '/extended')['data']
    if not isinstance(data, dict):
        raise ProviderError('Invalid provider response.')
    for remote in _records(data.get('remoteIds')):
        source = re.sub(r'[^a-z0-9]', '', str(remote.get('sourceName', '')).lower())
        if not ids.get('tmdb') and source in ('tmdb', 'themoviedb', 'themoviedbcom', 'themoviedatabase'):
            ids['tmdb'] = _numeric(remote.get('id'))
        if not ids.get('imdb') and source in ('imdb', 'imdbcom'):
            imdb = remote.get('id')
            if isinstance(imdb, str) and re.fullmatch(r'tt[0-9]{1,12}', imdb):
                ids['imdb'] = imdb
    field = 'artworks'
    if context.media_type == 'season':
        seasons = [row for row in _records(data.get('seasons'))
                   if row.get('number') == context.season_number
                   and isinstance(row.get('type'), dict) and row['type'].get('type') == 'official'
                   and _numeric(row.get('id'))]
        if len(seasons) != 1:
            # Do not substitute series posters or another season ordering.
            return []
        data = _tvdb_request(client, token, '/seasons/' + str(seasons[0]['id']) + '/extended')['data']
        if not isinstance(data, dict):
            raise ProviderError('Invalid provider response.')
        field = 'artwork'  # The official season schema is singular.
    result = []
    types = _TVDB_TYPES[context.media_type]
    for row in _records(data.get(field)):
        kind = types.get(row.get('type'))
        if kind:
            _image(result, row.get('image'), 'tvdb', kind)
    return result


def fetch_artwork(context: ArtworkContext, config: dict, session=None) -> tuple[list[Artwork], list[str]]:
    """Return (artwork, sanitized notices); empty credentials disable providers.

    context.ids holds parent-series IDs for seasons/episodes. Episode stills are
    returned as ``poster`` because that is Plex's episode thumbnail asset slot.
    No credentials or raw upstream exception text are returned in notices.
    """
    if not any(_credential(config, key) for key in ('TMDB_API_KEY', 'TVDB_API_KEY', 'FANART_API_KEY')):
        return [], []
    if (context.media_type not in ('movie', 'show', 'season', 'episode')
            or (context.media_type in ('season', 'episode') and not _number(context.season_number))
            or (context.media_type == 'episode' and not _number(context.episode_number))):
        return [], ['Artwork: unsupported or incomplete media context.']
    ids = {name: _numeric(context.ids.get(name)) for name in ('tmdb', 'tvdb')}
    imdb = context.ids.get('imdb')
    ids['imdb'] = imdb if isinstance(imdb, str) and re.fullmatch(r'tt[0-9]{1,12}', imdb) else None
    owned = session is None
    session = requests.Session() if owned else session
    client = _Client(session)
    result, notices = [], []
    try:
        key = _credential(config, 'TMDB_API_KEY')
        if key:
            try:
                result.extend(_tmdb_artwork(client, key, context, ids))
            except ProviderError as exc:
                notices.append('TMDB: no matching provider ID.' if str(exc) == 'TMDB: no matching provider ID.'
                               else 'TMDB: artwork request failed.')
            except Exception:
                notices.append('TMDB: artwork request failed.')
            needs_tvdb = _credential(config, 'TVDB_API_KEY') and (context.media_type != 'movie' or not ids.get('imdb'))
            needs_fanart_tv = _credential(config, 'FANART_API_KEY') and context.media_type in ('show', 'season')
            if not ids.get('tvdb') and (needs_tvdb or needs_fanart_tv):
                try:
                    _tmdb_external_ids(client, key, context, ids)
                except Exception:
                    notices.append('TMDB: ID lookup failed.')
        key = _credential(config, 'TVDB_API_KEY')
        if key:
            try:
                result.extend(_tvdb_artwork(client, key, _credential(config, 'TVDB_PIN'), context, ids, notices))
            except ProviderError as exc:
                notices.append('TVDB: no matching provider ID.' if str(exc) == 'TVDB: no matching provider ID.'
                               else 'TVDB: artwork request failed.')
            except Exception:
                notices.append('TVDB: artwork request failed.')
        tmdb_key = _credential(config, 'TMDB_API_KEY')
        if tmdb_key and ids.get('tmdb') and 'TMDB: no matching provider ID.' in notices:
            # An authoritative TVDB remoteId can succeed where TMDB /find did not.
            notices.remove('TMDB: no matching provider ID.')
            try:
                result.extend(_tmdb_artwork(client, tmdb_key, context, ids))
            except Exception:
                notices.append('TMDB: artwork request failed.')
        key = _credential(config, 'FANART_API_KEY')
        if key:
            try:
                result.extend(_fanart_artwork(client, key, context, ids))
            except ProviderError as exc:
                notices.append('fanart.tv: no matching provider ID.' if str(exc) == 'fanart.tv: no matching provider ID.'
                               else 'fanart.tv: artwork request failed.')
            except Exception:
                notices.append('fanart.tv: artwork request failed.')
    finally:
        if owned:
            session.close()
    return result, notices

"""Offline contract tests using the providers' documented JSON shapes."""
import importlib.util
import unittest

from artwork_common import ArtworkContext


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class Session:
    def __init__(self, routes):
        self.routes, self.calls = dict(routes), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        self_key = (method, url)
        if self_key not in self.routes:
            raise AssertionError(f'Unexpected request: {method} {url}')
        result = self.routes[self_key]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, list):
            result = result.pop(0)
        return result if isinstance(result, Response) else Response(result)


TMDB = 'https://api.themoviedb.org/3'
TVDB = 'https://api4.thetvdb.com/v4'
FANART = 'https://webservice.fanart.tv/v3'


class ProviderTests(unittest.TestCase):
    def test_tmdb_svg_logos_use_the_documented_png_variant(self):
        from artwork_providers import fetch_artwork
        session = Session({('GET', TMDB + '/movie/10/images'): {
            'logos': [{'file_path': '/logo.svg', 'file_type': '.svg'}]}})
        images, notices = fetch_artwork(ArtworkContext('movie', {'tmdb': '10'}),
                                        {'TMDB_API_KEY': 'a' * 32}, session)
        self.assertEqual(notices, [])
        self.assertEqual([a.url for a in images], ['https://image.tmdb.org/t/p/original/logo.png'])

    def test_tmdb_movie_types_original_png_and_request_security(self):
        from artwork_providers import fetch_artwork
        session = Session({('GET', TMDB + '/movie/10/images'): {
            'id': 10, 'posters': [{'file_path': '/poster.jpg'}],
            'backdrops': [{'file_path': '/backdrop.jpg'}],
            'logos': [{'file_path': '/transparent.png'}, {'file_path': '//evil.test/a.png'},
                      {'file_path': '/bad.png?api_key=secret'}]}})
        art, notices = fetch_artwork(ArtworkContext('movie', {'tmdb': '10'}),
                                     {'TMDB_API_KEY': 'a' * 32}, session)
        self.assertEqual(notices, [])
        self.assertEqual([(a.asset_type, a.url) for a in art], [
            ('poster', 'https://image.tmdb.org/t/p/original/poster.jpg'),
            ('background', 'https://image.tmdb.org/t/p/original/backdrop.jpg'),
            ('clearlogo', 'https://image.tmdb.org/t/p/original/transparent.png')])
        self.assertTrue(all(a.provider == 'tmdb' and a.origin == 'direct' for a in art))
        kwargs = session.calls[0][2]
        self.assertEqual(kwargs['params']['api_key'], 'a' * 32)
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (3.05, 12))

    def test_tmdb_season_and_episode_strict_scope_read_token(self):
        from artwork_providers import fetch_artwork
        cases = [
            (ArtworkContext('season', {'tmdb': '10'}, 0), '/tv/10/season/0/images',
             {'posters': [{'file_path': '/season.jpg'}], 'logos': [{'file_path': '/show.png'}]}, 'poster'),
            (ArtworkContext('episode', {'tmdb': '10'}, 2, 3), '/tv/10/season/2/episode/3/images',
             {'stills': [{'file_path': '/episode.jpg'}], 'posters': [{'file_path': '/show.jpg'}]}, 'poster')]
        for context, path, payload, kind in cases:
            with self.subTest(path=path):
                session = Session({('GET', TMDB + path): payload})
                art, notices = fetch_artwork(context, {'TMDB_API_KEY': 'eyJ.read.token'}, session)
                self.assertEqual(len(art), 1)
                self.assertEqual(art[0].asset_type, kind)
                self.assertEqual(notices, [])
                self.assertEqual(session.calls[0][2]['headers']['Authorization'], 'Bearer eyJ.read.token')
                self.assertNotIn('api_key', session.calls[0][2]['params'])

    def test_tmdb_find_tvdb_id_without_mutating_context(self):
        from artwork_providers import fetch_artwork
        context = ArtworkContext('show', {'tvdb': '42'})
        session = Session({
            ('GET', TMDB + '/find/42'): {'tv_results': [{'id': 10}], 'movie_results': []},
            ('GET', TMDB + '/tv/10/images'): {'posters': [{'file_path': '/show.jpg'}]}})
        art, notices = fetch_artwork(context, {'TMDB_API_KEY': 'a' * 32}, session)
        self.assertEqual(len(art), 1)
        self.assertEqual(notices, [])
        self.assertEqual(context.ids, {'tvdb': '42'})
        self.assertEqual(session.calls[0][2]['params']['external_source'], 'tvdb_id')

    def test_failure_sanitized_redirect_rejected(self):
        from artwork_providers import fetch_artwork
        for result in [RuntimeError('API secret-in-exception'), Response({}, 302), Response({}, 401), Response([])]:
            session = Session({('GET', TMDB + '/movie/10/images'): result})
            art, notices = fetch_artwork(ArtworkContext('movie', {'tmdb': '10'}),
                                         {'TMDB_API_KEY': 'secret-in-exception'}, session)
            self.assertEqual(art, [])
            self.assertEqual(notices, ['TMDB: artwork request failed.'])

    def test_fanart_movie_and_show_fields_and_fixed_image_hosts(self):
        from artwork_providers import fetch_artwork
        cases = [
            ('movie', {'tmdb': '10'}, '/movies/10', {
                'movieposter': [{'url': 'https://assets.fanart.tv/fanart/poster.jpg'}],
                'moviebackground': [{'url': 'https://assets.fanart.tv/fanart/bg.jpg'}],
                'hdmovielogo': [{'url': 'https://assets.fanart.tv/fanart/logo.png'}],
                'movielogo': [{'url': 'https://evil.test/logo.png'}]}),
            ('show', {'tvdb': '42'}, '/tv/42', {
                'tvposter': [{'url': 'https://assets.fanart.tv/fanart/poster.jpg'}],
                'show4kbackground': [{'url': 'https://assets.fanart.tv/fanart/bg.jpg'}],
                'hdtvlogo': [{'url': 'https://assets.fanart.tv/fanart/logo.png'}]})]
        for media, ids, path, payload in cases:
            session = Session({('GET', FANART + path): payload})
            art, notices = fetch_artwork(ArtworkContext(media, ids), {'FANART_API_KEY': 'project-key'}, session)
            self.assertEqual([a.asset_type for a in art], ['poster', 'background', 'clearlogo'])
            self.assertTrue(all(a.provider == 'fanart.tv' for a in art))
            self.assertEqual(notices, [])
            self.assertEqual(session.calls[0][2]['params'], {'api_key': 'project-key'})
            self.assertFalse(session.calls[0][2]['allow_redirects'])

    def test_fanart_season_exact_filter_no_show_or_all_season_substitution(self):
        from artwork_providers import fetch_artwork
        url = 'https://assets.fanart.tv/fanart/'
        session = Session({('GET', FANART + '/tv/42'): {
            'tvposter': [{'url': url + 'show.jpg'}],
            'seasonposter': [{'season': '2', 'url': url + 's2.jpg'},
                             {'season': '1', 'url': url + 's1.jpg'},
                             {'season': 'all', 'url': url + 'all.jpg'}],
            'seasonthumb': [{'season': '2', 'url': url + 's2-bg.jpg'}],
            'showbackground': [{'season': 'all', 'url': url + 'all-bg.jpg'}],
            'hdtvlogo': [{'url': url + 'show.png'}]}})
        art, notices = fetch_artwork(ArtworkContext('season', {'tvdb': '42'}, 2),
                                     {'FANART_API_KEY': 'project-key'}, session)
        self.assertEqual([(a.asset_type, a.url) for a in art],
                         [('poster', url + 's2.jpg'), ('background', url + 's2-bg.jpg')])
        self.assertEqual(notices, [])
        episode_session = Session({})
        self.assertEqual(fetch_artwork(ArtworkContext('episode', {'tvdb': '42'}, 2, 3),
                                        {'FANART_API_KEY': 'project-key'}, episode_session), ([], []))
        self.assertEqual(episode_session.calls, [])

    def test_tmdb_external_ids_enable_fanart_even_when_tmdb_images_fail(self):
        from artwork_providers import fetch_artwork
        session = Session({
            ('GET', TMDB + '/tv/10/images'): Response({}, 503),
            ('GET', TMDB + '/tv/10/external_ids'): {'id': 10, 'tvdb_id': 42, 'imdb_id': 'tt123'},
            ('GET', FANART + '/tv/42'): {'hdtvlogo': [{'url': 'https://assets.fanart.tv/fanart/logo.png'}]}})
        art, notices = fetch_artwork(ArtworkContext('show', {'tmdb': '10'}),
                                     {'TMDB_API_KEY': 'a' * 32, 'FANART_API_KEY': 'project'}, session)
        self.assertEqual(len(art), 1)
        self.assertEqual(art[0].provider, 'fanart.tv')
        self.assertEqual(notices, ['TMDB: artwork request failed.'])

    def test_tvdb_show_and_movie_artwork_type_ids_login_pin(self):
        from artwork_providers import fetch_artwork
        cases = [('show', 'series', (2, 3, 23)), ('movie', 'movies', (14, 15, 25))]
        for media, path, types in cases:
            for pin in ('', 'subscriber-pin'):
                payload = {'status': 'success', 'data': {'artworks': [
                    {'type': types[0], 'image': 'https://artworks.thetvdb.com/banners/poster.jpg'},
                    {'type': types[1], 'image': 'https://artworks.thetvdb.com/banners/background.jpg'},
                    {'type': types[2], 'image': 'https://artworks.thetvdb.com/banners/logo.png'},
                    {'type': 1, 'image': 'https://artworks.thetvdb.com/banners/banner.jpg'}]}}
                session = Session({('POST', TVDB + '/login'): {'data': {'token': 'jwt'}},
                                   ('GET', TVDB + f'/{path}/42/extended'): payload})
                art, notices = fetch_artwork(ArtworkContext(media, {'tvdb': '42'}),
                                             {'TVDB_API_KEY': 'key', 'TVDB_PIN': pin}, session)
                self.assertEqual([a.asset_type for a in art], ['poster', 'background', 'clearlogo'])
                self.assertTrue(art[-1].url.endswith('.png'))
                self.assertEqual(notices, [])
                self.assertEqual(session.calls[0][2]['json'],
                                 {'apikey': 'key', **({'pin': pin} if pin else {})})
                self.assertEqual(session.calls[1][2]['headers']['Authorization'], 'Bearer jwt')

    def test_tvdb_season_uses_its_own_id_and_singular_artwork_field(self):
        from artwork_providers import fetch_artwork
        session = Session({
            ('POST', TVDB + '/login'): {'data': {'token': 'jwt'}},
            ('GET', TVDB + '/series/42/extended'): {'data': {
                'artworks': [{'type': 2, 'image': 'https://artworks.thetvdb.com/show.jpg'}],
                'seasons': [{'id': 90, 'number': 2, 'type': {'type': 'dvd'}},
                            {'id': 99, 'number': 2, 'type': {'type': 'official'}}]}},
            ('GET', TVDB + '/seasons/99/extended'): {'data': {'artwork': [
                {'type': 7, 'image': 'https://artworks.thetvdb.com/season.jpg'},
                {'type': 8, 'image': 'https://artworks.thetvdb.com/season-bg.jpg'},
                {'type': 2, 'image': 'https://artworks.thetvdb.com/wrong.jpg'}]}}})
        art, notices = fetch_artwork(ArtworkContext('season', {'tvdb': '42'}, 2),
                                     {'TVDB_API_KEY': 'key'}, session)
        self.assertEqual([a.url for a in art], ['https://artworks.thetvdb.com/season.jpg',
                                               'https://artworks.thetvdb.com/season-bg.jpg'])
        self.assertEqual(notices, [])

    def test_tvdb_episode_still_exact_match_and_bounded_fixed_host_pagination(self):
        from artwork_providers import fetch_artwork
        path = TVDB + '/series/42/episodes/official'
        session = Session({
            ('POST', TVDB + '/login'): {'data': {'token': 'jwt'}},
            ('GET', path): [
                Response({'data': {'episodes': [{'number': 3, 'seasonNumber': 1,
                                                'image': 'https://artworks.thetvdb.com/wrong.jpg'}]},
                          'links': {'next': 'https://evil.test/?secret=key'}}),
                Response({'data': {'episodes': [{'number': 3, 'seasonNumber': 2, 'seriesId': 42,
                                                'image': 'https://artworks.thetvdb.com/episode.jpg'}]},
                          'links': {'next': None}})]})
        art, notices = fetch_artwork(ArtworkContext('episode', {'tvdb': '42'}, 2, 3),
                                     {'TVDB_API_KEY': 'key'}, session)
        self.assertEqual([(a.asset_type, a.url) for a in art], [('poster', 'https://artworks.thetvdb.com/episode.jpg')])
        self.assertEqual(notices, [])
        self.assertEqual([call[2]['params']['page'] for call in session.calls[1:]], [0, 1])
        self.assertTrue(all(c[1] == path for c in session.calls[1:]))
        repeated = {'data': {'episodes': []}, 'links': {'next': 'anything'}}
        session = Session({('POST', TVDB + '/login'): {'data': {'token': 'jwt'}},
                           ('GET', path): repeated})
        art, notices = fetch_artwork(ArtworkContext('episode', {'tvdb': '42'}, 2, 3),
                                     {'TVDB_API_KEY': 'key'}, session)
        self.assertEqual(art, [])
        self.assertEqual(len(session.calls), 6)  # login plus at most five pages
        self.assertEqual(notices, ['TVDB: episode pagination limit reached.'])

    def test_tvdb_remote_id_imdb_resolves_show_and_powers_fanart(self):
        from artwork_providers import fetch_artwork
        session = Session({
            ('POST', TVDB + '/login'): {'data': {'token': 'jwt'}},
            ('GET', TVDB + '/search/remoteid/tt123'): {'data': [{'series': {'id': 42}}]},
            ('GET', TVDB + '/series/42/extended'): {'data': {'artworks': []}},
            ('GET', FANART + '/tv/42'): {'hdtvlogo': [{'url': 'https://assets.fanart.tv/fanart/logo.png'}]}})
        art, notices = fetch_artwork(ArtworkContext('show', {'imdb': 'tt123'}),
                                     {'TVDB_API_KEY': 'key', 'FANART_API_KEY': 'project'}, session)
        self.assertEqual([a.provider for a in art], ['fanart.tv'])
        self.assertEqual(notices, [])

    def test_tvdb_failure_does_not_hide_other_provider_artwork_or_secrets(self):
        from artwork_providers import fetch_artwork
        session = Session({
            ('GET', TMDB + '/tv/10/images'): {'posters': [{'file_path': '/show.jpg'}]},
            ('POST', TVDB + '/login'): RuntimeError('url?apikey=secret&pin=secret-pin'),
            ('GET', FANART + '/tv/42'): {'hdtvlogo': [{'url': 'https://assets.fanart.tv/fanart/logo.png'}]}})
        art, notices = fetch_artwork(ArtworkContext('show', {'tmdb': '10', 'tvdb': '42'}),
                                     {'TMDB_API_KEY': 'a' * 32, 'TVDB_API_KEY': 'secret',
                                      'TVDB_PIN': 'secret-pin', 'FANART_API_KEY': 'project'}, session)
        self.assertEqual([a.provider for a in art], ['tmdb', 'fanart.tv'])
        self.assertEqual(notices, ['TVDB: artwork request failed.'])

    def test_tvdb_remote_ids_resolve_movie_tmdb_for_fanart(self):
        from artwork_providers import fetch_artwork
        session = Session({
            ('POST', TVDB + '/login'): {'data': {'token': 'jwt'}},
            ('GET', TVDB + '/movies/42/extended'): {'data': {
                'artworks': [], 'remoteIds': [{'id': '10', 'type': 12, 'sourceName': 'TheMovieDB.com'}]}},
            ('GET', FANART + '/movies/10'): {'movieposter': [{'url': 'https://assets.fanart.tv/fanart/poster.jpg'}]}})
        art, notices = fetch_artwork(ArtworkContext('movie', {'tvdb': '42'}),
                                     {'TVDB_API_KEY': 'key', 'FANART_API_KEY': 'project'}, session)
        self.assertEqual(len(art), 1)
        self.assertEqual(notices, [])

    def test_invalid_context_or_ids_never_become_metadata_urls(self):
        from artwork_providers import fetch_artwork
        session = Session({})
        for context in [ArtworkContext('season', {'tmdb': '10'}),
                        ArtworkContext('episode', {'tvdb': '42'}, 1),
                        ArtworkContext('episode', {'tvdb': '42'}, -1, 3),
                        ArtworkContext('season', {'tmdb': '10'}, True), ArtworkContext('artist')]:
            self.assertEqual(fetch_artwork(context, {'TMDB_API_KEY': 'key'}, session),
                             ([], ['Artwork: unsupported or incomplete media context.']))
        art, notices = fetch_artwork(ArtworkContext('show', {'tmdb': '../foo?secret=key', 'imdb': 'https://evil.test'}),
                                     {'TMDB_API_KEY': 'key'}, session)
        self.assertEqual(art, [])
        self.assertEqual(notices, ['TMDB: no matching provider ID.'])
        self.assertEqual(session.calls, [])

    def test_reject_ambiguous_tmdb_mapping_without_title_fallback(self):
        from artwork_providers import fetch_artwork
        session = Session({('GET', TMDB + '/find/tt123'): {'movie_results': [{'id': 10}, {'id': 11}]}})
        art, notices = fetch_artwork(ArtworkContext('movie', {'imdb': 'tt123'}), {'TMDB_API_KEY': 'key'}, session)
        self.assertEqual((art, notices), ([], ['TMDB: no matching provider ID.']))
        self.assertEqual(len(session.calls), 1)

    def test_exception_text_never_displayed_even_with_lookalike_prefix(self):
        from artwork_common import ProviderError
        from artwork_providers import fetch_artwork
        session = Session({('GET', TMDB + '/movie/10/images'): ProviderError('TMDB: no matching secret-api-key')})
        self.assertEqual(fetch_artwork(ArtworkContext('movie', {'tmdb': '10'}), {'TMDB_API_KEY': 'key'}, session),
                         ([], ['TMDB: artwork request failed.']))

    def test_tmdb_movie_with_fanart_does_not_fetch_unneeded_external_ids(self):
        from artwork_providers import fetch_artwork
        session = Session({
            ('GET', TMDB + '/movie/10/images'): {'posters': [{'file_path': '/movie.jpg'}]},
            ('GET', FANART + '/movies/10'): {'hdmovielogo': [{'url': 'https://assets.fanart.tv/fanart/logo.png'}]}})
        art, notices = fetch_artwork(ArtworkContext('movie', {'tmdb': '10'}),
                                     {'TMDB_API_KEY': 'a' * 32, 'FANART_API_KEY': 'project'}, session)
        self.assertEqual(len(art), 2)
        self.assertEqual(notices, [])
        self.assertEqual(len(session.calls), 2)

    def test_tvdb_remote_mapping_retries_tmdb_when_find_has_no_match(self):
        from artwork_providers import fetch_artwork
        session = Session({
            ('GET', TMDB + '/find/42'): {'tv_results': []},
            ('POST', TVDB + '/login'): {'data': {'token': 'jwt'}},
            ('GET', TVDB + '/series/42/extended'): {'data': {
                'artworks': [], 'remoteIds': [{'id': '10', 'sourceName': 'TheMovieDB.com'}]}},
            ('GET', TMDB + '/tv/10/images'): {'posters': [{'file_path': '/show.jpg'}]}})
        art, notices = fetch_artwork(ArtworkContext('show', {'tvdb': '42'}),
                                     {'TMDB_API_KEY': 'a' * 32, 'TVDB_API_KEY': 'key'}, session)
        self.assertEqual([a.provider for a in art], ['tmdb'])
        self.assertEqual(notices, [])

    def test_empty_configuration_skips_network(self):
        self.assertIsNotNone(importlib.util.find_spec('artwork_providers'),
                             'Provider module must exist')
        from artwork_providers import fetch_artwork
        self.assertEqual(fetch_artwork(ArtworkContext('movie'), {}, session=object()), ([], []))


if __name__ == '__main__':
    unittest.main()

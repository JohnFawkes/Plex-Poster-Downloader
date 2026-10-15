"""Network-free contract tests for the anime poster adapters."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch


class Response:
    def __init__(self, data, status=200, headers=None):
        self.body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        self.status_code = status
        self.headers = headers or {}

    def iter_content(self, chunk_size=65536):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]

    def close(self):
        pass


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {'DATA_DIR': self.tmp.name, 'ANIME_AUTO_MAPPING': False}
        self.session = Mock()
        import anime_providers as providers
        self.addCleanup(patch.stopall)
        patch.object(providers, '_NEXT_REQUEST', {}).start()
        patch.object(providers, '_COOLDOWNS', {}).start()

    def context(self, ids, media_type='show', season=None):
        return ArtworkContext(media_type, ids, season_number=season, is_anime=True)

    def test_anilist_public_query_returns_valid_poster_without_auth(self):
        from anime_providers import fetch_artwork
        self.session.post.return_value = Response({'data': {'Media': {
            'id': 1, 'idMal': 1, 'coverImage': {
                'extraLarge': 'https://s4.anilist.co/file/anilistcdn/media/anime/cover/a.jpg'}}}})
        artwork, notices = fetch_artwork(self.context({'anilist': '1'}), self.config, self.session)
        self.assertEqual(len(artwork), 1)
        self.assertEqual(artwork[0].provider, 'anilist')
        self.assertEqual(artwork[0].asset_type, 'poster')
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], 'https://graphql.anilist.co')
        self.assertNotIn('Authorization', kwargs.get('headers', {}))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertIn('timeout', kwargs)
        self.assertEqual(kwargs['json']['variables'], {'id': 1})
        self.session.get.assert_not_called()
        self.assertEqual(notices, [])

    def test_mal_requires_client_id_without_network(self):
        from anime_providers import fetch_artwork
        artwork, notices = fetch_artwork(self.context({'mal': '1'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertIn('MAL_CLIENT_ID', str(notices))
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_mal_uses_official_endpoint_and_client_header(self):
        from anime_providers import fetch_artwork
        self.config['MAL_CLIENT_ID'] = 'private-client-value'
        self.session.get.return_value = Response({'id': 1, 'main_picture': {
            'large': 'https://cdn.myanimelist.net/images/anime/cover.jpg'}})
        artwork, notices = fetch_artwork(self.context({'mal': '1'}), self.config, self.session)
        self.assertEqual([a.provider for a in artwork], ['mal'])
        args, kwargs = self.session.get.call_args
        self.assertEqual(args[0], 'https://api.myanimelist.net/v2/anime/1')
        self.assertEqual(kwargs['headers']['X-MAL-CLIENT-ID'], 'private-client-value')
        self.assertEqual(kwargs['params']['fields'], 'id,main_picture')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(notices, [])

    def test_anidb_requires_registered_client_without_network(self):
        from anime_providers import fetch_artwork
        artwork, notices = fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertIn('ANIDB_CLIENT', str(notices))
        self.assertIn('registered', str(notices).lower())
        self.session.get.assert_not_called()

    def test_anidb_returns_https_cover_from_xml_picture(self):
        from anime_providers import fetch_artwork
        self.config.update(ANIDB_CLIENT='registeredapp', ANIDB_CLIENT_VERSION='1')
        self.session.get.return_value = Response('<anime id="23"><picture>440.jpg</picture></anime>')
        artwork, notices = fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
        self.assertEqual([a.url for a in artwork], ['https://cdn.anidb.net/images/main/440.jpg'])
        args, kwargs = self.session.get.call_args
        self.assertEqual(args[0], 'http://api.anidb.net:9001/httpapi')
        self.assertEqual(kwargs['params'], {'request': 'anime', 'client': 'registeredapp',
                                          'clientver': 1, 'protover': 1, 'aid': 23})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(notices, [])

    def test_anidb_success_is_cached_across_repeated_and_restarted_calls(self):
        import anime_providers as providers
        self.config.update(ANIDB_CLIENT='registeredapp', ANIDB_CLIENT_VERSION=1)
        self.session.get.return_value = Response('<anime id="23"><picture>440.jpg</picture></anime>')
        first = providers.fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
        # Evict the optional process cache: DATA_DIR cache must survive restart.
        if hasattr(providers, '_CACHE'):
            providers._CACHE.clear()
        second = providers.fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
        self.assertEqual(first, second)
        self.assertEqual(self.session.get.call_count, 1)

    def test_anidb_requests_are_spaced_at_least_two_seconds(self):
        import anime_providers as providers
        self.config.update(ANIDB_CLIENT='registeredapp', ANIDB_CLIENT_VERSION=1)
        self.session.get.side_effect = [Response('<anime id="23"><picture>440.jpg</picture></anime>'),
                                        Response('<anime id="1"><picture>441.jpg</picture></anime>')]
        clock = [100.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(providers, '_NEXT_REQUEST', {}, create=True), \
             patch.object(providers, '_COOLDOWNS', {}, create=True), \
             patch('time.monotonic', side_effect=lambda: clock[0]), \
             patch('time.sleep', side_effect=sleep) as sleeping:
            providers.fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
            providers.fetch_artwork(self.context({'anidb': '1'}), self.config, self.session)
        self.assertTrue(sleeping.called)
        self.assertGreaterEqual(sum(c.args[0] for c in sleeping.call_args_list), 2)

    def test_anidb_error_response_is_cached_and_never_echoed(self):
        from anime_providers import fetch_artwork
        self.config.update(ANIDB_CLIENT='registeredapp', ANIDB_CLIENT_VERSION=1)
        self.session.get.return_value = Response('<error code="500">secret-value</error>')
        first = fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
        second = fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
        self.assertEqual(first, second)
        self.assertEqual(self.session.get.call_count, 1)
        self.assertNotIn('secret-value', str(first))

    def test_retry_after_stops_requests_instead_of_immediate_retry(self):
        from anime_providers import fetch_artwork
        self.session.post.return_value = Response({}, status=429, headers={'Retry-After': '120'})
        first = fetch_artwork(self.context({'anilist': '1'}), self.config, self.session)
        second = fetch_artwork(self.context({'anilist': '2'}), self.config, self.session)
        self.assertFalse(first[0])
        self.assertFalse(second[0])
        self.assertEqual(self.session.post.call_count, 1)
        self.assertIn('rate limited', str(second[1]))

    def test_anidb_ban_stops_different_entry_requests(self):
        from anime_providers import fetch_artwork
        self.config.update(ANIDB_CLIENT='registeredapp', ANIDB_CLIENT_VERSION=1)
        self.session.get.return_value = Response('<error code="500">Banned</error>')
        fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
        artwork, notices = fetch_artwork(self.context({'anidb': '24'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertTrue(notices)
        self.assertEqual(self.session.get.call_count, 1)

    def test_invalid_explicit_id_is_not_repaired_or_requested(self):
        from anime_providers import fetch_artwork
        artwork, notices = fetch_artwork(self.context({'mal': ' 1 '}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertTrue(notices)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_owned_sessions_are_closed_on_provider_failure(self):
        from anime_providers import fetch_artwork
        with patch('anime_providers.requests.Session', return_value=self.session):
            fetch_artwork(self.context({'mal': '1'}), self.config)
        self.session.close.assert_called_once()

    def test_resolver_closes_its_owned_session(self):
        from anime_providers import resolve_anime_ids
        self.config['ANIME_AUTO_MAPPING'] = True
        with patch('anime_providers.requests.Session', return_value=self.session):
            resolve_anime_ids({}, 1, self.config)
        self.session.close.assert_called_once()

    def test_supplied_session_is_never_closed(self):
        from anime_providers import fetch_artwork
        fetch_artwork(self.context({'mal': '1'}), self.config, self.session)
        self.session.close.assert_not_called()

    def test_redirects_are_not_followed_and_location_is_not_exposed(self):
        from anime_providers import fetch_artwork
        self.session.post.return_value = Response({}, status=302,
            headers={'Location': 'https://evil.invalid/?key=secret-value'})
        artwork, notices = fetch_artwork(self.context({'anilist': '1'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertNotIn('secret-value', str(notices))
        self.assertNotIn('https://', str(notices))
        self.assertFalse(self.session.post.call_args.kwargs['allow_redirects'])

    def test_streamed_response_size_is_bounded(self):
        from anime_providers import fetch_artwork
        self.session.post.return_value = Response('x' * (1024 * 1024 + 1))
        artwork, notices = fetch_artwork(self.context({'anilist': '1'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertIn('size limit', str(notices))

    def test_untrusted_picture_query_does_not_leak_secret(self):
        from anime_providers import fetch_artwork
        self.config['MAL_CLIENT_ID'] = 'secret-value'
        self.session.get.return_value = Response({'id': 1, 'main_picture': {
            'large': 'https://cdn.myanimelist.net/cover.jpg?key=secret-value'}})
        artwork, notices = fetch_artwork(self.context({'mal': '1'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertNotIn('secret-value', str(notices))
        self.assertNotIn('https://', str(notices))

    def test_anidb_rejects_xml_entities(self):
        from anime_providers import fetch_artwork
        self.config.update(ANIDB_CLIENT='registeredapp', ANIDB_CLIENT_VERSION=1)
        self.session.get.return_value = Response('''<!DOCTYPE anime [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
            <anime id="23"><picture>&xxe;</picture></anime>''')
        artwork, notices = fetch_artwork(self.context({'anidb': '23'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertIn('invalid XML', str(notices))

    def test_network_exception_text_cannot_leak_client_id(self):
        import requests
        from anime_providers import fetch_artwork
        self.config['MAL_CLIENT_ID'] = 'secret-value'
        self.session.get.side_effect = requests.ConnectionError('https://secret-value.invalid/')
        artwork, notices = fetch_artwork(self.context({'mal': '1'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertNotIn('secret-value', str(notices))
        self.assertNotIn('https://', str(notices))

    def test_anilist_graphql_error_is_sanitized(self):
        from anime_providers import fetch_artwork
        self.session.post.return_value = Response({'errors': [{'message': 'secret-client-value'}]})
        artwork, notices = fetch_artwork(self.context({'anilist': '1'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertTrue(notices)
        self.assertNotIn('secret-client-value', str(notices))

    def test_anilist_rejects_untrusted_cover_host(self):
        from anime_providers import fetch_artwork
        self.session.post.return_value = Response({'data': {'Media': {
            'id': 1, 'coverImage': {'extraLarge': 'https://evil.invalid/cover.jpg'}}}})
        artwork, notices = fetch_artwork(self.context({'anilist': '1'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertTrue(notices)

from artwork_common import ArtworkContext


class MappingTests(unittest.TestCase):
    """Crosswalk fixtures are intentionally small and have no scraped metadata."""
    setUp = ProviderTests.setUp
    context = ProviderTests.context
    XML = '''<anime-list>
      <anime anidbid="10" tvdbid="100" defaulttvdbseason="1" tmdbtv="200" tmdbseason="1"/>
      <anime anidbid="20" tvdbid="100" defaulttvdbseason="2" tmdbtv="200" tmdbseason="3"/>
    </anime-list>'''
    BRIDGE = [{'anidb_id': 10, 'mal_id': 110, 'anilist_id': 210},
              {'anidb_id': 20, 'mal_id': 120, 'anilist_id': 220}]

    def resolver(self, ids, season=2, media_type='season'):
        from anime_providers import resolve_anime_ids
        self.config['ANIME_AUTO_MAPPING'] = True
        return resolve_anime_ids(ids, season, self.config, self.session, media_type=media_type)

    def fixture_routes(self, xml=None, bridge=None):
        def get(url, **kwargs):
            if url.endswith('anime-list.xml'):
                return Response(xml if xml is not None else self.XML)
            if url.endswith('anime-offline-database-reduced.json'):
                return Response(bridge if bridge is not None else self.BRIDGE)
            raise AssertionError('Unexpected API request: ' + url)
        self.session.get.side_effect = get

    def test_tvdb_season_resolves_distinct_entry_not_first_show_id(self):
        self.fixture_routes()
        ids, notices = self.resolver({'tvdb': '100'})
        self.assertEqual(ids, {'tvdb': '100', 'anidb': '20', 'mal': '120', 'anilist': '220'})
        self.assertEqual(notices, [])
        self.assertTrue(all(not c.kwargs['allow_redirects'] for c in self.session.get.call_args_list))

    def test_tmdb_uses_its_own_season_not_tvdb_season_number(self):
        self.fixture_routes()
        ids, notices = self.resolver({'tmdb': '200'}, season=3)
        self.assertEqual(ids['anidb'], '20')
        self.assertFalse(notices)

    def test_split_cour_same_tvdb_season_is_ambiguous(self):
        self.fixture_routes(xml=self.XML.replace('defaulttvdbseason="2"', 'defaulttvdbseason="1"'))
        ids, notices = self.resolver({'tvdb': '100'}, season=1)
        self.assertNotIn('anidb', ids)
        self.assertIn('set ID overrides', str(notices))
        self.assertEqual(self.session.get.call_count, 1)

    def test_absolute_a_default_requires_override(self):
        self.fixture_routes(xml=self.XML.replace('defaulttvdbseason="2"', 'defaulttvdbseason="a"'))
        ids, notices = self.resolver({'tvdb': '100'})
        self.assertNotIn('anidb', ids)
        self.assertIn('set ID overrides', str(notices))

    def test_episode_mapping_season_can_resolve_when_default_is_different(self):
        xml = '''<anime-list><anime anidbid="20" tvdbid="100" defaulttvdbseason="1">
        <mapping-list><mapping anidbseason="1" tvdbseason="2" start="13" end="24" offset="-12"/>
        </mapping-list></anime></anime-list>'''
        self.fixture_routes(xml=xml)
        ids, notices = self.resolver({'tvdb': '100'})
        self.assertEqual(ids['anidb'], '20')
        self.assertFalse(notices)

    def test_movie_tmdb_and_show_tmdb_namespaces_are_distinct(self):
        self.fixture_routes(xml='''<anime-list>
        <anime anidbid="10" tmdbid="200" imdbid="tt1234567"/>
        <anime anidbid="20" tmdbtv="200" tmdbseason="1"/>
        </anime-list>''')
        ids, notices = self.resolver({'tmdb': '200'}, season=None, media_type='movie')
        self.assertEqual(ids['anidb'], '10')
        self.assertFalse(notices)

    def test_imdb_show_only_is_not_mistaken_for_a_season_crosswalk(self):
        self.fixture_routes()
        ids, notices = self.resolver({'imdb': 'tt1234567'})
        self.assertNotIn('anidb', ids)
        self.assertIn('set ID overrides', str(notices))
        self.session.get.assert_not_called()

    def test_explicit_anime_entry_wins_over_tvdb_mapping(self):
        self.fixture_routes()
        self.session.post.return_value = Response({'data': {'Media': {
            'id': 210, 'idMal': 110, 'coverImage': {}}}})
        ids, notices = self.resolver({'tvdb': '100', 'anidb': '10', 'anilist': '210', 'mal': '110'})
        self.assertEqual(ids['anidb'], '10')
        self.assertEqual(ids['mal'], '110')
        self.session.get.assert_not_called()

    def test_mal_to_anilist_uses_official_idmal_lookup(self):
        self.fixture_routes()
        self.session.post.return_value = Response({'data': {'Media': {
            'id': 220, 'idMal': 120, 'coverImage': {}}}})
        ids, notices = self.resolver({'mal': '120'})
        self.assertEqual(ids['anilist'], '220')
        self.assertEqual(ids['anidb'], '20')
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.session.post.call_args.kwargs['json']['variables'], {'idMal': 120})
        self.assertFalse(notices)

    def test_anilist_to_mal_uses_official_idmal_field(self):
        self.fixture_routes(bridge=[{'anidb_id': 20, 'anilist_id': 220}])
        self.session.post.return_value = Response({'data': {'Media': {
            'id': 220, 'idMal': 120, 'coverImage': {}}}})
        ids, notices = self.resolver({'anilist': '220'})
        self.assertEqual(ids['mal'], '120')
        self.assertEqual(ids['anidb'], '20')
        self.assertEqual(self.session.post.call_count, 1)
        self.assertFalse(notices)

    def test_fetch_season_maps_then_fetches_only_that_entry(self):
        from anime_providers import fetch_artwork
        self.config['ANIME_AUTO_MAPPING'] = True
        self.fixture_routes()
        self.session.post.return_value = Response({'data': {'Media': {
            'id': 220, 'idMal': 120, 'coverImage': {
                'extraLarge': 'https://s4.anilist.co/file/anilistcdn/season-two.jpg'}}}})
        artwork, notices = fetch_artwork(self.context({'tvdb': '100'}, 'season', 2),
                                         self.config, self.session)
        self.assertEqual([item.provider for item in artwork], ['anilist'])
        self.assertEqual(self.session.post.call_args.kwargs['json']['variables'], {'id': 220})
        self.assertIn('ANIDB_CLIENT', str(notices))
        self.assertIn('MAL_CLIENT_ID', str(notices))

    def test_pair_lookup_cache_avoids_duplicate_poster_request(self):
        from anime_providers import fetch_artwork
        self.config['ANIME_AUTO_MAPPING'] = True
        self.fixture_routes()
        self.session.post.return_value = Response({'data': {'Media': {
            'id': 220, 'idMal': 120, 'coverImage': {
                'large': 'https://s4.anilist.co/file/anilistcdn/season-two.jpg'}}}})
        artwork, _ = fetch_artwork(self.context({'anilist': '220'}), self.config, self.session)
        self.assertTrue(artwork)
        self.assertEqual(self.session.post.call_count, 1)

    def test_conflicting_official_pair_discards_derived_posters(self):
        from anime_providers import fetch_artwork
        self.config['ANIME_AUTO_MAPPING'] = True
        self.fixture_routes()
        self.session.post.return_value = Response({'data': {'Media': {
            'id': 220, 'idMal': 999, 'coverImage': {
                'large': 'https://s4.anilist.co/file/anilistcdn/wrong-entry.jpg'}}}})
        artwork, notices = fetch_artwork(self.context({'anidb': '20'}), self.config, self.session)
        self.assertFalse(artwork)
        self.assertIn('set ID overrides', str(notices))

    def test_missing_season_number_requires_an_exact_override(self):
        self.fixture_routes(xml='''<anime-list>
            <anime anidbid="20" tvdbid="100" defaulttvdbseason="1"/>
            </anime-list>''')
        ids, notices = self.resolver({'tvdb': '100'}, season=None)
        self.assertNotIn('anidb', ids)
        self.assertIn('set ID overrides', str(notices))
        self.session.get.assert_not_called()

    def test_show_with_multiple_anime_entries_is_not_assumed_season_one(self):
        self.fixture_routes()
        ids, notices = self.resolver({'tvdb': '100'}, season=None, media_type='show')
        self.assertNotIn('anidb', ids)
        self.assertIn('set ID overrides', str(notices))

    def test_conflicting_tvdb_and_tmdb_mappings_are_not_guessed(self):
        self.fixture_routes(xml=self.XML.replace('tmdbseason="1"', 'tmdbseason="2"'))
        ids, notices = self.resolver({'tvdb': '100', 'tmdb': '200'}, season=2)
        self.assertNotIn('anidb', ids)
        self.assertIn('set ID overrides', str(notices))

    def test_duplicate_bridge_entries_do_not_silently_select_first(self):
        self.fixture_routes(bridge=[{'anidb_id': 20, 'mal_id': 120},
                                    {'anidb_id': 20, 'mal_id': 121}])
        ids, notices = self.resolver({'tvdb': '100'})
        self.assertEqual(ids['anidb'], '20')
        self.assertNotIn('mal', ids)
        self.assertIn('set ID overrides', str(notices))

    def test_disabled_mapping_preserves_ids_without_network(self):
        from anime_providers import resolve_anime_ids
        ids, notices = resolve_anime_ids({'tvdb': '100'}, 2, self.config, self.session)
        self.assertEqual(ids, {'tvdb': '100'})
        self.assertFalse(notices)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()


class AnimeGatingTests(unittest.TestCase):
    def test_non_anime_context_does_not_contact_any_service(self):
        from anime_providers import fetch_artwork
        session = Mock()
        result = fetch_artwork(ArtworkContext('show', {'anilist': '1'}), {}, session)
        self.assertEqual(result, ([], []))
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_episode_never_uses_anime_cover_as_titlecard(self):
        from anime_providers import fetch_artwork
        session = Mock()
        result = fetch_artwork(ArtworkContext('episode', {'anilist': '1'}, is_anime=True), {}, session)
        self.assertEqual(result, ([], []))
        session.get.assert_not_called()
        session.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()

import importlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


# Never load the instance configuration or start its scheduler during tests.
with tempfile.TemporaryDirectory(prefix='poster-tests-') as data_dir:
    with patch.dict(os.environ, {'DATA_DIR': data_dir}), patch('threading.Thread.start'):
        downloader = importlib.import_module('plex_poster_downloader')


class UIFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix='poster-tests-')
        self.addCleanup(self.data_dir.cleanup)
        self.cfg = dict(downloader.DEFAULT_CONFIG, AUTH_DISABLED=True,
                        DOWNLOAD_BASE_DIR=self.data_dir.name, PLEX_TOKEN='',
                        HISTORY_FILE=os.path.join(self.data_dir.name, 'history.json'))
        self.cfg_patch = patch.object(downloader, 'get_config', side_effect=lambda: self.cfg.copy())
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)
        self.plex = SimpleNamespace(library=SimpleNamespace(sections=lambda: []), friendlyName='Test Plex')
        self.plex_patch = patch.object(downloader, 'plex', self.plex)
        self.plex_patch.start()
        self.addCleanup(self.plex_patch.stop)
        self.client = downloader.app.test_client()

    def make_item(self, kind='movie', key=2):
        posters = [SimpleNamespace(provider=provider, key=f'https://example.com/{name}.jpg')
                   for provider, name in [(None, 'uploaded'), ('upload', 'explicit-upload'),
                                          ('tmdb', 'remote'), ('local', 'local')]]
        self.lib = SimpleNamespace(title='Library', key=1, search=lambda **kwargs: [])
        item = SimpleNamespace(type=kind, ratingKey=key, title=f'Title {key}',
                               locations=['/media/Title/movie.mkv'], section=lambda: self.lib,
                               posters=lambda: posters, arts=lambda: [SimpleNamespace(
                                   provider=None, key='https://example.com/background.jpg')],
                               seasons=lambda: [], episodes=lambda: [], index=1)
        show = SimpleNamespace(type='show', title='Parent Show', ratingKey=10,
                               locations=['/media/My Show/'], section=lambda: self.lib, seasons=lambda: [])
        item.show = lambda: show
        season = SimpleNamespace(type='season', title='Season 1', ratingKey=20, index=1, episodes=lambda: [])
        item.season = lambda: season
        item.parentIndex = 1
        self.plex.fetchItem = lambda key: item
        return item

    def test_uploaded_posters_hidden_only_when_enabled(self):
        for kind in ('movie', 'show', 'season'):
            self.make_item(kind)
            for enabled in (True, False):
                with self.subTest(kind=kind, enabled=enabled):
                    self.cfg['HIDE_UPLOADED_POSTERS'] = enabled
                    path = '/season/2' if kind == 'season' else '/item/2'
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    for name in ('uploaded', 'explicit-upload'):
                        self.assertEqual(f'https://example.com/{name}.jpg' in html, not enabled,
                                         f'{kind}: uploaded poster visibility')
                    for name in ('remote', 'local', 'background'):
                        self.assertIn(f'https://example.com/{name}.jpg', html)

    def test_all_uploaded_posters_has_empty_state(self):
        item = self.make_item()
        item.posters = lambda: [SimpleNamespace(provider=None, key='https://example.com/upload.jpg')]
        self.cfg['HIDE_UPLOADED_POSTERS'] = True
        self.assertIn('No posters available', self.client.get('/item/2').get_data(as_text=True))

    def test_episode_paths_for_both_asset_styles_and_specials(self):
        episode = self.make_item('episode')
        episode.show().type = 'show'
        episode.show().locations = ['/media/My Show/']
        for season_number, episode_number in ((1, 2), (0, 1), (12, 105)):
            episode.parentIndex = season_number
            episode.index = episode_number
            name = f'S{season_number:02d}E{episode_number:02d}'
            for style in ('ASSET_FOLDERS', 'NO_ASSET_FOLDERS'):
                for img_type in ('poster', 'background'):
                    with self.subTest(season=season_number, episode=episode_number,
                                      style=style, img_type=img_type):
                        suffix = '' if img_type == 'poster' else '_background'
                        parts = ('My Show', f'{name}{suffix}.jpg') if style == 'ASSET_FOLDERS' else (f'My Show_{name}{suffix}.jpg',)
                        expected = os.path.join(self.data_dir.name, 'Library', *parts)
                        self.assertEqual(downloader.get_target_file_path(
                            episode, 'Library', style=style, img_type=img_type), expected)

    def test_previous_next_follow_library_order_including_page_boundaries(self):
        from unittest.mock import Mock
        for kind in ('movie', 'show'):
            for key in (1, 50, 51, 52):
                with self.subTest(kind=kind, key=key):
                    self.make_item(kind, key)
                    self.lib.search = Mock(return_value=[SimpleNamespace(ratingKey=k, title=f'Title {k}')
                                                         for k in range(1, 53)])
                    html = self.client.get(f'/item/{key}').get_data(as_text=True)
                    self.assertIn('aria-label="Item navigation"', html)
                    if key > 1:
                        self.assertIn(f'href="/item/{key - 1}" rel="prev"', html)
                    else:
                        self.assertIn('aria-disabled="true">&larr; Previous', html)
                    if key < 52:
                        self.assertIn(f'href="/item/{key + 1}" rel="next"', html)
                    else:
                        self.assertIn('aria-disabled="true">Next &rarr;', html)
                    self.lib.search.assert_called_once_with(sort='titleSort', libtype=kind)

    def test_season_page_links_episode_titlecards(self):
        season = self.make_item('season')
        episode = SimpleNamespace(ratingKey=31, index=2, title='<b>Episode</b> {{ 1+1 }}', thumb=None)
        season.episodes = lambda: [episode]
        html = self.client.get('/season/2').get_data(as_text=True)
        self.assertIn('href="/episode/31"', html)
        self.assertIn('Episode Titlecards', html)
        self.assertNotIn('<b>Episode</b>', html)

    def test_episode_page_filters_uploads_and_uses_landscape_cards(self):
        self.make_item('episode')
        for enabled in (True, False):
            self.cfg['HIDE_UPLOADED_POSTERS'] = enabled
            response = self.client.get('/episode/2')
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('Titlecards', html)
            self.assertIn('class="background-grid"', html)
            self.assertIn('name="img_type" value="poster"', html)
            self.assertEqual('https://example.com/uploaded.jpg' in html, not enabled)
            self.assertIn('https://example.com/remote.jpg', html)
            self.assertIn('/season/20', html)

    def test_season_and_episode_navigation_uses_numeric_order_within_parent(self):
        for kind, path in (('season', '/season/2'), ('episode', '/episode/2')):
            for key, has_prev, has_next in ((1, False, True), (2, True, True), (3, True, False)):
                with self.subTest(kind=kind, key=key):
                    item = self.make_item(kind, key)
                    siblings = [SimpleNamespace(ratingKey=k, index=k, title=f'Title {k}') for k in (3, 1, 2)]
                    if kind == 'season':
                        item.show().seasons = lambda: siblings
                    else:
                        item.season().episodes = lambda: siblings
                    html = self.client.get(path.replace('/2', f'/{key}')).get_data(as_text=True)
                    prefix = '/season/' if kind == 'season' else '/episode/'
                    if has_prev:
                        self.assertIn(f'href="{prefix}{key - 1}" rel="prev"', html)
                    else:
                        self.assertIn('aria-disabled="true">&larr; Previous', html)
                    if has_next:
                        self.assertIn(f'href="{prefix}{key + 1}" rel="next"', html)
                    else:
                        self.assertIn('aria-disabled="true">Next &rarr;', html)

    def test_episode_routes_handle_wrong_types_and_missing_items(self):
        from unittest.mock import Mock
        self.make_item('movie')
        self.assertEqual(self.client.get('/episode/2').status_code, 404)
        self.assertEqual(self.client.get('/season/2').status_code, 404)
        self.plex.fetchItem = Mock(side_effect=downloader.NotFound('Missing'))
        self.assertEqual(self.client.get('/episode/999').status_code, 404)
        self.assertEqual(self.client.get('/season/999').status_code, 404)
        self.assertEqual(self.client.get('/episode/not-an-id').status_code, 404)

    def test_manual_season_and_episode_downloads_save_file_history_and_return_to_picker(self):
        from pathlib import Path
        from unittest.mock import Mock
        for kind in ('season', 'episode'):
            with self.subTest(kind=kind):
                item = self.make_item(kind)
                image_key = 'https://example.com/remote.jpg'
                response_data = SimpleNamespace(status_code=200, iter_content=lambda size: [b'actual-artwork'])
                with patch.object(downloader, 'validate_image_url', return_value=True), \
                        patch.object(downloader.requests, 'get', Mock(return_value=response_data)):
                    response = self.client.post('/download', data={
                        'rating_key': '2', 'poster_key': image_key, 'img_type': 'poster'},
                        headers={'Referer': f'http://localhost/{kind}/2'})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, f'/{kind}/2')
                self.assertEqual(Path(downloader.get_target_file_path(item)).read_bytes(), b'actual-artwork')
                self.assertEqual(downloader.get_history_url('2'), image_key)
                self.assertIn('CURRENT', self.client.get(f'/{kind}/2').get_data(as_text=True))

    def test_episode_download_rejects_unlisted_and_blocked_image_keys(self):
        from unittest.mock import Mock
        self.make_item('episode')
        for image_key, allowed in (('https://evil.example/image.jpg', True),
                                   ('https://example.com/remote.jpg', False)):
            with patch.object(downloader, 'validate_image_url', return_value=allowed), \
                    patch.object(downloader.requests, 'get', Mock()) as http:
                response = self.client.post('/download', data={
                    'rating_key': '2', 'poster_key': image_key, 'img_type': 'poster'},
                    headers={'Referer': 'http://localhost/episode/2'})
                self.assertEqual(response.location, '/episode/2')
                http.assert_not_called()
                self.assertIsNone(downloader.get_history_url('2'))

    def test_episode_page_has_empty_state_and_clears_stale_selection(self):
        item = self.make_item('episode')
        downloader.save_download_history(2, 'https://example.com/remote.jpg')
        html = self.client.get('/episode/2').get_data(as_text=True)
        self.assertNotIn('>CURRENT<', html)
        item.posters = lambda: []
        self.assertIn('No titlecards available', self.client.get('/episode/2').get_data(as_text=True))

    def test_episode_page_requires_authentication_and_handles_disconnected_plex(self):
        self.make_item('episode')
        self.cfg.update(AUTH_DISABLED=False, AUTH_USER='test')
        self.assertEqual(self.client.get('/episode/2').location, '/login')
        self.cfg['AUTH_DISABLED'] = True
        with patch.object(downloader, 'plex', None):
            self.assertEqual(self.client.get('/episode/2').location, '/settings')

    def test_single_item_and_missing_item_disable_navigation(self):
        item = self.make_item()
        for results in ([item], []):
            self.lib.search = lambda **kwargs: results
            html = self.client.get('/item/2').get_data(as_text=True)
            self.assertIn('aria-disabled="true">&larr; Previous', html)
            self.assertIn('aria-disabled="true">Next &rarr;', html)

    def test_navigation_failure_does_not_block_poster_page(self):
        self.make_item()
        from unittest.mock import Mock
        self.lib.search = Mock(side_effect=downloader.requests.ConnectionError('Offline'))
        response = self.client.get('/item/2')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Unable to load item navigation', response.get_data(as_text=True))

    def test_navigation_titles_are_escaped(self):
        item = self.make_item()
        other = SimpleNamespace(ratingKey=3, title='<script>alert(1)</script> {{ 7*7 }}')
        self.lib.search = lambda **kwargs: [item, other]
        html = self.client.get('/item/2').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt; {{ 7*7 }}', html)

    def test_uploaded_poster_setting_is_displayed_and_saved(self):
        response = self.client.get('/settings')
        self.assertIn(b'name="hide_uploaded_posters"', response.data)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                form = {'action': 'update_config'}
                if enabled:
                    form['hide_uploaded_posters'] = 'on'
                with patch.object(downloader, 'CONFIG_FILE', os.path.join(self.data_dir.name, 'config.json')), \
                        patch.object(downloader, 'init_plex', return_value=True):
                    response = self.client.post('/settings', data=form)
                self.assertEqual(response.status_code, 302)
                import json
                with open(os.path.join(self.data_dir.name, 'config.json')) as saved:
                    self.assertIs(json.load(saved)['HIDE_UPLOADED_POSTERS'], enabled)
                self.cfg['HIDE_UPLOADED_POSTERS'] = enabled
                html = self.client.get('/settings').get_data(as_text=True)
                from html.parser import HTMLParser

                class Inputs(HTMLParser):
                    def handle_starttag(parser, tag, attrs):
                        attrs = dict(attrs)
                        if tag == 'input' and attrs.get('name') == 'hide_uploaded_posters':
                            parser.checked = 'checked' in attrs

                inputs = Inputs()
                inputs.feed(html)
                self.assertIs(inputs.checked, enabled)

    def test_schedule_child_artwork_options_are_saved_and_rendered(self):
        import json
        for enabled in (True, False):
            form = {'action': 'update_config'}
            if enabled:
                form.update(cron_download_seasons='on', cron_download_titlecards='on')
            config_path = os.path.join(self.data_dir.name, 'config.json')
            with patch.object(downloader, 'CONFIG_FILE', config_path), \
                    patch.object(downloader, 'init_plex', return_value=True):
                self.assertEqual(self.client.post('/settings', data=form).status_code, 302)
            with open(config_path) as config_file:
                saved = json.load(config_file)
            for key, field in (('CRON_DOWNLOAD_SEASONS', 'cron_download_seasons'),
                               ('CRON_DOWNLOAD_TITLECARDS', 'cron_download_titlecards')):
                self.assertIs(saved.get(key), enabled)
                self.cfg[key] = enabled
                html = self.client.get('/settings').get_data(as_text=True)
                self.assertIn(f'name="{field}"', html)


if __name__ == '__main__':
    unittest.main()

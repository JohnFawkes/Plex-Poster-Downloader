import json
import os
import unittest
from unittest.mock import patch

import test_ui_features as ui


downloader = ui.downloader
REAL_GET_CONFIG = downloader.get_config


class ProviderIntegrationTests(unittest.TestCase):
    setUp = ui.UIFeaturesTest.setUp
    make_item = ui.UIFeaturesTest.make_item

    def test_api_credentials_are_encrypted_on_disk_and_masked_in_settings(self):
        keys = ('TMDB_API_KEY', 'TVDB_API_KEY', 'TVDB_PIN', 'FANART_API_KEY', 'MAL_CLIENT_ID')
        self.cfg.update({key: 'private-value-for-test' for key in keys})
        path = os.path.join(self.data_dir.name, 'config.json')
        with patch.object(downloader, 'CONFIG_FILE', path), \
                patch.object(downloader, 'KEY_FILE', os.path.join(self.data_dir.name, '.secret.key')):
            downloader.save_config(self.cfg)
            with open(path) as config_file:
                persisted = json.load(config_file)
            for key in keys:
                self.assertNotEqual(persisted[key], self.cfg[key], key)
                self.assertEqual(REAL_GET_CONFIG()[key], self.cfg[key])
        html = self.client.get('/settings').get_data(as_text=True)
        self.assertNotIn('private-value-for-test', html)
        for field in ('tmdb_api_key', 'tvdb_api_key', 'fanart_api_key', 'mal_client_id'):
            self.assertIn(f'name="{field}"', html)

    def test_settings_save_anime_libraries_and_preserve_or_clear_keys(self):
        self.cfg['TMDB_API_KEY'] = 'keep-existing-key'
        with patch.object(downloader, 'save_config') as save, patch.object(downloader, 'init_plex', return_value=True):
            response = self.client.post('/settings', data={'action': 'update_config', 'tmdb_api_key': '',
                                                          'anime_libs': ['Library'], 'anime_auto_mapping': 'on'})
            self.assertEqual(response.status_code, 302)
            saved = save.call_args.args[0]
            self.assertEqual(saved.get('ANIME_LIBRARIES'), ['Library'])
            self.assertEqual(saved['TMDB_API_KEY'], 'keep-existing-key')
            self.client.post('/settings', data={'action': 'update_config', 'clear_tmdb_api_key': 'on'})
            self.assertEqual(save.call_args.args[0]['TMDB_API_KEY'], '')

    def test_clearlogo_paths_and_history_do_not_collide_with_backgrounds(self):
        for kind in ('movie', 'show', 'season', 'episode'):
            item = self.make_item(kind)
            for style in ('ASSET_FOLDERS', 'NO_ASSET_FOLDERS'):
                path = downloader.get_target_file_path(item, 'Library', style=style, img_type='clearlogo')
                self.assertTrue(path.endswith('.png'), path)
                self.assertNotEqual(path, downloader.get_target_file_path(item, 'Library', style=style, img_type='background'))
        downloader.save_download_history(2, 'https://example.com/logo.png', 'clearlogo')
        downloader.save_download_history(2, 'https://example.com/bg.jpg', 'background')
        self.assertEqual(downloader.get_history_url(2, 'clearlogo'), 'https://example.com/logo.png')
        self.assertEqual(downloader.get_history_url(2, 'background'), 'https://example.com/bg.jpg')

    def test_badges_distinguish_direct_and_plex_artwork(self):
        self.assertTrue(hasattr(downloader, 'artwork_badge'), 'Origin-specific badge helper missing')
        from artwork_common import Artwork
        from types import SimpleNamespace
        self.assertEqual(downloader.artwork_badge(SimpleNamespace(provider='tmdb')), 'tmdb_plex')
        self.assertEqual(downloader.artwork_badge(Artwork('https://image.tmdb.org/t/p/original/x.jpg', 'tmdb')), 'tmdb')

    def test_anime_season_context_does_not_inherit_whole_show_anime_ids(self):
        self.assertTrue(hasattr(downloader, 'artwork_context'), 'ID context resolver missing')
        from types import SimpleNamespace
        season = self.make_item('season')
        season.index = 2
        season.show().guids = [SimpleNamespace(id='tvdb://100'), SimpleNamespace(id='anilist://200')]
        self.cfg['ANIME_LIBRARIES'] = ['Library']
        self.cfg['ARTWORK_ID_OVERRIDES'] = {'2': {'mal': '300', 'anidb': '400'}}
        ctx = downloader.artwork_context(season, self.cfg)
        self.assertTrue(ctx.is_anime)
        self.assertEqual(ctx.ids, {'tvdb': '100', 'mal': '300', 'anidb': '400'})
        self.assertEqual(ctx.season_number, 2)

    def test_mapping_form_saves_season_ids_and_rejects_malformed_identifiers(self):
        self.make_item('season')
        with patch.object(downloader, 'save_config') as save:
            response = self.client.post('/artwork-mapping/2', data={'tvdb': '100', 'mal': '300'})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, '/season/2')
            self.assertEqual(save.call_args.args[0]['ARTWORK_ID_OVERRIDES']['2'], {'tvdb': '100', 'mal': '300'})
        for invalid in ('https://example.com/123', '123/../../bad', '-1', ' 123 ', '01'):
            with patch.object(downloader, 'save_config') as save:
                self.assertEqual(self.client.post('/artwork-mapping/2', data={'mal': invalid}).status_code, 400)
                save.assert_not_called()

    def test_combined_picker_displays_direct_and_plex_sources_with_clearlogo_tab(self):
        self.assertTrue(hasattr(downloader, 'direct_artwork'), 'Direct provider integration missing')
        from artwork_common import Artwork
        self.make_item('movie')
        images = [Artwork('https://image.tmdb.org/t/p/original/poster.jpg', 'tmdb'),
                  Artwork('https://assets.fanart.tv/fanart/logo.png', 'fanart.tv', 'clearlogo'),
                  Artwork('https://artworks.thetvdb.com/banners/bg.jpg', 'tvdb', 'background')]
        with patch.object(downloader, 'direct_artwork', return_value=(images, ['Provider setup notice'])):
            html = self.client.get('/item/2').get_data(as_text=True)
        self.assertIn('>tmdb_plex<', html)
        self.assertIn('>tmdb<', html)
        self.assertIn('>fanart.tv<', html)
        self.assertIn('Clearlogos', html)
        self.assertIn('name="img_type" value="clearlogo"', html)
        self.assertIn('Provider setup notice', html)
        self.assertIn('Artwork IDs', html)

    def test_direct_download_whitelists_opaque_keys_and_preserves_logo_transparency(self):
        from io import BytesIO
        from pathlib import Path
        from PIL import Image
        from artwork_common import Artwork
        from types import SimpleNamespace
        from unittest.mock import Mock
        item = self.make_item('movie')
        logo = Artwork('https://assets.fanart.tv/fanart/logo.png', 'fanart.tv', 'clearlogo')
        image_data = BytesIO()
        Image.new('RGBA', (3, 2), (255, 0, 0, 0)).save(image_data, format='PNG')
        response = SimpleNamespace(status_code=200, iter_content=lambda size: [image_data.getvalue()])
        with patch.object(downloader, 'direct_artwork', return_value=([logo], [])), \
                patch.object(downloader, 'validate_image_url', return_value=True), \
                patch.object(downloader.requests, 'get', Mock(return_value=response)) as http:
            result = self.client.post('/download', data={'rating_key': '2', 'img_type': 'clearlogo', 'poster_key': logo.key})
            self.assertEqual(result.status_code, 302)
            path = Path(downloader.get_target_file_path(item, img_type='clearlogo'))
            self.assertTrue(path.exists(), 'Direct clearlogo was not downloaded')
            with Image.open(path) as saved:
                self.assertEqual(saved.format, 'PNG')
                self.assertEqual(saved.getpixel((0, 0))[3], 0)
            self.assertEqual(downloader.get_history_url(2, 'clearlogo'), logo.url)
            self.assertFalse(http.call_args.kwargs.get('allow_redirects', True))
            http.reset_mock()
            self.client.post('/download', data={'rating_key': '2', 'img_type': 'poster', 'poster_key': logo.key})
            http.assert_not_called()

    def test_invalid_logo_and_stream_failure_do_not_overwrite_existing_file(self):
        from pathlib import Path
        from artwork_common import Artwork
        from types import SimpleNamespace
        logo = Artwork('https://assets.fanart.tv/fanart/logo.png', 'fanart.tv', 'clearlogo')
        item = self.make_item('movie')
        path = Path(downloader.get_target_file_path(item, img_type='clearlogo'))
        path.parent.mkdir(parents=True)
        path.write_bytes(b'keep-existing')
        response = SimpleNamespace(status_code=200, iter_content=lambda size: [b'not-an-image'])
        with patch.object(downloader, 'direct_artwork', return_value=([logo], [])), \
                patch.object(downloader, 'validate_image_url', return_value=True), \
                patch.object(downloader.requests, 'get', return_value=response):
            self.client.post('/download', data={'rating_key': '2', 'img_type': 'clearlogo', 'poster_key': logo.key})
        self.assertEqual(path.read_bytes(), b'keep-existing')
        self.assertIsNone(downloader.get_history_url(2, 'clearlogo'))

    def test_download_rejects_unknown_asset_type_before_request(self):
        self.make_item('movie')
        with patch.object(downloader.requests, 'get') as http:
            result = self.client.post('/download', data={'rating_key': '2', 'img_type': '../../bad', 'poster_key': 'x'})
            self.assertEqual(result.status_code, 400)
            http.assert_not_called()

    def test_unsupported_plex_logos_do_not_hide_other_artwork(self):
        from plexapi.exceptions import BadRequest
        from unittest.mock import Mock
        item = self.make_item('movie')
        item.logos = Mock(side_effect=BadRequest('Unsupported'))
        response = self.client.get('/item/2')
        self.assertEqual(response.status_code, 200)
        self.assertIn('https://example.com/remote.jpg', response.get_data(as_text=True))

    def test_direct_metadata_cache_is_scoped_to_ids_and_credentials(self):
        self.make_item('movie')
        self.cfg['TMDB_API_KEY'] = 'test-only-key'
        downloader._artwork_cache.clear()
        with patch('artwork_providers.fetch_artwork', return_value=([], [])) as standard, \
                patch('anime_providers.fetch_artwork', return_value=([], [])):
            downloader.direct_artwork(self.plex.fetchItem(2))
            downloader.direct_artwork(self.plex.fetchItem(2))
            self.assertEqual(standard.call_count, 1)
            self.cfg['TMDB_API_KEY'] = 'changed-test-only-key'
            downloader.direct_artwork(self.plex.fetchItem(2))
            self.assertEqual(standard.call_count, 2)
            self.cfg['ARTWORK_ID_OVERRIDES'] = {'2': {'tmdb': '123'}}
            downloader.direct_artwork(self.plex.fetchItem(2))
            self.assertEqual(standard.call_count, 3)

    def test_clearlogos_are_counted_separately_from_posters_in_stats(self):
        from pathlib import Path
        from types import SimpleNamespace
        lib = SimpleNamespace(type='movie', title='Library', totalSize=1)
        directory = Path(self.data_dir.name, 'Library', 'Movie')
        directory.mkdir(parents=True)
        for filename in ('poster.jpg', 'background.jpg', 'clearlogo.png'):
            (directory / filename).write_bytes(b'x')
        stats = downloader.get_library_stats(lib)
        self.assertEqual(stats['downloaded_count'], 1)
        self.assertEqual(stats.get('clearlogo_count'), 1)

    def test_atomic_download_preserves_existing_asset_permissions(self):
        from pathlib import Path
        from types import SimpleNamespace
        import stat
        path = Path(self.data_dir.name, 'poster.jpg')
        path.write_bytes(b'old')
        path.chmod(0o664)
        response = SimpleNamespace(status_code=200, iter_content=lambda size: [b'new'])
        self.assertTrue(downloader.save_image_response(response, str(path), 'poster'))
        self.assertEqual(path.read_bytes(), b'new')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o664)

    def test_new_asset_permissions_honor_process_umask(self):
        from pathlib import Path
        from types import SimpleNamespace
        import stat
        path = Path(self.data_dir.name, 'poster.jpg')
        response = SimpleNamespace(status_code=200, iter_content=lambda size: [b'new'])
        old_umask = os.umask(0o027)
        try:
            downloader.save_image_response(response, str(path), 'poster')
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_interrupted_download_keeps_file_history_and_removes_temporary_file(self):
        from pathlib import Path
        from types import SimpleNamespace
        from artwork_common import Artwork
        item = self.make_item('movie')
        image = Artwork('https://image.tmdb.org/t/p/original/poster.jpg', 'tmdb')
        path = Path(downloader.get_target_file_path(item))
        path.parent.mkdir(parents=True)
        path.write_bytes(b'original')
        downloader.save_download_history(2, 'original-history')

        def interrupted(size):
            yield b'partial'
            raise downloader.requests.ConnectionError('Interrupted')

        response = SimpleNamespace(status_code=200, iter_content=interrupted)
        with patch.object(downloader, 'direct_artwork', return_value=([image], [])), \
                patch.object(downloader, 'validate_image_url', return_value=True), \
                patch.object(downloader.requests, 'get', return_value=response):
            self.client.post('/download', data={'rating_key': '2', 'img_type': 'poster', 'poster_key': image.key})
        self.assertEqual(path.read_bytes(), b'original')
        self.assertEqual(downloader.get_history_url(2), 'original-history')
        self.assertEqual(list(path.parent.glob('.artwork-*')), [])

    def test_picker_calls_real_provider_adapter_with_mocked_http(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from test_artwork_providers import Session, TMDB
        item = self.make_item('movie')
        item.guids = [SimpleNamespace(id='tmdb://10')]
        self.cfg['TMDB_API_KEY'] = 'a' * 32
        downloader._artwork_cache.clear()
        transport = Session({('GET', TMDB + '/movie/10/images'): {
            'posters': [{'file_path': '/provider-poster.jpg'}]}})
        http = Mock()
        http.request.side_effect = transport.request
        with patch('artwork_providers.requests.Session', return_value=http):
            response = self.client.get('/item/2')
        self.assertEqual(response.status_code, 200)
        self.assertIn('https://image.tmdb.org/t/p/original/provider-poster.jpg', response.get_data(as_text=True))
        self.assertIn('>tmdb<', response.get_data(as_text=True))
        self.assertEqual(len(transport.calls), 1)
        http.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()

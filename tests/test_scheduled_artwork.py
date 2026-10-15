import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_ui_features import downloader


class ScheduledArtworkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='scheduled-artwork-')
        self.addCleanup(self.directory.cleanup)
        self.cfg = dict(downloader.DEFAULT_CONFIG, DOWNLOAD_BASE_DIR=self.directory.name,
                        HISTORY_FILE=os.path.join(self.directory.name, 'history.json'),
                        CRON_DOWNLOAD_SEASONS=True, CRON_DOWNLOAD_TITLECARDS=True)
        self.lib = SimpleNamespace(title='TV', type='show')
        self.show = self.item('show', 1)
        self.show.locations = ['/media/My Show/']
        self.season = self.item('season', 2)
        self.season.index = 1
        self.episode = self.item('episode', 3)
        self.episode.parentIndex, self.episode.index = 1, 2
        self.show.seasons = Mock(return_value=[self.season])
        self.season.episodes = Mock(return_value=[self.episode])
        self.season.show = self.episode.show = lambda: self.show
        self.lib.all = Mock(return_value=[self.show])
        self.plex = SimpleNamespace(library=SimpleNamespace(sections=lambda: [self.lib]))
        self.http = Mock(return_value=SimpleNamespace(status_code=200, iter_content=lambda size: [b'artwork']))
        for patcher in (patch.object(downloader, 'get_config', side_effect=lambda: self.cfg.copy()),
                        patch.object(downloader, 'plex', self.plex),
                        patch.object(downloader, 'validate_image_url', return_value=True),
                        patch.object(downloader.requests, 'get', self.http)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def item(self, kind, key):
        return SimpleNamespace(type=kind, ratingKey=key, title=f'{kind} {key}',
                               section=lambda: self.lib,
                               posters=Mock(return_value=[SimpleNamespace(
                                   provider='tmdb', key=f'https://example.com/{key}.jpg')]),
                               arts=Mock(return_value=[SimpleNamespace(
                                   provider='tmdb', key=f'https://example.com/{key}-bg.jpg')]))

    def downloaded_files(self):
        return {str(p.relative_to(self.directory.name)) for p in Path(self.directory.name).rglob('*')
                if p.suffix in ('.jpg', '.png')}

    def test_schedule_downloads_show_season_and_episode_artwork(self):
        downloader.run_cron_job()
        self.assertEqual(self.downloaded_files(), {'TV/My Show/poster.jpg', 'TV/My Show/Season01.jpg',
                                                   'TV/My Show/S01E02.jpg'})
        for path in Path(self.directory.name).rglob('*.jpg'):
            self.assertEqual(path.read_bytes(), b'artwork')
        with open(self.cfg['HISTORY_FILE']) as history_file:
            self.assertEqual(set(json.load(history_file)['downloads']), {'1', '2', '3'})

    def test_child_options_are_independent_and_opt_in(self):
        self.cfg['CRON_DOWNLOAD_SEASONS'] = False
        downloader.run_cron_job()
        self.assertEqual(self.downloaded_files(), {'TV/My Show/poster.jpg', 'TV/My Show/S01E02.jpg'})
        self.season.posters.assert_not_called()

    def test_disabling_titlecards_does_not_fetch_episodes(self):
        self.cfg['CRON_DOWNLOAD_TITLECARDS'] = False
        downloader.run_cron_job()
        self.assertEqual(self.downloaded_files(), {'TV/My Show/poster.jpg', 'TV/My Show/Season01.jpg'})
        self.season.episodes.assert_not_called()

    def test_old_configuration_does_not_start_child_downloads(self):
        self.cfg.pop('CRON_DOWNLOAD_SEASONS', None)
        self.cfg.pop('CRON_DOWNLOAD_TITLECARDS', None)
        downloader.run_cron_job()
        self.assertEqual(self.downloaded_files(), {'TV/My Show/poster.jpg'})
        self.show.seasons.assert_not_called()

    def test_existing_parent_assets_do_not_skip_missing_children(self):
        parent = Path(self.directory.name, 'TV', 'My Show', 'poster.jpg')
        parent.parent.mkdir(parents=True)
        parent.write_bytes(b'existing')
        downloader.run_cron_job()
        self.assertEqual(parent.read_bytes(), b'existing')
        self.assertIn('TV/My Show/S01E02.jpg', self.downloaded_files())
        self.assertIn('TV/My Show/Season01.jpg', self.downloaded_files())
        self.show.posters.assert_not_called()
        self.http.reset_mock()
        downloader.run_cron_job()
        self.http.assert_not_called()

    def test_background_option_applies_to_seasons_not_episodes(self):
        self.cfg['CRON_DOWNLOAD_BACKGROUNDS'] = True
        downloader.run_cron_job()
        self.assertEqual(self.downloaded_files(), {'TV/My Show/poster.jpg', 'TV/My Show/Season01.jpg',
                                                   'TV/My Show/S01E02.jpg', 'TV/My Show/background.jpg',
                                                   'TV/My Show/Season01_background.jpg'})
        self.episode.arts.assert_not_called()

    def test_library_filters_apply_to_all_children(self):
        for settings in ({'IGNORED_LIBRARIES': ['TV']}, {'CRON_LIBRARIES': ['Movies']}):
            with self.subTest(settings=settings):
                self.cfg.update(settings)
                downloader.run_cron_job()
                self.http.assert_not_called()
                self.lib.all.assert_not_called()

    def test_provider_modes_apply_to_child_artwork_and_accept_plex_provider_names(self):
        for mode in ('SPECIFIC_PROVIDER', 'RANDOM_PROVIDER'):
            with self.subTest(mode=mode):
                self.cfg.update(CRON_MODE=mode, CRON_PROVIDER='tmdb')
                self.episode.posters.return_value = [SimpleNamespace(
                    provider='com.plexapp.agents.themoviedb', key='https://example.com/episode.jpg')]
                downloader.run_cron_job()
                self.assertIn('TV/My Show/S01E02.jpg', self.downloaded_files())
                for path in Path(self.directory.name).rglob('*.jpg'):
                    path.unlink()

    def test_upload_sources_are_excluded_from_scheduled_child_artwork(self):
        for provider in (None, '', 'upload'):
            with self.subTest(provider=provider):
                self.episode.posters.return_value = [SimpleNamespace(
                    provider=provider, key='https://example.com/upload.jpg')]
                downloader.run_cron_job()
                self.assertNotIn('TV/My Show/S01E02.jpg', self.downloaded_files())

    def test_child_fetch_failure_does_not_stop_other_shows_or_seasons(self):
        broken_show = self.item('show', 4)
        broken_show.locations = ['/media/Broken/']
        broken_show.seasons = Mock(side_effect=downloader.requests.ConnectionError('Unavailable'))
        broken_season = self.item('season', 5)
        broken_season.index = 0
        broken_season.show = lambda: self.show
        broken_season.episodes = Mock(side_effect=downloader.NotFound('Unavailable'))
        self.lib.all.return_value = [broken_show, self.show]
        self.show.seasons.return_value = [broken_season, self.season]
        downloader.run_cron_job()
        self.assertIn('TV/My Show/S01E02.jpg', self.downloaded_files())

    def test_child_download_failures_do_not_write_history_or_block_next_item(self):
        self.http.side_effect = [downloader.requests.ConnectionError('Unavailable'),
                                 SimpleNamespace(status_code=404),
                                 SimpleNamespace(status_code=200, iter_content=lambda size: [b'episode'])]
        downloader.run_cron_job()
        self.assertEqual(self.downloaded_files(), {'TV/My Show/S01E02.jpg'})
        with open(self.cfg['HISTORY_FILE']) as history_file:
            self.assertEqual(set(json.load(history_file)['downloads']), {'3'})

    def test_blocked_child_url_is_not_downloaded(self):
        with patch.object(downloader, 'validate_image_url', side_effect=lambda url: not url.endswith('/3.jpg')):
            downloader.run_cron_job()
        self.assertNotIn('TV/My Show/S01E02.jpg', self.downloaded_files())
        self.assertEqual(self.http.call_count, 2)

    def test_schedule_includes_direct_sources_and_optional_clearlogos(self):
        from artwork_common import Artwork
        from io import BytesIO
        from PIL import Image
        data = BytesIO()
        Image.new('RGBA', (2, 2), (0, 0, 0, 0)).save(data, format='PNG')
        self.http.return_value = SimpleNamespace(status_code=200, iter_content=lambda size: [data.getvalue()])
        self.cfg['CRON_DOWNLOAD_CLEARLOGOS'] = True
        self.show.posters.return_value = []
        self.season.posters.return_value = []
        direct = [Artwork('https://image.tmdb.org/t/p/original/p.jpg', 'tmdb'),
                  Artwork('https://image.tmdb.org/t/p/original/logo.png', 'tmdb', 'clearlogo')]
        with patch.object(downloader, 'direct_artwork', return_value=(direct, [])):
            downloader.run_cron_job()
        self.assertIn('TV/My Show/clearlogo.png', self.downloaded_files())
        self.assertIn('TV/My Show/poster.jpg', self.downloaded_files())
        self.assertIn('TV/My Show/Season01_clearlogo.png', self.downloaded_files())
        self.assertNotIn('TV/My Show/S01E02_clearlogo.png', self.downloaded_files())


if __name__ == '__main__':
    unittest.main()

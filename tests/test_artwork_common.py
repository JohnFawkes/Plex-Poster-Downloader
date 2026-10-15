import importlib.util
import unittest


class ArtworkContractTests(unittest.TestCase):
    def test_direct_artwork_has_stable_opaque_key_and_validates_image_hosts(self):
        self.assertIsNotNone(importlib.util.find_spec('artwork_common'), 'Artwork contract is missing')
        from artwork_common import Artwork, ArtworkContext
        image = Artwork(url='https://image.tmdb.org/t/p/original/poster.jpg', provider='tmdb', asset_type='poster')
        self.assertEqual(image.key, Artwork(url=image.url, provider='tmdb', asset_type='poster').key)
        self.assertTrue(image.key.startswith('direct:tmdb:poster:'))
        self.assertNotIn('https', image.key)
        self.assertEqual(image.origin, 'direct')
        self.assertEqual(ArtworkContext(media_type='season', ids={'tmdb': '1'}, season_number=2).season_number, 2)
        for url in ('http://image.tmdb.org/a.png', 'https://evil.example/a.png',
                    'https://image.tmdb.org@127.0.0.1/a.png', 'https://image.tmdb.org/a.png?api_key=secret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Artwork(url=url, provider='tmdb', asset_type='clearlogo')


if __name__ == '__main__':
    unittest.main()

"""Shared, credential-free contracts for direct artwork providers."""
from dataclasses import dataclass, field
import hashlib
from urllib.parse import urlsplit


IMAGE_HOSTS = {
    'tmdb': ('image.tmdb.org',),
    'tvdb': ('artworks.thetvdb.com', 'www.thetvdb.com', 'thetvdb.com'),
    'fanart.tv': ('assets.fanart.tv',),
    'anilist': ('s4.anilist.co', 's3.anilist.co'),
    'mal': ('cdn.myanimelist.net',),
    'anidb': ('cdn-eu.anidb.net', 'cdn.anidb.net'),
}


@dataclass(frozen=True)
class Artwork:
    url: str
    provider: str
    asset_type: str = 'poster'
    origin: str = field(default='direct', init=False)

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (self.provider not in IMAGE_HOSTS or self.asset_type not in ('poster', 'background', 'clearlogo')
                or parsed.scheme != 'https' or parsed.hostname not in IMAGE_HOSTS[self.provider]
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment):
            raise ValueError('Invalid provider image URL')

    @property
    def key(self):
        digest = hashlib.sha256(self.url.encode()).hexdigest()
        return f'direct:{self.provider}:{self.asset_type}:{digest}'


@dataclass(frozen=True)
class ArtworkContext:
    media_type: str
    ids: dict = field(default_factory=dict)
    season_number: int | None = None
    episode_number: int | None = None
    is_anime: bool = False


class ProviderError(Exception):
    """A sanitized, user-displayable provider failure (never include credentials)."""

import base64

import orjson

from comet.core.logger import log_scraper_error
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


def build_upstream_forward_config(config: dict) -> dict | None:
    """Build a sanitized, credential-free config for forwarding to an upstream
    Comet instance. Returns None if forwarding is disabled. The result tells
    the upstream to run in torrent/p2p mode (no debrid) but to apply this
    user's RTN filters before returning, so we receive a pre-filtered set."""
    if not settings.COMET_SCRAPER_FORWARD_CONFIG:
        return None
    return {
        "cachedOnly": False,
        "sortCachedUncachedTogether": False,
        "removeTrash": config["removeTrash"],
        "resultFormat": ["all"],
        "maxResultsPerResolution": config["maxResultsPerResolution"],
        "maxSize": config["maxSize"],
        "debridService": "torrent",
        "debridApiKey": "",
        "debridServices": [],
        "enableTorrent": True,
        "deduplicateStreams": False,
        "scrapeDebridAccountTorrents": False,
        "debridStreamProxyPassword": "",
        "languages": config["languages"],
        "resolutions": config["resolutions"],
        "options": config["options"],
    }


class CometScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            b64 = None
            if request.forward_config is not None:
                b64 = base64.b64encode(
                    orjson.dumps(request.forward_config)
                ).decode()
            path = (
                f"{self.url}/{b64}/stream/{request.media_type}/{request.media_id}.json"
                if b64
                else f"{self.url}/stream/{request.media_type}/{request.media_id}.json"
            )
            async with self.session.get(path) as response:
                results = await response.json()

            for torrent in results["streams"]:
                title_full = torrent["description"]

                try:
                    title = title_full.split("\n")[0].split("📄 ")[1]
                except Exception:
                    continue

                seeders = (
                    int(title_full.split("👤 ")[1].split(" ")[0])
                    if "👤" in title_full
                    else None
                )

                torrents.append(
                    {
                        "title": title,
                        "infoHash": torrent["infoHash"].lower(),
                        "fileIndex": torrent.get("fileIdx", None),
                        "seeders": seeders,
                        "size": torrent["behaviorHints"].get("videoSize"),
                        "tracker": "CometNet|ElfHosted",
                        "sources": torrent.get("sources", []),
                    }
                )
        except Exception as e:
            log_scraper_error("Comet", self.url, request.media_id, e)

        return torrents

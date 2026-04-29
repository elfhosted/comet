from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel


class ScrapeRequest(BaseModel):
    media_type: str  # "movie" or "series"
    media_id: str  # Full ID (e.g., "tt1234567:1:1" or "kitsu:123")
    media_only_id: str  # Base ID (e.g., "tt1234567")
    title: str
    year: Optional[int] = None
    year_end: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    context: str = "live"  # "live" or "background"
    # Sanitized, credential-free config forwarded to upstream scrapers that
    # accept a b64config (currently only CometScraper). None means no forward.
    forward_config: Optional[Dict[str, Any]] = None


class ScrapeResult(TypedDict):
    title: str
    infoHash: str
    fileIndex: Optional[int]
    seeders: Optional[int]
    size: Optional[int]
    tracker: str
    sources: List[str]

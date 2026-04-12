import hashlib
import uuid

import orjson

from comet.core.logger import logger
from comet.core.models import settings

_redis = None
TORRENT_CACHE_PREFIX = "comet:tc:"
RANKED_CACHE_PREFIX = "comet:rc:"
LOCK_PREFIX = "comet:lock:"
DEBRID_CACHE_PREFIX = "comet:dc:"
FIRST_SEARCH_PREFIX = "comet:fs:"
FRESH_TORRENT_PREFIX = "comet:ft:"
METADATA_CACHE_PREFIX = "comet:mc:"
DEFAULT_TTL = 300


async def setup_redis():
    global _redis
    url = getattr(settings, "REDIS_URL", None)
    if not url:
        return
    try:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(url, decode_responses=False)
        await _redis.ping()
        logger.log("COMET", f"Redis cache connected: {url}")
    except Exception as e:
        logger.warning(f"Redis cache unavailable, falling back to DB only: {e}")
        _redis = None


async def shutdown_redis():
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


_settings_hash_cache = {}


def _settings_hash(rtn_settings, rtn_ranking, max_size, remove_trash, max_results_per_resolution=0):
    # model_dump() is expensive; cache the base hash per (id(settings), id(ranking))
    # and only mix in the cheap scalar params per call.
    s_id = id(rtn_settings)
    r_id = id(rtn_ranking)
    base_key = (s_id, r_id)

    base_bytes = _settings_hash_cache.get(base_key)
    if base_bytes is None:
        base_bytes = orjson.dumps(
            {
                "s": rtn_settings.model_dump() if hasattr(rtn_settings, "model_dump") else str(rtn_settings),
                "r": rtn_ranking.model_dump() if hasattr(rtn_ranking, "model_dump") else str(rtn_ranking),
            },
            option=orjson.OPT_SORT_KEYS,
        )
        _settings_hash_cache[base_key] = base_bytes
        # Evict old entries to avoid unbounded growth
        if len(_settings_hash_cache) > 256:
            _settings_hash_cache.clear()
            _settings_hash_cache[base_key] = base_bytes

    raw = base_bytes + orjson.dumps(
        {"ms": max_size, "mr": max_results_per_resolution, "rt": remove_trash},
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()[:12]


def _torrent_key(media_id: str, season, episode):
    return f"{TORRENT_CACHE_PREFIX}{media_id}:{season}:{episode}"


def _ranked_key(media_id: str, season, episode, settings_hash: str):
    return f"{RANKED_CACHE_PREFIX}{media_id}:{season}:{episode}:{settings_hash}"


def _serialize_torrent_dict(torrents: dict) -> bytes:
    serializable = {}
    for info_hash, t in torrents.items():
        entry = {
            "fileIndex": t.get("fileIndex"),
            "title": t["title"],
            "seeders": t.get("seeders"),
            "size": t.get("size"),
            "tracker": t.get("tracker"),
            "sources": t.get("sources"),
        }
        parsed = t.get("parsed")
        if parsed is not None:
            entry["parsed"] = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
        serializable[info_hash] = entry
    return orjson.dumps(serializable)


def _deserialize_torrent_dict(data: bytes) -> dict:
    from RTN import ParsedData

    raw = orjson.loads(data)
    torrents = {}
    for info_hash, entry in raw.items():
        parsed_dict = entry.pop("parsed", None)
        if parsed_dict is not None:
            entry["parsed"] = ParsedData.model_construct(**parsed_dict)
        else:
            entry["parsed"] = None
        torrents[info_hash] = entry
    return torrents


def _serialize_ranked(ranked_torrents: dict, torrents: dict) -> bytes:
    data = {
        "ranked": list(ranked_torrents.keys()),
        "torrents": {},
    }
    for info_hash in ranked_torrents:
        t = torrents.get(info_hash, {})
        entry = {
            "fileIndex": t.get("fileIndex"),
            "title": t.get("title", ""),
            "seeders": t.get("seeders"),
            "size": t.get("size"),
            "tracker": t.get("tracker"),
            "sources": t.get("sources"),
        }
        parsed = t.get("parsed")
        if parsed is not None:
            entry["parsed"] = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
        data["torrents"][info_hash] = entry
    return orjson.dumps(data)


def _deserialize_ranked(data: bytes):
    from RTN import ParsedData

    raw = orjson.loads(data)
    ranked_hashes = raw["ranked"]
    torrents = {}
    for info_hash, entry in raw["torrents"].items():
        parsed_dict = entry.pop("parsed", None)
        if parsed_dict is not None:
            entry["parsed"] = ParsedData.model_construct(**parsed_dict)
        else:
            entry["parsed"] = None
        torrents[info_hash] = entry
    return ranked_hashes, torrents


async def get_cached_torrents(media_id: str, season, episode):
    if not _redis:
        return None
    try:
        data = await _redis.get(_torrent_key(media_id, season, episode))
        if data:
            return _deserialize_torrent_dict(data)
    except Exception:
        pass
    return None


async def set_cached_torrents(media_id: str, season, episode, torrents: dict, ttl: int = DEFAULT_TTL):
    if not _redis or not torrents:
        return
    try:
        key = _torrent_key(media_id, season, episode)
        await _redis.set(key, _serialize_torrent_dict(torrents), ex=ttl)
    except Exception:
        pass


async def get_cached_ranked(
    media_id, season, episode, rtn_settings, rtn_ranking, max_size, remove_trash,
    max_results_per_resolution=0,
):
    if not _redis:
        return None, None
    try:
        sh = _settings_hash(rtn_settings, rtn_ranking, max_size, remove_trash, max_results_per_resolution)
        data = await _redis.get(_ranked_key(media_id, season, episode, sh))
        if data:
            return _deserialize_ranked(data)
    except Exception:
        pass
    return None, None


async def set_cached_ranked(
    media_id, season, episode, rtn_settings, rtn_ranking, max_size, remove_trash,
    max_results_per_resolution, ranked_torrents, torrents, ttl: int = DEFAULT_TTL,
):
    if not _redis or not ranked_torrents:
        return
    try:
        sh = _settings_hash(rtn_settings, rtn_ranking, max_size, remove_trash, max_results_per_resolution)
        key = _ranked_key(media_id, season, episode, sh)
        await _redis.set(key, _serialize_ranked(ranked_torrents, torrents), ex=ttl)
    except Exception:
        pass


def is_redis_available():
    return _redis is not None


# --- Distributed Lock via Redis ---

async def redis_lock_acquire(lock_key: str, timeout: int) -> str | None:
    if not _redis:
        return None
    instance_id = str(uuid.uuid4())
    try:
        key = f"{LOCK_PREFIX}{lock_key}"
        acquired = await _redis.set(key, instance_id, nx=True, ex=timeout)
        if acquired:
            return instance_id
    except Exception:
        pass
    return None


async def redis_lock_release(lock_key: str, instance_id: str):
    if not _redis:
        return
    try:
        key = f"{LOCK_PREFIX}{lock_key}"
        # Only release if we own the lock (atomic check-and-delete via Lua)
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        await _redis.eval(script, 1, key, instance_id)
    except Exception:
        pass


async def redis_lock_refresh(lock_key: str, instance_id: str, timeout: int) -> bool:
    if not _redis:
        return False
    try:
        key = f"{LOCK_PREFIX}{lock_key}"
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        result = await _redis.eval(script, 1, key, instance_id, str(timeout))
        return result == 1
    except Exception:
        return False


async def redis_lock_exists(lock_key: str) -> bool:
    if not _redis:
        return False
    try:
        key = f"{LOCK_PREFIX}{lock_key}"
        return bool(await _redis.exists(key))
    except Exception:
        return False


# --- First Search via Redis ---

async def redis_first_search(media_id: str, ttl: int = 86400) -> bool | None:
    """Returns True if this is the first search (key was set), False if already searched, None if Redis unavailable."""
    if not _redis:
        return None
    try:
        key = f"{FIRST_SEARCH_PREFIX}{media_id}"
        result = await _redis.set(key, b"1", nx=True, ex=ttl)
        return bool(result)
    except Exception:
        return None


# --- Fresh Torrent Count via Redis ---

def _fresh_torrent_key(media_id: str, season, episode):
    return f"{FRESH_TORRENT_PREFIX}{media_id}:{season}:{episode}"


async def redis_mark_fresh_torrents(media_id: str, season, episode, ttl: int = None):
    """Mark that fresh torrents exist for this media. TTL = LIVE_TORRENT_CACHE_TTL."""
    if not _redis:
        return
    if ttl is None:
        ttl = settings.LIVE_TORRENT_CACHE_TTL
    if ttl < 0:
        ttl = 86400 * 365  # effectively never expires
    try:
        key = _fresh_torrent_key(media_id, season, episode)
        await _redis.set(key, b"1", ex=ttl)
    except Exception:
        pass


async def redis_has_fresh_torrents(media_id: str, season, episode) -> bool | None:
    """Check if fresh torrents exist. Returns True/False or None if Redis unavailable."""
    if not _redis:
        return None
    try:
        key = _fresh_torrent_key(media_id, season, episode)
        return bool(await _redis.exists(key))
    except Exception:
        return None


# --- Debrid Availability Cache via Redis ---

def _debrid_cache_key(debrid_service: str, season, episode, info_hash: str):
    return f"{DEBRID_CACHE_PREFIX}{debrid_service}:{season}:{episode}:{info_hash}"


async def redis_get_debrid_availability(
    debrid_service: str, info_hashes: list, season=None, episode=None
) -> list | None:
    """Check Redis for cached debrid availability. Returns list of row-like dicts or None."""
    if not _redis:
        return None
    try:
        if not info_hashes:
            return []
        keys = [_debrid_cache_key(debrid_service, season, episode, ih) for ih in info_hashes]
        results = await _redis.mget(*keys)
        found = []
        for ih, data in zip(info_hashes, results):
            if data is not None:
                entry = orjson.loads(data)
                entry["info_hash"] = ih
                found.append(entry)
        if not found:
            return None
        return found
    except Exception:
        return None


async def redis_set_debrid_availability(
    debrid_service: str, rows: list, season=None, episode=None, ttl: int = None
):
    """Cache debrid availability rows as individual keys with TTL."""
    if not _redis or not rows:
        return
    if ttl is None:
        ttl = settings.DEBRID_CACHE_TTL
    try:
        pipe = _redis.pipeline()
        for row in rows:
            ih = row["info_hash"]
            key = _debrid_cache_key(debrid_service, season, episode, ih)
            entry = {
                "file_index": row.get("file_index"),
                "title": row.get("title"),
                "size": row.get("size"),
                "parsed": row.get("parsed"),
            }
            pipe.set(key, orjson.dumps(entry), ex=ttl)
        await pipe.execute()
    except Exception:
        pass


# --- Metadata Cache via Redis ---

def _metadata_cache_key(media_id: str):
    return f"{METADATA_CACHE_PREFIX}{media_id}"


async def redis_get_metadata(media_id: str) -> dict | None:
    """Get cached metadata from Redis. Returns dict with title, year, year_end, aliases or None."""
    if not _redis:
        return None
    try:
        key = _metadata_cache_key(media_id)
        data = await _redis.get(key)
        if data:
            return orjson.loads(data)
    except Exception:
        pass
    return None


async def redis_set_metadata(media_id: str, metadata: dict, aliases: dict, ttl: int = None):
    """Cache metadata in Redis."""
    if not _redis:
        return
    if ttl is None:
        ttl = settings.METADATA_CACHE_TTL
    try:
        key = _metadata_cache_key(media_id)
        data = orjson.dumps({
            "title": metadata["title"],
            "year": metadata.get("year"),
            "year_end": metadata.get("year_end"),
            "aliases": aliases,
        })
        await _redis.set(key, data, ex=ttl)
    except Exception:
        pass

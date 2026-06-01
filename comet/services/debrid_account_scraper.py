import asyncio
import hashlib
import time
from datetime import datetime

import orjson

from comet.core.database import (_debrid_account_snapshot_ttl,
                                 build_json_list_membership_predicate,
                                 database, encode_json_param)
from comet.core.execution import get_executor
from comet.core.logger import logger
from comet.core.models import settings
from comet.debrid.manager import build_account_key_hash
from comet.debrid.stremthru import StremThru
from comet.services.filtering import filter_worker
from comet.services.lock import DistributedLock
from comet.services.redis_cache import (redis_get_account_snapshot_fp,
                                        redis_set_account_snapshot_fp)
from comet.services.torrent_manager import torrent_update_queue
from comet.utils.parsing import parsed_matches_target

_SYNC_LOCK_PREFIX = "debrid-account-sync"
_CACHED_STATUSES = frozenset({"cached", "downloaded"})
_background_tasks: set[asyncio.Task] = set()
TORRENT_INFO_HASH_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "info_hash", "info_hashes"
)
# Columns written per magnet, in a fixed order, so a snapshot can be flattened into
# a single multi-row INSERT instead of one statement per magnet.
_MAGNET_INSERT_COLUMNS = (
    "debrid_service",
    "account_key_hash",
    "magnet_id",
    "info_hash",
    "name",
    "size",
    "status",
    "added_at",
    "synced_at",
)
# Columns whose change should trigger a real upsert (everything except the conflict
# key). synced_at is included so the read-side freshness watermark is refreshed.
_MAGNET_UPDATE_COLUMNS = (
    "info_hash",
    "name",
    "size",
    "status",
    "added_at",
    "synced_at",
)
# Fields that define whether the account's magnet set actually changed (the snapshot
# fingerprint). Excludes synced_at, which is just the freshness watermark.
_MAGNET_FINGERPRINT_COLUMNS = (
    "magnet_id",
    "info_hash",
    "name",
    "size",
    "status",
    "added_at",
)
_MAGNET_INSERT_CHUNK = 100
_MAGNET_SQL_CACHE: dict[int, str] = {}

_TOUCH_SNAPSHOT_QUERY = """
    UPDATE debrid_account_magnets
    SET synced_at = :synced_at
    WHERE debrid_service = :debrid_service
      AND account_key_hash = :account_key_hash
    RETURNING 1
"""
_UPSERT_ACCOUNT_SYNC_STATE_QUERY = """
    INSERT INTO debrid_account_sync_state (
        debrid_service,
        account_key_hash,
        last_sync_at
    ) VALUES (
        :debrid_service,
        :account_key_hash,
        :last_sync_at
    )
    ON CONFLICT (debrid_service, account_key_hash)
    DO UPDATE SET last_sync_at = EXCLUDED.last_sync_at
"""


def _dedupe_accounts(debrid_entries: list[dict]) -> list[tuple[str, str, str]]:
    seen = set()
    accounts = []
    for entry in debrid_entries:
        service = entry["service"]
        api_key = entry["apiKey"]
        if not api_key:
            continue
        key = (service, api_key)
        if key in seen:
            continue
        seen.add(key)
        accounts.append((service, api_key, build_account_key_hash(api_key)))
    return accounts


def _sync_lock_key(service: str, account_key_hash: str) -> str:
    return f"{_SYNC_LOCK_PREFIX}:{service}:{account_key_hash}"


def _to_epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return time.time()
    return time.time()


def _should_force_requested_episode_scope(
    parsed,
    season: int | None,
    episode: int | None,
    reject_unknown_episode_files: bool,
) -> bool:
    return (
        reject_unknown_episode_files
        and season is not None
        and episode is not None
        and not (parsed.seasons and parsed.episodes)
    )


def _resolve_cache_scope(
    parsed,
    search_season: int | None,
    resolved_season: int | None,
    resolved_episode: int | None,
) -> tuple[list[int | None], list[int | None]]:
    if resolved_season is not None or resolved_episode is not None:
        return [resolved_season], [resolved_episode]
    return (
        parsed.seasons if parsed.seasons else [search_season],
        parsed.episodes if parsed.episodes else [None],
    )


async def _fetch_all_magnets(client: StremThru, max_items: int):
    limit = 500
    items_by_id = {}
    offset = 0

    while len(items_by_id) < max_items:
        page_limit = min(limit, max_items - len(items_by_id))
        items, total_items = await client.list_magnets(limit=page_limit, offset=offset)
        if items is None:
            return None

        if not items:
            break

        for item in items:
            magnet_id = str(item["id"])
            items_by_id[magnet_id] = item

        if len(items) < page_limit:
            break

        offset += page_limit
        if total_items and offset >= total_items:
            break

    return list(items_by_id.values())


def _multi_row_magnet_sql(n: int) -> str:
    sql = _MAGNET_SQL_CACHE.get(n)
    if sql is None:
        rows_sql = ",\n        ".join(
            "(" + ", ".join(f":{col}_{i}" for col in _MAGNET_INSERT_COLUMNS) + ")"
            for i in range(n)
        )
        set_sql = ",\n            ".join(
            f"{col} = EXCLUDED.{col}" for col in _MAGNET_UPDATE_COLUMNS
        )
        sql = f"""
    INSERT INTO debrid_account_magnets (
        {", ".join(_MAGNET_INSERT_COLUMNS)}
    ) VALUES
        {rows_sql}
    ON CONFLICT (debrid_service, account_key_hash, magnet_id)
    DO UPDATE SET
            {set_sql}
"""
        if len(_MAGNET_SQL_CACHE) > 64:
            _MAGNET_SQL_CACHE.clear()
        _MAGNET_SQL_CACHE[n] = sql
    return sql


def _snapshot_fingerprint(rows: list[dict]) -> str:
    """Stable fingerprint of an account's magnet set (content, not synced_at)."""
    items = sorted(
        tuple(row[col] for col in _MAGNET_FINGERPRINT_COLUMNS) for row in rows
    )
    return hashlib.md5(
        orjson.dumps(items), usedforsecurity=False
    ).hexdigest()


async def _upsert_snapshot_rows(rows: list[dict]):
    if not rows:
        return

    # A multi-row INSERT ... ON CONFLICT cannot affect the same conflict target
    # twice; magnet_id is the per-account discriminator, so dedupe on it (keep last).
    deduped: dict = {}
    for row in rows:
        deduped[row["magnet_id"]] = row
    rows = list(deduped.values())

    for start in range(0, len(rows), _MAGNET_INSERT_CHUNK):
        chunk = rows[start : start + _MAGNET_INSERT_CHUNK]
        params = {}
        for i, row in enumerate(chunk):
            for col in _MAGNET_INSERT_COLUMNS:
                params[f"{col}_{i}"] = row[col]
        await database.execute(_multi_row_magnet_sql(len(chunk)), params)


async def _touch_snapshot(
    service: str, account_key_hash: str, synced_at: float
) -> bool:
    """Refresh the per-row synced_at watermark for an unchanged snapshot in one
    bulk UPDATE, instead of re-upserting every magnet. Returns True only if rows
    were actually updated, so the caller can fall back to a full upsert when the
    durable rows are gone (e.g. cleaned up while the Redis fingerprint survived)."""
    row = await database.fetch_one(
        _TOUCH_SNAPSHOT_QUERY,
        {
            "debrid_service": service,
            "account_key_hash": account_key_hash,
            "synced_at": synced_at,
        },
    )
    return row is not None


async def _set_last_sync(service: str, account_key_hash: str, last_sync: float):
    await database.execute(
        _UPSERT_ACCOUNT_SYNC_STATE_QUERY,
        {
            "debrid_service": service,
            "account_key_hash": account_key_hash,
            "last_sync_at": last_sync,
        },
    )


async def _sync_single_account(
    session,
    service: str,
    api_key: str,
    ip: str,
    account_key_hash: str,
):
    client = StremThru(session, "", "", f"{service}:{api_key}", ip)
    synced_at = time.time()

    magnets = await _fetch_all_magnets(
        client, settings.DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS
    )
    if magnets is None:
        return

    rows = []
    for item in magnets:
        info_hash = item["hash"].lower()
        if not info_hash:
            continue

        rows.append(
            {
                "debrid_service": service,
                "account_key_hash": account_key_hash,
                "magnet_id": str(item["id"]),
                "info_hash": info_hash,
                "name": item["name"],
                "size": item["size"],
                "status": item["status"],
                "added_at": _to_epoch(item.get("added_at")),
                "synced_at": synced_at,
            }
        )

    # Coalesce the durable write through Redis. The full snapshot is re-upserted
    # on every sync, but the account's magnet set is usually unchanged — in that
    # case re-writing every row (only to bump synced_at) is wasted DB work. When
    # the fingerprint matches the last sync, refresh the synced_at watermark with a
    # single bulk UPDATE and skip the per-magnet upsert + stale GC entirely. This
    # collapsed the ~496M individual upsert calls that were churning the table.
    fingerprint = _snapshot_fingerprint(rows)
    prev_fingerprint = await redis_get_account_snapshot_fp(service, account_key_hash)

    coalesced = False
    if rows and prev_fingerprint is not None and prev_fingerprint == fingerprint:
        # Unchanged set: just bump the watermark. _touch_snapshot returns False if
        # no rows exist (durable copy was cleaned up while the fingerprint lived),
        # in which case we fall through to a full upsert to self-heal.
        coalesced = await _touch_snapshot(service, account_key_hash, synced_at)

    if not coalesced:
        await _upsert_snapshot_rows(rows)

        await database.execute(
            """
            DELETE FROM debrid_account_magnets
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
              AND synced_at < :synced_at
            """,
            {
                "debrid_service": service,
                "account_key_hash": account_key_hash,
                "synced_at": synced_at,
            },
        )

    # Record the fingerprint only after a successful DB write, so a matching
    # fingerprint always implies the durable rows are present. Refreshed on both
    # paths to keep it alive while the account is actively synced.
    await redis_set_account_snapshot_fp(
        service, account_key_hash, fingerprint, _debrid_account_snapshot_ttl() * 2
    )

    await _set_last_sync(service, account_key_hash, synced_at)

    logger.log(
        "SCRAPER",
        f"{service}: Synced {len(rows)} account torrents",
    )


async def _sync_task(
    lock: DistributedLock,
    session,
    service: str,
    api_key: str,
    ip: str,
    account_key_hash: str,
):
    try:
        await _sync_single_account(session, service, api_key, ip, account_key_hash)
    except Exception as e:
        logger.warning(f"Failed to sync debrid account torrents for {service}: {e}")
    finally:
        await lock.release()


def _handle_sync_task_done(task: asyncio.Task):
    _background_tasks.discard(task)
    if task.cancelled():
        return

    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Debrid account sync task completion handling failed: {e}")
        return

    if error:
        logger.error(f"Debrid account sync task failed: {error}")


async def _has_fresh_snapshot(
    service: str, account_key_hash: str, min_timestamp: float
):
    row = await database.fetch_one(
        """
        SELECT 1
        WHERE EXISTS (
            SELECT 1
            FROM debrid_account_sync_state
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
              AND last_sync_at >= :min_timestamp
        )
        OR EXISTS (
            SELECT 1
            FROM debrid_account_magnets
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
              AND synced_at >= :min_timestamp
        )
        """,
        {
            "debrid_service": service,
            "account_key_hash": account_key_hash,
            "min_timestamp": min_timestamp,
        },
        force_primary=True,
    )
    return bool(row)


async def _wait_for_snapshot_targets(
    targets: list[tuple[str, str]],
    min_timestamp: float,
    deadline: float,
):
    if not targets:
        return

    pending = targets
    while pending and time.monotonic() < deadline:
        unresolved = []
        for service, account_key_hash in pending:
            has_snapshot = await _has_fresh_snapshot(
                service, account_key_hash, min_timestamp
            )
            if not has_snapshot:
                unresolved.append((service, account_key_hash))
        if not unresolved:
            return
        pending = unresolved
        await asyncio.sleep(0.15)


async def ensure_account_snapshot_ready(session, debrid_entries: list[dict], ip: str):
    accounts = _dedupe_accounts(debrid_entries)
    if not accounts:
        return

    min_timestamp = time.time() - _debrid_account_snapshot_ttl()
    missing = []
    for service, api_key, account_key_hash in accounts:
        has_snapshot = await _has_fresh_snapshot(
            service, account_key_hash, min_timestamp
        )
        if not has_snapshot:
            missing.append((service, api_key, account_key_hash))

    if not missing:
        return

    deadline = time.monotonic() + settings.DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT
    sync_tasks = []
    waiting_targets = []

    for service, api_key, account_key_hash in missing:
        lock = DistributedLock(_sync_lock_key(service, account_key_hash), timeout=300)
        if await lock.acquire():
            sync_tasks.append(
                _sync_task(lock, session, service, api_key, ip, account_key_hash)
            )
        else:
            waiting_targets.append((service, account_key_hash))

    if sync_tasks:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*sync_tasks, return_exceptions=True),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                logger.log(
                    "SCRAPER",
                    "Debrid account warm sync timed out, continuing with partial data",
                )
        else:
            for sync_task in sync_tasks:
                task = asyncio.create_task(sync_task)
                _background_tasks.add(task)
                task.add_done_callback(_handle_sync_task_done)

    if waiting_targets:
        await _wait_for_snapshot_targets(waiting_targets, min_timestamp, deadline)


async def trigger_account_snapshot_sync(session, service: str, api_key: str, ip: str):
    if not api_key:
        return False

    account_key_hash = build_account_key_hash(api_key)
    lock = DistributedLock(_sync_lock_key(service, account_key_hash), timeout=300)
    if not await lock.acquire():
        return False

    task = asyncio.create_task(
        _sync_task(lock, session, service, api_key, ip, account_key_hash)
    )
    _background_tasks.add(task)
    task.add_done_callback(_handle_sync_task_done)
    return True


async def _fetch_existing_media_torrent_keys(
    media_id: str, info_hashes: list[str]
) -> set[tuple[str, int | None, int | None]]:
    if not info_hashes:
        return set()

    rows = await database.fetch_all(
        f"""
        SELECT info_hash, season, episode
        FROM torrents
        WHERE media_id = :media_id
          AND {TORRENT_INFO_HASH_MEMBERSHIP_SQL}
        """,
        {
            "media_id": media_id,
            "info_hashes": encode_json_param(info_hashes),
        },
        force_primary=True,
    )
    return {(row["info_hash"], row["season"], row["episode"]) for row in rows}


async def ingest_account_torrents_to_public_cache(
    account_torrents: dict,
    media_id: str,
    search_season: int | None,
):
    if not account_torrents:
        return 0

    existing_torrent_keys = await _fetch_existing_media_torrent_keys(
        media_id, list(account_torrents.keys())
    )

    file_infos_to_enqueue = []
    for info_hash, torrent in account_torrents.items():
        parsed = torrent["parsed"]
        parsed_seasons, parsed_episodes = _resolve_cache_scope(
            parsed,
            search_season,
            torrent.get("season"),
            torrent.get("episode"),
        )
        episode = None if len(parsed_episodes) > 1 else parsed_episodes[0]

        for season in parsed_seasons:
            if (info_hash, season, episode) in existing_torrent_keys:
                continue

            file_info = {
                "info_hash": info_hash,
                "index": torrent["fileIndex"],
                "title": torrent["title"],
                "size": torrent["size"],
                "season": season,
                "episode": episode,
                "parsed": parsed,
                "seeders": torrent["seeders"],
                "tracker": torrent["tracker"],
                "sources": torrent["sources"],
            }
            file_infos_to_enqueue.append(file_info)

    if file_infos_to_enqueue:
        await torrent_update_queue.add_torrent_infos(file_infos_to_enqueue, media_id)

    return len(file_infos_to_enqueue)


async def schedule_account_snapshot_refresh(
    background_tasks,
    session,
    debrid_entries: list[dict],
    ip: str,
):
    now = time.time()

    for service, api_key, account_key_hash in _dedupe_accounts(debrid_entries):
        row = await database.fetch_one(
            """
            SELECT last_sync_at
            FROM debrid_account_sync_state
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
            """,
            {
                "debrid_service": service,
                "account_key_hash": account_key_hash,
            },
            force_primary=True,
        )

        if (
            row
            and row["last_sync_at"]
            and (
                now - row["last_sync_at"]
                < settings.DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL
            )
        ):
            continue

        lock = DistributedLock(_sync_lock_key(service, account_key_hash), timeout=300)
        lock_acquired = await lock.acquire()
        if not lock_acquired:
            continue

        background_tasks.add_task(
            _sync_task,
            lock,
            session,
            service,
            api_key,
            ip,
            account_key_hash,
        )


async def get_account_torrents_for_media(
    debrid_entries: list[dict],
    media_type: str,
    title: str,
    year: int | None,
    year_end: int | None,
    season: int | None,
    episode: int | None,
    aliases: dict | None,
    remove_adult_content: bool,
    target_air_date: str | None = None,
    reject_unknown_episode_files: bool = False,
):
    account_torrents = {}
    service_cache_status = {}

    accounts = _dedupe_accounts(debrid_entries)
    if not accounts:
        return account_torrents, service_cache_status

    min_timestamp = time.time() - _debrid_account_snapshot_ttl()
    aliases = aliases or {}

    async def fetch_rows(service: str, account_key_hash: str):
        rows = await database.fetch_all(
            """
            SELECT info_hash, name, size, status
            FROM debrid_account_magnets
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
              AND synced_at >= :min_timestamp
            ORDER BY added_at DESC
            LIMIT :limit
            """,
            {
                "debrid_service": service,
                "account_key_hash": account_key_hash,
                "min_timestamp": min_timestamp,
                "limit": settings.DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS,
            },
            force_primary=True,
        )
        return service, rows

    results = await asyncio.gather(
        *[
            fetch_rows(service, account_key_hash)
            for service, _, account_key_hash in accounts
        ],
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"Failed to read debrid account snapshot: {result}")
            continue

        service, rows = result
        candidate_torrents = []
        service_cached_status = {}
        for row in rows:
            info_hash = row["info_hash"]
            if not info_hash:
                continue

            info_hash = info_hash.lower()
            is_cached = row["status"] in _CACHED_STATUSES
            if is_cached:
                service_cached_status[info_hash] = True
            elif info_hash not in service_cached_status:
                service_cached_status[info_hash] = False

            candidate_torrents.append(
                {
                    "infoHash": info_hash,
                    "fileIndex": None,
                    "title": row["name"],
                    "seeders": 0,
                    "size": row["size"],
                    "tracker": f"DebridAccount|{service}",
                    "sources": [],
                }
            )

        if not candidate_torrents:
            continue

        loop = asyncio.get_running_loop()
        filtered_torrents = await loop.run_in_executor(
            get_executor(),
            filter_worker,
            candidate_torrents,
            title,
            year,
            year_end,
            media_type,
            aliases,
            remove_adult_content,
        )

        for torrent in filtered_torrents:
            parsed = torrent["parsed"]
            if not parsed_matches_target(
                parsed,
                season,
                episode,
                target_air_date=target_air_date,
                reject_unknown_episode_files=reject_unknown_episode_files,
            ):
                continue

            info_hash = torrent["infoHash"]
            cached_state = service_cached_status.get(info_hash, False)
            status_map = service_cache_status.setdefault(info_hash, {})
            if cached_state:
                status_map[service] = True
            elif service not in status_map:
                status_map[service] = False

            if info_hash in account_torrents:
                continue

            force_requested_scope = _should_force_requested_episode_scope(
                parsed,
                season,
                episode,
                reject_unknown_episode_files,
            )
            account_torrents[info_hash] = {
                "fileIndex": torrent["fileIndex"],
                "title": torrent["title"],
                "seeders": torrent["seeders"],
                "size": torrent["size"],
                "tracker": torrent["tracker"],
                "sources": torrent["sources"],
                "parsed": parsed,
                "season": season if force_requested_scope else None,
                "episode": episode if force_requested_scope else None,
            }

    return account_torrents, service_cache_status

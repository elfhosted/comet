import time

from comet.core.database import (build_distinct_from_predicate,
                                 build_json_list_membership_predicate,
                                 build_scope_lookup_params, build_scope_params,
                                 build_upsert_assignments, encode_json_param)
from comet.core.models import database, settings
from comet.services.redis_cache import (redis_filter_debrid_db_writes,
                                        redis_get_debrid_availability,
                                        redis_mark_debrid_db_written,
                                        redis_set_debrid_availability)
from comet.utils.parsing import default_dump

DEBRID_UPDATE_INTERVAL = (
    settings.DEBRID_CACHE_TTL // 2 if settings.DEBRID_CACHE_TTL > 0 else 31536000
)

DEBRID_CHANGE_DETECTION_COLUMNS = (
    "title",
    "file_index",
    "size",
    "parsed_json",
)
DEBRID_UPDATE_COLUMNS = (*DEBRID_CHANGE_DETECTION_COLUMNS, "updated_at")
DEBRID_UPDATE_SET_SQL = build_upsert_assignments(DEBRID_UPDATE_COLUMNS)
DEBRID_DISTINCT_UPDATE_WHERE_SQL = build_distinct_from_predicate(
    "debrid_availability",
    "EXCLUDED",
    DEBRID_CHANGE_DETECTION_COLUMNS,
)
INFO_HASH_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "info_hash", "info_hashes"
)
SCOPE_FILTER_SQL = """
season_norm = :season_norm
AND episode_norm = :episode_norm
"""


def _build_conditional_update() -> str:
    return f"""
        DO UPDATE SET
{DEBRID_UPDATE_SET_SQL}
        WHERE
            {DEBRID_DISTINCT_UPDATE_WHERE_SQL}
            OR COALESCE(debrid_availability.updated_at, 0) < (EXCLUDED.updated_at - :update_interval)
"""


CONDITIONAL_UPDATE_SQL = _build_conditional_update()

# Columns written by the upsert, in a fixed order. Used to flatten rows into a
# single multi-row INSERT so a batch of files costs one statement instead of N.
_INSERT_COLUMNS = (
    "debrid_service",
    "info_hash",
    "season",
    "episode",
    "season_norm",
    "episode_norm",
    "file_index",
    "title",
    "size",
    "parsed_json",
    "updated_at",
)
_DB_INSERT_CHUNK = 100
_MULTI_ROW_SQL_CACHE: dict[int, str] = {}


def _multi_row_insert_sql(n: int) -> str:
    sql = _MULTI_ROW_SQL_CACHE.get(n)
    if sql is None:
        rows_sql = ",\n        ".join(
            "(" + ", ".join(f":{col}_{i}" for col in _INSERT_COLUMNS) + ")"
            for i in range(n)
        )
        sql = f"""
    INSERT INTO debrid_availability (
        {", ".join(_INSERT_COLUMNS)}
    )
    VALUES
        {rows_sql}
    ON CONFLICT (debrid_service, info_hash, season_norm, episode_norm)
    {CONDITIONAL_UPDATE_SQL}
"""
        if len(_MULTI_ROW_SQL_CACHE) > 64:
            _MULTI_ROW_SQL_CACHE.clear()
        _MULTI_ROW_SQL_CACHE[n] = sql
    return sql


async def _persist_availability(values: list):
    """Write availability rows to the durable DB as batched multi-row upserts.

    A single ``INSERT ... ON CONFLICT`` cannot affect the same conflict target
    twice, so the batch is first deduplicated by (info_hash, season_norm,
    episode_norm), keeping the last occurrence.
    """
    if not values:
        return

    deduped: dict = {}
    for v in values:
        deduped[(v["info_hash"], v["season_norm"], v["episode_norm"])] = v
    rows = list(deduped.values())

    for start in range(0, len(rows), _DB_INSERT_CHUNK):
        chunk = rows[start : start + _DB_INSERT_CHUNK]
        params = {"update_interval": DEBRID_UPDATE_INTERVAL}
        for i, row in enumerate(chunk):
            for col in _INSERT_COLUMNS:
                params[f"{col}_{i}"] = row[col]
        await database.execute(_multi_row_insert_sql(len(chunk)), params)


async def cache_availability(debrid_service: str, availability: list):
    current_time = time.time()

    values = [
        {
            "debrid_service": debrid_service,
            "info_hash": file["info_hash"],
            "file_index": str(file["index"]) if file["index"] is not None else None,
            "title": file["title"],
            "season": file["season"],
            "episode": file["episode"],
            **build_scope_params(file["season"], file["episode"]),
            "size": file["size"] if file["index"] is not None else None,
            "parsed_json": (
                encode_json_param(file["parsed"], default=default_dump)
                if file["parsed"] is not None
                else None
            ),
            "updated_at": current_time,
            "update_interval": DEBRID_UPDATE_INTERVAL,
        }
        for file in availability
    ]

    # Write-through to Redis (group by season/episode)
    by_se = {}
    for val in values:
        se_key = (val.get("season"), val.get("episode"))
        by_se.setdefault(se_key, []).append({
            "info_hash": val["info_hash"],
            "file_index": val.get("file_index"),
            "title": val.get("title"),
            "size": val.get("size"),
            "parsed": val.get("parsed_json"),
        })
    for (s, e), rows in by_se.items():
        await redis_set_debrid_availability(debrid_service, rows, season=s, episode=e)

    # Coalesce durable DB writes through Redis. Redis already serves the read path,
    # so the DB is only the durable backstop and needs at most one refresh per key
    # per DEBRID_UPDATE_INTERVAL. Skipping the rest removes the bulk of the per-row
    # upsert volume that was dominating DB CPU (the conditional upsert was firing
    # ~940x/sec, almost all no-op/refresh writes). When Redis is unavailable the
    # filter returns None and we persist everything, preserving DB-only behaviour.
    #
    # Tradeoff: within the interval, a key's durable row is not refreshed even if
    # the file metadata changed. Per-(info_hash, season, episode) availability is
    # effectively immutable and Redis is authoritative for reads, so the durable
    # copy can lag by at most DEBRID_UPDATE_INTERVAL with no practical impact.
    db_keys = [
        (v["season_norm"], v["episode_norm"], v["info_hash"]) for v in values
    ]
    need = await redis_filter_debrid_db_writes(debrid_service, db_keys)
    if need is None:
        db_values = values
        written_keys = None
    else:
        need_set = set(need)
        db_values = [
            v
            for v in values
            if (v["season_norm"], v["episode_norm"], v["info_hash"]) in need_set
        ]
        written_keys = need

    await _persist_availability(db_values)

    # Mark only after a successful write so a failed/transient DB error is retried
    # on the next request rather than suppressed for the whole interval.
    if written_keys:
        await redis_mark_debrid_db_written(
            debrid_service, written_keys, DEBRID_UPDATE_INTERVAL
        )


async def get_cached_availability(
    debrid_service: str,
    info_hashes: list[str],
    season: int | None = None,
    episode: int | None = None,
):
    # Try Redis first
    redis_results = await redis_get_debrid_availability(
        debrid_service, info_hashes, season, episode
    )
    if redis_results is not None:
        return redis_results

    select_clause = "SELECT info_hash, file_index, title, size, parsed_json AS parsed"

    min_timestamp = time.time() - settings.DEBRID_CACHE_TTL
    base_from_where = f"""
        FROM debrid_availability
        WHERE {INFO_HASH_MEMBERSHIP_SQL}
        AND updated_at >= :min_timestamp
    """

    params = {
        "info_hashes": encode_json_param(info_hashes),
        "min_timestamp": min_timestamp,
        **build_scope_lookup_params(season, episode),
    }

    base_from_where += " AND debrid_service = :debrid_service"
    params["debrid_service"] = debrid_service

    if debrid_service == "offcloud":
        query = f"""
            SELECT info_hash, file_index, title, size, parsed
            FROM (
                SELECT
                    info_hash,
                    file_index,
                    title,
                    size,
                    parsed_json AS parsed,
                    ROW_NUMBER() OVER (
                        PARTITION BY info_hash
                        ORDER BY
                            CASE WHEN {SCOPE_FILTER_SQL} THEN 0 ELSE 1 END,
                            updated_at DESC
                    ) AS row_number
                {base_from_where}
                AND (
                    ({SCOPE_FILTER_SQL})
                    OR title IS NULL
                )
            ) ranked_offcloud_availability
            WHERE row_number = 1
        """
        results = await database.fetch_all(query, params)
    else:
        query = f"""
            {select_clause}
            {base_from_where}
            AND {SCOPE_FILTER_SQL}
        """
        results = await database.fetch_all(query, params)

    # Backfill Redis with DB results
    if results:
        redis_rows = [
            {
                "info_hash": r["info_hash"],
                "file_index": r["file_index"],
                "title": r["title"],
                "size": r["size"],
                "parsed": r["parsed"],
            }
            for r in results
        ]
        await redis_set_debrid_availability(debrid_service, redis_rows, season, episode)

    return results


async def get_cached_availability_any_service(
    info_hashes: list, season: int = None, episode: int = None
):
    min_timestamp = time.time() - settings.DEBRID_CACHE_TTL
    base_from_where = f"""
        FROM debrid_availability
        WHERE {INFO_HASH_MEMBERSHIP_SQL}
        AND updated_at >= :min_timestamp
        AND season_norm = :season_norm
        AND episode_norm = :episode_norm
    """

    params = {
        "info_hashes": encode_json_param(info_hashes),
        "min_timestamp": min_timestamp,
        **build_scope_lookup_params(season, episode),
    }

    query = f"""
        SELECT info_hash, file_index, title, size, parsed
        FROM (
            SELECT
                info_hash,
                file_index,
                title,
                size,
                parsed_json AS parsed,
                ROW_NUMBER() OVER (
                    PARTITION BY info_hash
                    ORDER BY updated_at DESC
                ) AS row_number
            {base_from_where}
        ) latest_debrid_availability
        WHERE row_number = 1
    """

    return await database.fetch_all(query, params)

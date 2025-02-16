import hashlib
import re
import bencodepy
import aiohttp
import anyio
import asyncio
<<<<<<< HEAD
import orjson
=======
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
import time

from RTN import parse
from urllib.parse import parse_qs, urlparse
from demagnetize.core import Demagnetizer
from torf import Magnet
<<<<<<< HEAD
from RTN import ParsedData, parse
=======
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b

from comet.utils.logger import logger
from comet.utils.models import settings, database
from comet.utils.general import is_video

info_hash_pattern = re.compile(r"btih:([a-fA-F0-9]{40})")


def extract_trackers_from_magnet(magnet_uri: str):
    try:
        parsed = urlparse(magnet_uri)
        params = parse_qs(parsed.query)
        return params.get("tr", [])
    except Exception as e:
        logger.warning(f"Failed to extract trackers from magnet URI: {e}")
        return []


async def download_torrent(session: aiohttp.ClientSession, url: str):
    try:
        timeout = aiohttp.ClientTimeout(total=settings.GET_TORRENT_TIMEOUT)
        async with session.get(url, allow_redirects=False, timeout=timeout) as response:
            if response.status == 200:
                return (await response.read(), None, None)

            location = response.headers.get("Location", "")
            if location:
                match = info_hash_pattern.search(location)
                if match:
                    return (None, match.group(1), location)
            return (None, None, None)
    except Exception as e:
        logger.warning(f"Failed to download torrent from {url}: {e}")
        return (None, None, None)


demagnetizer = Demagnetizer()


async def get_torrent_from_magnet(magnet_uri: str):
    try:
        magnet = Magnet.from_string(magnet_uri)
        with anyio.fail_after(60):
            torrent_data = await demagnetizer.demagnetize(magnet)
            if torrent_data:
                return torrent_data.dump()
    except Exception as e:
        logger.warning(f"Failed to get torrent from magnet: {e}")
        return None


def extract_torrent_metadata(content: bytes, season: str, episode: str):
    try:
        torrent_data = bencodepy.decode(content)
        info = torrent_data[b"info"]
        m = hashlib.sha1()
        info_hash = m.hexdigest()

        torrent_name = info.get(b"name", b"").decode()
        if not torrent_name:
            return {}

        announce_list = [
            tracker[0].decode() for tracker in torrent_data.get(b"announce-list", [])
        ]

        metadata = {
            "info_hash": info_hash.lower(),
            "announce_list": announce_list,
        }

        if b"files" in info:
            files = info[b"files"]
            file_data = []
            best_index = None
            best_score = -1
            best_size = 0
            is_movie = True

            for idx, file in enumerate(files):
                if b"path" in file:
                    path_parts = [part.decode() for part in file[b"path"]]
                    path = "/".join(path_parts)
                else:
                    path = file[b"name"].decode() if b"name" in file else ""

                if not path or not is_video(path):
                    continue

                size = file[b"length"]
                score = size

                file_parsed = parse(path)

                season_exists = len(file_parsed.seasons) != 0
                episode_exists = len(file_parsed.episodes) != 0

                if season_exists or episode_exists:
                    is_movie = False

                if (
                    season_exists
                    and episode_exists
                    and file_parsed.seasons[0] == season
                    and file_parsed.episodes[0] == episode
                ):
                    score *= 3

                file_info = {
                    "index": idx,
                    "size": size,
                    "season": file_parsed.seasons[0] if season_exists else None,
                    "episode": file_parsed.episodes[0] if episode_exists else None,
                }

                if score > best_score:
                    best_score = score
                    best_size = size
                    best_index = idx
                    best_file_info = file_info

                if not is_movie:
                    file_data.append(file_info)

            if is_movie and best_index is not None:
                file_data = [best_file_info]

            metadata.update(
                {
                    "file_data": file_data,
                    "file_index": best_index,
                    "file_size": best_size,
                }
            )
        else:
            name = info[b"name"].decode()
            if not is_video(name):
                return {}

            size = info[b"length"]

            file_parsed = parse(name)

<<<<<<< HEAD
            parsed = parse(name)

            metadata["files"].append(
                {"index": idx, "name": name, "size": size, "parsed": parsed}
=======
            metadata.update(
                {
                    "file_index": 0,
                    "file_size": size,
                    "file_data": [
                        {
                            "index": 0,
                            "size": size,
                            "season": file_parsed.seasons[0]
                            if len(file_parsed.seasons) != 0
                            else None,
                            "episode": file_parsed.episodes[0]
                            if len(file_parsed.episodes) != 0
                            else None,
                        }
                    ],
                }
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
            )

        return metadata

    except Exception as e:
        logger.warning(f"Failed to extract torrent metadata: {e}")
        return {}


<<<<<<< HEAD
async def add_torrent(
    info_hash: str,
    seeders: int,
    tracker: str,
    media_id: str,
    search_season: int,
    sources: list,
    file_index: int,
    title: str,
    size: int,
    parsed: ParsedData,
):
    try:
        parsed_season = parsed.seasons[0] if parsed.seasons else search_season
        parsed_episode = parsed.episodes[0] if parsed.episodes else None

        if parsed_episode is not None:
            await database.execute(
                """
                DELETE FROM torrents
                WHERE info_hash = :info_hash
                AND season = :season 
                AND episode IS NULL
                """,
                {
                    "info_hash": info_hash,
                    "season": parsed_season,
                },
            )
            logger.log(
                "SCRAPER",
                f"Deleted season-only entry for S{parsed_season:02d} of {info_hash}",
=======
async def update_torrent_file_index(
    info_hash: str, season: str, episode: str, index: int, size: int
):
    try:
        if season is None and episode is None:
            existing = await database.fetch_one(
                """
                SELECT file_index, file_size
                FROM torrent_file_indexes 
                WHERE info_hash = :info_hash 
                AND season IS NULL
                AND episode IS NULL
                """,
                {"info_hash": info_hash},
            )  # for movies, we keep best file (largest size)

            if existing and existing["file_size"] >= size:
                return

            await database.execute(
                """
                DELETE FROM torrent_file_indexes 
                WHERE info_hash = :info_hash 
                AND season IS NULL
                AND episode IS NULL
                """,
                {"info_hash": info_hash},
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
            )

        await database.execute(
            f"""
<<<<<<< HEAD
                INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
                INTO torrents
                VALUES (:media_id, :info_hash, :file_index, :season, :episode, :title, :seeders, :size, :tracker, :sources, :parsed, :timestamp)
                {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
            """,
            {
                "media_id": media_id,
                "info_hash": info_hash,
                "file_index": file_index,
                "season": parsed_season,
                "episode": parsed_episode,
                "title": title,
                "seeders": seeders,
                "size": size,
                "tracker": tracker,
                "sources": orjson.dumps(sources).decode("utf-8"),
                "parsed": orjson.dumps(parsed, default_dump).decode("utf-8"),
=======
            INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
            INTO torrent_file_indexes 
            VALUES (:info_hash, :season, :episode, :file_index, :file_size, :timestamp)
            {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
            """,
            {
                "info_hash": info_hash,
                "season": season,
                "episode": episode,
                "file_index": index,
                "file_size": size,
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
                "timestamp": time.time(),
            },
        )

        additional = ""
        if parsed_season:
            additional += f" - S{parsed_season:02d}"
            additional += f"E{parsed_episode:02d}" if parsed_episode else ""

        logger.log("SCRAPER", f"Added torrent for {media_id} - {title}{additional}")
    except Exception as e:
        logger.warning(f"Failed to add torrent for {info_hash}: {e}")


class AddTorrentQueue:
    def __init__(self, max_concurrent: int = 10):
        self.queue = asyncio.Queue()
        self.max_concurrent = max_concurrent
        self.is_running = False
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def add_torrent(
        self,
        magnet_url: str,
        seeders: int,
        tracker: str,
        media_id: str,
        search_season: int,
    ):
        if not settings.DOWNLOAD_TORRENTS:
            return

<<<<<<< HEAD
        await self.queue.put((magnet_url, seeders, tracker, media_id, search_season))
=======
        cached = await database.fetch_one(
            """
            SELECT file_index 
            FROM torrent_file_indexes 
            WHERE info_hash = :info_hash 
            AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
            AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
            AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "info_hash": info_hash,
                "season": season,
                "episode": episode,
                "cache_ttl": settings.CACHE_TTL,
                "current_time": time.time(),
            },
        )
        if cached:
            return

        await self.queue.put((info_hash, magnet_url, season, episode))
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        while self.is_running:
            try:
<<<<<<< HEAD
                (
                    magnet_url,
                    seeders,
                    tracker,
                    media_id,
                    search_season,
                ) = await self.queue.get()
=======
                info_hash, magnet_url, season, episode = await self.queue.get()
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b

                async with self.semaphore:
                    try:
                        content = await get_torrent_from_magnet(magnet_url)
                        if content:
<<<<<<< HEAD
                            metadata = extract_torrent_metadata(content)
                            for file in metadata["files"]:
                                await add_torrent(
                                    metadata["info_hash"],
                                    seeders,
                                    tracker,
                                    media_id,
                                    search_season,
                                    metadata["announce_list"],
                                    file["index"],
                                    file["name"],
                                    file["size"],
                                    file["parsed"],
                                )
=======
                            metadata = extract_torrent_metadata(
                                content, season, episode
                            )
                            if metadata and "file_data" in metadata:
                                for file_info in metadata["file_data"]:
                                    await update_torrent_file_index(
                                        info_hash,
                                        file_info["season"],
                                        file_info["episode"],
                                        file_info["index"],
                                        file_info["size"],
                                    )
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
                    finally:
                        self.queue.task_done()

            except Exception:
                await asyncio.sleep(1)

        self.is_running = False


add_torrent_queue = AddTorrentQueue()


class TorrentUpdateQueue:
    def __init__(self, batch_size: int = 100, flush_interval: float = 5.0):
        self.queue = asyncio.Queue()
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.is_running = False
        self.batches = {"to_check": [], "to_delete": [], "inserts": [], "updates": []}
        self.last_error_time = 0
        self.error_backoff = 1

<<<<<<< HEAD
    async def add_torrent_info(self, file_info: dict, media_id: str = None):
        await self.queue.put((file_info, media_id))
=======
    async def add_update(
        self, info_hash: str, season: str, episode: str, index: int, size: int
    ):
        await self.queue.put((info_hash, season, episode, index, size))
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        last_flush_time = time.time()

        while self.is_running:
            try:
<<<<<<< HEAD
                while not self.queue.empty():
                    try:
                        file_info, media_id = self.queue.get_nowait()
                        await self._process_file_info(file_info, media_id)
                    except asyncio.QueueEmpty:
                        break

                current_time = time.time()

                if (
                    current_time - last_flush_time >= self.flush_interval
                    or self.queue.empty()
                ) and any(len(batch) > 0 for batch in self.batches.values()):
                    await self._flush_batch()
                    last_flush_time = current_time

                if self.queue.empty() and not any(
                    len(batch) > 0 for batch in self.batches.values()
                ):
                    self.is_running = False
                    break

                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error in _process_queue: {e}")
                await self._handle_error(e)

        if any(len(batch) > 0 for batch in self.batches.values()):
            await self._flush_batch()
=======
                info_hash, season, episode, index, size = await self.queue.get()
                try:
                    await update_torrent_file_index(
                        info_hash, season, episode, index, size
                    )
                finally:
                    self.queue.task_done()
            except Exception:
                await asyncio.sleep(1)
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b

        self.is_running = False

    async def _flush_batch(self):
        try:
            if self.batches["to_check"]:
                sub_batch_size = 100
                for i in range(0, len(self.batches["to_check"]), sub_batch_size):
                    sub_batch = self.batches["to_check"][i : i + sub_batch_size]
                    check_params = []
                    for item in sub_batch:
                        check_params.append(
                            {
                                "info_hash": item["info_hash"],
                                "season": item["season"],
                                "episode": item["episode"],
                            }
                        )

                    placeholders = []
                    params = {}
                    for idx, check in enumerate(check_params):
                        key_suffix = f"_{idx}"
                        condition = (
                            f"(info_hash = :info_hash{key_suffix} AND "
                            f"((:season{key_suffix} IS NULL AND season IS NULL) OR season = :season{key_suffix}) AND "
                            f"((:episode{key_suffix} IS NULL AND episode IS NULL) OR episode = :episode{key_suffix}))"
                        )
                        placeholders.append(condition)
                        params[f"info_hash{key_suffix}"] = check["info_hash"]
                        params[f"season{key_suffix}"] = check["season"]
                        params[f"episode{key_suffix}"] = check["episode"]

                    query = f"""
                        SELECT info_hash, season, episode
                        FROM torrents 
                        WHERE {" OR ".join(placeholders)}
                    """

                    async with database.transaction():
                        existing_rows = await database.fetch_all(query, params)

                        existing_set = {
                            (
                                row["info_hash"],
                                row["season"] if row["season"] is not None else None,
                                row["episode"] if row["episode"] is not None else None,
                            )
                            for row in existing_rows
                        }

                        for item in sub_batch:
                            key = (item["info_hash"], item["season"], item["episode"])
                            if key in existing_set:
                                self.batches["updates"].append(item["params"])
                            else:
                                self.batches["inserts"].append(item["params"])

                self.batches["to_check"] = []

            if self.batches["to_delete"]:
                sub_batch_size = 100
                for i in range(0, len(self.batches["to_delete"]), sub_batch_size):
                    sub_batch = self.batches["to_delete"][i : i + sub_batch_size]

                    placeholders = []
                    params = {}
                    for idx, item in enumerate(sub_batch):
                        key_suffix = f"_{idx}"
                        placeholders.append(
                            f"(:info_hash{key_suffix}, :season{key_suffix})"
                        )
                        params[f"info_hash{key_suffix}"] = item["info_hash"]
                        params[f"season{key_suffix}"] = item["season"]

                    async with database.transaction():
                        delete_query = f"""
                            DELETE FROM torrents
                            WHERE (info_hash, season) IN (
                                {",".join(placeholders)}
                            )
                            AND episode IS NULL
                        """
                        await database.execute(delete_query, params)

                if len(self.batches["to_delete"]) > 0:
                    logger.log(
                        "SCRAPER",
                        f"Deleted {len(self.batches['to_delete'])} season-only entries in batch",
                    )
                self.batches["to_delete"] = []

            if self.batches["inserts"]:
                sub_batch_size = 100
                for i in range(0, len(self.batches["inserts"]), sub_batch_size):
                    sub_batch = self.batches["inserts"][i : i + sub_batch_size]
                    async with database.transaction():
                        insert_query = f"""
                            INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
                            INTO torrents
                            VALUES (:media_id, :info_hash, :file_index, :season, :episode, :title, :seeders, :size, :tracker, :sources, :parsed, :timestamp)
                            {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
                        """
                        await database.execute_many(insert_query, sub_batch)

                if len(self.batches["inserts"]) > 0:
                    logger.log(
                        "SCRAPER",
                        f"Inserted {len(self.batches['inserts'])} new torrents in batch",
                    )
                self.batches["inserts"] = []

            if self.batches["updates"]:
                sub_batch_size = 100
                for i in range(0, len(self.batches["updates"]), sub_batch_size):
                    sub_batch = self.batches["updates"][i : i + sub_batch_size]
                    async with database.transaction():
                        update_query = """
                            UPDATE torrents 
                            SET title = :title,
                                file_index = :file_index,
                                size = :size,
                                seeders = :seeders,
                                tracker = :tracker,
                                sources = :sources,
                                parsed = :parsed,
                                timestamp = :timestamp,
                                media_id = :media_id
                            WHERE info_hash = :info_hash 
                            AND (:season IS NULL AND season IS NULL OR season = :season)
                            AND (:episode IS NULL AND episode IS NULL OR episode = :episode)
                        """
                        await database.execute_many(update_query, sub_batch)

                if len(self.batches["updates"]) > 0:
                    logger.log(
                        "SCRAPER",
                        f"Updated {len(self.batches['updates'])} existing torrents in batch",
                    )
                self.batches["updates"] = []

            self.error_backoff = 1

        except Exception as e:
            await self._handle_error(e)

    async def _process_file_info(self, file_info: dict, media_id: str = None):
        try:
            params = {
                "info_hash": file_info["info_hash"],
                "file_index": file_info["index"],
                "season": file_info["season"],
                "episode": file_info["episode"],
                "title": file_info["title"],
                "seeders": file_info["seeders"],
                "size": file_info["size"],
                "tracker": file_info["tracker"],
                "sources": orjson.dumps(file_info["sources"]).decode("utf-8"),
                "parsed": orjson.dumps(
                    file_info["parsed"], default=default_dump
                ).decode("utf-8"),
                "timestamp": time.time(),
                "media_id": media_id,
            }

            self.batches["to_check"].append(
                {
                    "info_hash": file_info["info_hash"],
                    "season": file_info["season"],
                    "episode": file_info["episode"],
                    "params": params,
                }
            )

            if file_info["episode"] is not None:
                self.batches["to_delete"].append(
                    {"info_hash": file_info["info_hash"], "season": file_info["season"]}
                )

            await self._check_batch_size()

        finally:
            self.queue.task_done()

    async def _check_batch_size(self):
        if any(len(batch) >= self.batch_size for batch in self.batches.values()):
            await self._flush_batch()
            self.error_backoff = 1

    async def _handle_error(self, e: Exception):
        current_time = time.time()
        if current_time - self.last_error_time < 5:
            self.error_backoff = min(self.error_backoff * 2, 30)
        else:
            self.error_backoff = 1

        self.last_error_time = current_time
        logger.warning(f"Database error in torrent batch processing: {e}")
        logger.warning(f"Waiting {self.error_backoff} seconds before retry")
        await asyncio.sleep(self.error_backoff)


torrent_update_queue = TorrentUpdateQueue()

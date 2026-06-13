from __future__ import annotations

import logging
import re
import time as time_module
from datetime import datetime, timedelta

from daily_us.config import AppConfig, WatcherConfig
from daily_us.site import AudioNotAvailableYet, PostRef, UsInsightClient
from daily_us.storage import SeenStore
from daily_us.telegram import TelegramClient

LOGGER = logging.getLogger(__name__)
DEFAULT_SEED_LIMIT = 100


def poll_once(
    config: AppConfig,
    ignore_schedule: bool = True,
    watcher_name: str | None = None,
) -> None:
    store = SeenStore(config.storage.database_path)
    telegram = TelegramClient(config.telegram)
    now = datetime.now().time()
    watchers = [
        watcher
        for watcher in config.watchers
        if (watcher_name is None or watcher.name == watcher_name)
        and (ignore_schedule or watcher.is_active_now(now))
    ]

    if not watchers:
        LOGGER.info("No watcher matched or is active now.")
        return

    with UsInsightClient(config.site) as client:
        for watcher in watchers:
            _process_watcher(client, store, telegram, config, watcher)


def send_latest_for_test(
    config: AppConfig,
    watcher_name: str | None = None,
    limit: int = 1,
) -> None:
    telegram = TelegramClient(config.telegram)
    watchers = [
        watcher
        for watcher in config.watchers
        if watcher_name is None or watcher.name == watcher_name
    ]

    if not watchers:
        raise RuntimeError(f"No watcher matched: {watcher_name}")

    with UsInsightClient(config.site) as client:
        for watcher in watchers:
            _process_latest_for_test(client, telegram, config, watcher, limit)


def send_latest_body_for_test(
    config: AppConfig,
    watcher_name: str | None = None,
    limit: int = 1,
) -> None:
    telegram = TelegramClient(config.telegram)
    watchers = [
        watcher
        for watcher in config.watchers
        if watcher_name is None or watcher.name == watcher_name
    ]

    if not watchers:
        raise RuntimeError(f"No watcher matched: {watcher_name}")

    with UsInsightClient(config.site) as client:
        for watcher in watchers:
            _process_latest_body_for_test(client, telegram, watcher, limit)


def seed_seen_posts(
    config: AppConfig,
    watcher_name: str | None = None,
    limit: int = DEFAULT_SEED_LIMIT,
) -> None:
    if not config.watchers:
        raise RuntimeError("No watchers are configured in config.yaml.")

    store = SeenStore(config.storage.database_path)
    watchers = [
        watcher
        for watcher in config.watchers
        if watcher_name is None or watcher.name == watcher_name
    ]

    if not watchers:
        raise RuntimeError(f"No watcher matched: {watcher_name}")

    failed_watchers: list[str] = []
    with UsInsightClient(config.site) as client:
        for watcher in watchers:
            try:
                _seed_seen_for_watcher(client, store, watcher, limit)
            except Exception:
                failed_watchers.append(watcher.name)
                LOGGER.exception("Failed to seed seen history for watcher: %s", watcher.name)

    if failed_watchers:
        raise RuntimeError(f"seed-seen failed for watcher(s): {', '.join(failed_watchers)}")


def _seed_seen_for_watcher(
    client: UsInsightClient,
    store: SeenStore,
    watcher: WatcherConfig,
    limit: int,
) -> None:
    LOGGER.info(
        "Seeding seen history for watcher %s with latest %s matching post(s).",
        watcher.name,
        limit,
    )
    posts = client.find_posts(watcher.title_contains, limit)
    posts = _filter_excluded_posts(watcher, posts)
    posts_to_seed = _filter_posts_for_seed(watcher, posts)
    seeded_count = 0
    already_seen_count = 0

    for post in posts_to_seed:
        if store.has_seen(watcher.name, post.post_id):
            already_seen_count += 1
            LOGGER.info("Already seeded: %s", post.title)
            continue

        store.mark_seen(watcher.name, post.post_id, post.title, post.url)
        seeded_count += 1
        LOGGER.info("Seeded seen post: %s", post.title)

    LOGGER.info(
        "Seed complete for watcher %s: seeded=%s already_seen=%s skipped=%s found=%s",
        watcher.name,
        seeded_count,
        already_seen_count,
        len(posts) - len(posts_to_seed),
        len(posts),
    )


def run_forever(config: AppConfig) -> None:
    store = SeenStore(config.storage.database_path)
    telegram = TelegramClient(config.telegram)
    next_run: dict[str, datetime] = {watcher.name: datetime.min for watcher in config.watchers}

    LOGGER.info("Poller started with %s watcher(s).", len(config.watchers))
    while True:
        try:
            now = datetime.now()
            due_watchers = [
                watcher
                for watcher in config.watchers
                if watcher.is_active_now(now.time()) and now >= next_run[watcher.name]
            ]

            if due_watchers:
                with UsInsightClient(config.site) as client:
                    for watcher in due_watchers:
                        try:
                            _process_watcher(client, store, telegram, config, watcher)
                        except Exception:
                            LOGGER.exception("Watcher failed: %s", watcher.name)
                        finally:
                            next_run[watcher.name] = datetime.now() + timedelta(
                                minutes=watcher.interval_minutes
                            )
        except Exception:
            LOGGER.exception("Poller loop failed; continuing after sleep.")
        finally:
            time_module.sleep(30)


def _process_watcher(
    client: UsInsightClient,
    store: SeenStore,
    telegram: TelegramClient,
    config: AppConfig,
    watcher: WatcherConfig,
) -> None:
    LOGGER.info("Checking watcher: %s", watcher.name)
    posts = client.find_posts(watcher.title_contains, watcher.max_posts_per_poll)
    _mark_prior_posts_seen_if_today_exists(store, watcher, posts)
    posts = _filter_excluded_posts(watcher, posts)
    posts = _filter_posts_for_watcher(watcher, posts)

    for post in posts:
        if store.has_seen(watcher.name, post.post_id):
            LOGGER.info("Already sent: %s", post.title)
            continue

        if not watcher.send_audio:
            body_text = client.fetch_post_body_text(post)
            if _send_body_messages(telegram, body_text, post.title):
                store.mark_seen(watcher.name, post.post_id, post.title, post.url)
                LOGGER.info("Sent body-only post to Telegram: %s", post.title)
            continue

        try:
            audio = client.download_audio_from_post(
                post,
                config.storage.download_dir,
                watcher.audio_filename_template,
            )
        except AudioNotAvailableYet:
            LOGGER.info("Audio is not available yet; will retry on next poll: %s", post.title)
            continue
        audio_caption = audio.path.stem
        telegram.send_audio(audio.path, audio_caption)
        store.mark_seen(watcher.name, post.post_id, post.title, post.url)
        _send_body_messages(telegram, audio.body_text, post.title)
        LOGGER.info("Sent to Telegram: %s", post.title)


def _process_latest_for_test(
    client: UsInsightClient,
    telegram: TelegramClient,
    config: AppConfig,
    watcher: WatcherConfig,
    limit: int,
) -> None:
    LOGGER.info("Checking latest %s test post(s) for watcher: %s", limit, watcher.name)
    posts = client.find_posts(watcher.title_contains, limit)
    posts = _filter_excluded_posts(watcher, posts)
    if not posts:
        LOGGER.info("No candidate post found for watcher: %s", watcher.name)
        return

    for index, post in enumerate(posts, start=1):
        if not watcher.send_audio:
            body_text = client.fetch_post_body_text(post)
            if _send_body_messages(telegram, body_text, post.title):
                LOGGER.info(
                    "Sent latest body-only test post %s/%s to Telegram: %s",
                    index,
                    len(posts),
                    post.title,
                )
            else:
                LOGGER.warning(
                    "Failed to send latest body-only test post %s/%s to Telegram: %s",
                    index,
                    len(posts),
                    post.title,
                )
            continue

        try:
            audio = client.download_audio_from_post(
                post,
                config.storage.download_dir,
                watcher.audio_filename_template,
            )
        except AudioNotAvailableYet:
            LOGGER.info("Audio is not available yet for latest test post: %s", post.title)
            continue
        audio_caption = audio.path.stem
        telegram.send_audio(audio.path, audio_caption)
        _send_body_messages(telegram, audio.body_text, post.title)
        LOGGER.info(
            "Sent latest test post %s/%s to Telegram without marking seen: %s",
            index,
            len(posts),
            post.title,
        )


def _process_latest_body_for_test(
    client: UsInsightClient,
    telegram: TelegramClient,
    watcher: WatcherConfig,
    limit: int,
) -> None:
    LOGGER.info("Checking latest %s body-only test post(s) for watcher: %s", limit, watcher.name)
    posts = client.find_posts(watcher.title_contains, limit)
    posts = _filter_excluded_posts(watcher, posts)
    if not posts:
        LOGGER.info("No candidate post found for watcher: %s", watcher.name)
        return

    for index, post in enumerate(posts, start=1):
        body_text = client.fetch_post_body_text(post)
        if _send_body_messages(telegram, body_text, post.title):
            LOGGER.info(
                "Sent latest body-only test post %s/%s to Telegram: %s",
                index,
                len(posts),
                post.title,
            )
        else:
            LOGGER.warning(
                "Failed to send latest body-only test post %s/%s to Telegram: %s",
                index,
                len(posts),
                post.title,
            )


def _send_body_messages(telegram: TelegramClient, body_text: str, post_title: str) -> bool:
    try:
        for message in _telegram_body_messages(body_text):
            telegram.send_message(message, parse_mode="MarkdownV2")
    except Exception:
        LOGGER.exception("Body message failed for post: %s", post_title)
        return False
    return True


def _filter_posts_for_watcher(watcher: WatcherConfig, posts: list[PostRef]) -> list[PostRef]:
    if not watcher.only_today:
        return posts

    today = datetime.now()
    filtered_posts = []
    for post in posts:
        if _title_matches_today(post.title, today):
            filtered_posts.append(post)
        else:
            LOGGER.info("Skipping non-today post for watcher %s: %s", watcher.name, post.title)
    return filtered_posts


def _filter_excluded_posts(watcher: WatcherConfig, posts: list[PostRef]) -> list[PostRef]:
    if not watcher.title_exclude_contains:
        return posts

    filtered_posts = []
    for post in posts:
        matched_excludes = [
            keyword for keyword in watcher.title_exclude_contains if keyword in post.title
        ]
        if matched_excludes:
            LOGGER.info(
                "Skipping post for watcher %s because title contains excluded keyword(s) %s: %s",
                watcher.name,
                ", ".join(matched_excludes),
                post.title,
            )
            continue
        filtered_posts.append(post)
    return filtered_posts


def _filter_posts_for_seed(watcher: WatcherConfig, posts: list[PostRef]) -> list[PostRef]:
    if not watcher.only_today:
        return posts

    today = datetime.now()
    filtered_posts = []
    for post in posts:
        if _title_matches_today(post.title, today):
            LOGGER.info(
                "Skipping today's post during seed-seen for only_today watcher %s: %s",
                watcher.name,
                post.title,
            )
            continue
        filtered_posts.append(post)
    return filtered_posts


def _mark_prior_posts_seen_if_today_exists(
    store: SeenStore,
    watcher: WatcherConfig,
    posts: list[PostRef],
) -> None:
    if not watcher.only_today:
        return

    today = datetime.now()
    if not any(_title_matches_today(post.title, today) for post in posts):
        return

    for post in posts:
        if _title_is_before_today(post.title, today) and not store.has_seen(watcher.name, post.post_id):
            store.mark_seen(watcher.name, post.post_id, post.title, post.url)
            LOGGER.info(
                "Marked prior-date post complete because today's post exists: %s",
                post.title,
            )


def _title_matches_today(title: str, today: datetime) -> bool:
    post_date = _date_from_title(title, today)
    if post_date is None:
        LOGGER.info("Post title has no Korean date; skipping for only_today: %s", title)
        return False

    return post_date.date() == today.date()


def _title_is_before_today(title: str, today: datetime) -> bool:
    post_date = _date_from_title(title, today)
    return post_date is not None and post_date.date() < today.date()


def _date_from_title(title: str, today: datetime) -> datetime | None:
    match = re.search(r"(\d{1,2})월\s*(\d{1,2})일", title)
    if not match:
        return None

    month = int(match.group(1))
    day = int(match.group(2))
    try:
        return datetime(year=today.year, month=month, day=day)
    except ValueError:
        LOGGER.info("Post title has invalid Korean date; skipping date comparison: %s", title)
        return None


def _telegram_body_messages(body_text: str) -> list[str]:
    max_plain_chunk_length = 3800
    body_text = body_text.strip()
    if not body_text:
        return []

    return _split_text_for_telegram(body_text, max_plain_chunk_length)


def _split_text_for_telegram(text: str, max_length: int) -> list[str]:
    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0
    inside_code_block = False

    def flush_current(close_code_block: bool = False) -> None:
        nonlocal current_lines, current_length
        if not current_lines:
            return
        if close_code_block and _is_code_fence_line(current_lines[-1]):
            current_lines = current_lines[:-1]
        elif close_code_block and current_lines != ["```"]:
            current_lines.append("```")
        chunk = "\n".join(current_lines).strip()
        if chunk and chunk != "```":
            chunks.append(chunk)
        current_lines = []
        current_length = 0

    def reopen_code_block() -> None:
        nonlocal current_lines, current_length
        current_lines = ["```"]
        current_length = 4

    for line in text.splitlines():
        line_length = len(line) + 1
        if _is_code_fence_line(line):
            if inside_code_block and current_lines == ["```"]:
                current_lines = []
                current_length = 0
                inside_code_block = False
                continue

            if current_lines and current_length + line_length > max_length:
                flush_current(close_code_block=inside_code_block)
                if inside_code_block:
                    inside_code_block = False
                    continue

            current_lines.append(line)
            current_length += line_length
            inside_code_block = not inside_code_block
            continue

        if line_length > max_length:
            if inside_code_block:
                flush_current(close_code_block=True)
                segment_length = max_length - 8
                for index in range(0, len(line), segment_length):
                    segment = line[index : index + segment_length].strip()
                    if segment:
                        chunks.append(f"```\n{segment}\n```")
                reopen_code_block()
            else:
                flush_current()
                for index in range(0, len(line), max_length):
                    chunks.append(line[index : index + max_length].strip())
            continue

        if current_lines and current_length + line_length > max_length:
            flush_current(close_code_block=inside_code_block)
            if inside_code_block:
                reopen_code_block()

        current_lines.append(line)
        current_length += line_length

    flush_current(close_code_block=inside_code_block)
    return [chunk for chunk in chunks if chunk]


def _is_code_fence_line(line: str) -> bool:
    return line.strip().startswith("```")

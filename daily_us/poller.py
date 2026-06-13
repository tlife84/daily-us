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


def poll_once(config: AppConfig, ignore_schedule: bool = True) -> None:
    store = SeenStore(config.storage.database_path)
    telegram = TelegramClient(config.telegram)
    now = datetime.now().time()
    watchers = [
        watcher
        for watcher in config.watchers
        if ignore_schedule or watcher.is_active_now(now)
    ]

    if not watchers:
        LOGGER.info("No watcher is active now.")
        return

    with UsInsightClient(config.site) as client:
        for watcher in watchers:
            _process_watcher(client, store, telegram, config, watcher)


def send_latest_for_test(config: AppConfig, watcher_name: str | None = None) -> None:
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
            _process_latest_for_test(client, telegram, config, watcher)


def send_latest_body_for_test(config: AppConfig, watcher_name: str | None = None) -> None:
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
            _process_latest_body_for_test(client, telegram, watcher)


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
    posts = _filter_posts_for_watcher(watcher, posts)

    for post in posts:
        if store.has_seen(watcher.name, post.post_id):
            LOGGER.info("Already sent: %s", post.title)
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
) -> None:
    LOGGER.info("Checking latest test post for watcher: %s", watcher.name)
    posts = client.find_posts(watcher.title_contains, 1)
    if not posts:
        LOGGER.info("No candidate post found for watcher: %s", watcher.name)
        return

    post = posts[0]
    try:
        audio = client.download_audio_from_post(
            post,
            config.storage.download_dir,
            watcher.audio_filename_template,
        )
    except AudioNotAvailableYet:
        LOGGER.info("Audio is not available yet for latest test post: %s", post.title)
        return
    audio_caption = audio.path.stem
    telegram.send_audio(audio.path, audio_caption)
    _send_body_messages(telegram, audio.body_text, post.title)
    LOGGER.info("Sent latest test post to Telegram without marking seen: %s", post.title)


def _process_latest_body_for_test(
    client: UsInsightClient,
    telegram: TelegramClient,
    watcher: WatcherConfig,
) -> None:
    LOGGER.info("Checking latest body-only test post for watcher: %s", watcher.name)
    posts = client.find_posts(watcher.title_contains, 1)
    if not posts:
        LOGGER.info("No candidate post found for watcher: %s", watcher.name)
        return

    post = posts[0]
    body_text = client.fetch_post_body_text(post)
    _send_body_messages(telegram, body_text, post.title)
    LOGGER.info("Sent latest body-only test post to Telegram: %s", post.title)


def _send_body_messages(telegram: TelegramClient, body_text: str, post_title: str) -> None:
    try:
        for message in _telegram_body_messages(body_text):
            telegram.send_message(message, parse_mode="MarkdownV2")
    except Exception:
        LOGGER.exception("Body message failed for post: %s", post_title)


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

    chunks = _split_text_for_telegram(body_text, max_plain_chunk_length)
    return [
        _format_body_chunk_as_markdown(chunk, emphasize_first_line=index == 0)
        for index, chunk in enumerate(chunks)
    ]


def _split_text_for_telegram(text: str, max_length: int) -> list[str]:
    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0

    for line in text.splitlines():
        line_length = len(line) + 1
        if current_lines and current_length + line_length > max_length:
            chunks.append("\n".join(current_lines).strip())
            current_lines = []
            current_length = 0

        if line_length > max_length:
            if current_lines:
                chunks.append("\n".join(current_lines).strip())
                current_lines = []
                current_length = 0
            for index in range(0, len(line), max_length):
                chunks.append(line[index : index + max_length].strip())
            continue

        current_lines.append(line)
        current_length += line_length

    if current_lines:
        chunks.append("\n".join(current_lines).strip())
    return [chunk for chunk in chunks if chunk]


def _format_body_chunk_as_markdown(chunk: str, emphasize_first_line: bool) -> str:
    lines = chunk.splitlines()
    escaped_lines = [_escape_markdown_v2(line) for line in lines]
    if emphasize_first_line and escaped_lines:
        escaped_lines[0] = f"*{escaped_lines[0]}*"
    return "\n".join(escaped_lines)


def _escape_markdown_v2(text: str) -> str:
    special_chars = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{char}" if char in special_chars else char for char in text)

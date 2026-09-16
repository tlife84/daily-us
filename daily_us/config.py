from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from string import Formatter
from typing import Any

import yaml


@dataclass(frozen=True)
class SiteConfig:
    feed_url: str
    profile_dir: Path
    auth_state_path: Path
    session_storage_path: Path
    headless: bool
    navigation_timeout_ms: int


@dataclass(frozen=True)
class StorageConfig:
    database_path: Path
    download_dir: Path


@dataclass(frozen=True)
class TelegramConfig:
    bot_token_env: str
    chat_id_env: str
    chat_ids_env: str
    admin_chat_id_env: str


@dataclass(frozen=True)
class ActiveWindow:
    """One stretch of weekdays and hours a watcher runs in.

    A watcher holds several of these, so its hours can differ per weekday.
    """

    days: tuple[int, ...] | None
    hours: tuple[time, time] | None

    def matches(self, now: datetime) -> bool:
        """Whether the given moment falls inside this window.

        Args:
            now: Moment to test.

        Returns:
            True when the weekday and the time of day both match. A missing day or hour
            list means that half places no limit.
        """
        if self.days is not None and now.weekday() not in self.days:
            return False
        if self.hours is None:
            return True

        start, end = self.hours
        current = now.time()
        # 끝 시각이 시작보다 이르면 자정을 넘기는 구간으로 읽는다
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end


@dataclass(frozen=True)
class WatcherConfig:
    name: str
    title_contains: str
    title_exclude_contains: tuple[str, ...]
    send_audio: bool
    send_pdf: bool
    send_body_as_image: bool
    audio_filename_template: str | None
    only_today: bool
    schedules: tuple[ActiveWindow, ...] | None
    interval_minutes: int
    max_posts_per_poll: int

    def is_active_at(self, now: datetime) -> bool:
        """Whether the watcher should poll at the given moment.

        Args:
            now: Moment to test.

        Returns:
            True when any of the watcher's windows matches, or when it has no window at all.
        """
        if self.schedules is None:
            return True
        return any(window.matches(now) for window in self.schedules)


@dataclass(frozen=True)
class AppConfig:
    site: SiteConfig
    storage: StorageConfig
    telegram: TelegramConfig
    watchers: list[WatcherConfig]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    base_dir = config_path.parent

    site = raw.get("site", {})
    storage = raw.get("storage", {})
    telegram = raw.get("telegram", {})
    watchers = raw.get("watchers", [])

    return AppConfig(
        site=SiteConfig(
            feed_url=str(site["feed_url"]),
            profile_dir=_resolve(base_dir, site.get("profile_dir", ".us-insight-profile")),
            auth_state_path=_resolve(base_dir, site.get("auth_state_path", "data/auth_state.json")),
            session_storage_path=_resolve(
                base_dir,
                site.get("session_storage_path", "data/session_storage.json"),
            ),
            headless=bool(site.get("headless", True)),
            navigation_timeout_ms=int(site.get("navigation_timeout_ms", 45000)),
        ),
        storage=StorageConfig(
            database_path=_resolve(base_dir, storage.get("database_path", "data/seen.sqlite3")),
            download_dir=_resolve(base_dir, storage.get("download_dir", "downloads")),
        ),
        telegram=TelegramConfig(
            bot_token_env=str(telegram.get("bot_token_env", "TELEGRAM_BOT_TOKEN")),
            chat_id_env=str(telegram.get("chat_id_env", "TELEGRAM_CHAT_ID")),
            chat_ids_env=str(telegram.get("chat_ids_env", "TELEGRAM_CHAT_IDS")),
            admin_chat_id_env=str(
                telegram.get("admin_chat_id_env", "TELEGRAM_ADMIN_CHAT_ID")
            ),
        ),
        watchers=[_parse_watcher(item) for item in watchers],
    )


def _parse_watcher(raw: dict[str, Any]) -> WatcherConfig:
    name = str(raw["name"])
    send_audio = bool(raw.get("send_audio", True))
    send_pdf = bool(raw.get("send_pdf", False))
    if send_audio and send_pdf:
        raise ValueError(f"Watcher {name!r} cannot set both send_audio and send_pdf to true")

    audio_filename_template = (
        str(raw["audio_filename_template"])
        if raw.get("audio_filename_template") is not None
        else None
    )
    _validate_audio_filename_template(audio_filename_template)

    return WatcherConfig(
        name=name,
        title_contains=str(raw.get("title_contains", "")),
        title_exclude_contains=tuple(str(item) for item in raw.get("title_exclude_contains", [])),
        send_audio=send_audio,
        send_pdf=send_pdf,
        send_body_as_image=bool(raw.get("send_body_as_image", False)),
        audio_filename_template=audio_filename_template,
        only_today=bool(raw.get("only_today", False)),
        schedules=_parse_schedules(raw),
        interval_minutes=int(raw.get("interval_minutes", 10)),
        max_posts_per_poll=int(raw.get("max_posts_per_poll", 5)),
    )


def _validate_audio_filename_template(template: str | None) -> None:
    if not template:
        return

    allowed_fields = {"title", "date", "mm-dd"}
    for _literal_text, field_name, _format_spec, _conversion in Formatter().parse(template):
        if field_name is None:
            continue
        if field_name not in allowed_fields:
            allowed = ", ".join(sorted(allowed_fields))
            raise ValueError(
                "audio_filename_template contains unsupported placeholder "
                f"{field_name!r}. Allowed placeholders: {allowed}"
            )

    template.format(title="title", date="01-01", **{"mm-dd": "01-01"})


def _parse_schedules(raw: dict[str, Any]) -> tuple[ActiveWindow, ...] | None:
    """Read a watcher's run windows from config.

    Accepts either a `schedules` list, whose entries pair `days` with `hours`, or a single
    `active_days` / `active_hours` pair for watchers that keep the same hours every day.

    Args:
        raw: Raw watcher mapping read from config.yaml.

    Returns:
        The windows the watcher runs in, or None when it is never held back.
    """
    schedules = raw.get("schedules")
    has_single_window = raw.get("active_days") is not None or raw.get("active_hours") is not None
    if schedules and has_single_window:
        raise ValueError(
            "schedules cannot be combined with active_days or active_hours; "
            "move the single window into schedules"
        )

    if not schedules:
        window = ActiveWindow(
            days=_parse_active_days(raw.get("active_days")),
            hours=_parse_hours(raw.get("active_hours")),
        )
        # 요일도 시각도 없으면 제한이 없는 워처로 둔다
        if window.days is None and window.hours is None:
            return None
        return (window,)

    if not isinstance(schedules, list):
        raise ValueError("schedules must be a list of days/hours entries")

    windows = []
    for entry in schedules:
        if not isinstance(entry, dict):
            raise ValueError("Each schedules entry must be a mapping with days and/or hours")
        windows.append(
            ActiveWindow(
                days=_parse_active_days(entry.get("days")),
                hours=_parse_hours(entry.get("hours")),
            )
        )
    return tuple(windows)


def _parse_hours(values: Any) -> tuple[time, time] | None:
    """Parse one start/end pair of HH:MM values.

    Args:
        values: Two HH:MM strings, or nothing when the window covers the whole day.

    Returns:
        The parsed start and end times, or None when no hours were given.
    """
    if not values:
        return None
    if len(values) != 2:
        raise ValueError("Watcher hours must contain exactly two HH:MM values")
    return (_parse_time(values[0]), _parse_time(values[1]))


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _parse_active_days(values: Any) -> tuple[int, ...] | None:
    if values is None:
        return None
    if isinstance(values, (int, str)):
        values = [values]

    day_map = {
        "mon": 0,
        "monday": 0,
        "tue": 1,
        "tuesday": 1,
        "wed": 2,
        "wednesday": 2,
        "thu": 3,
        "thursday": 3,
        "fri": 4,
        "friday": 4,
        "sat": 5,
        "saturday": 5,
        "sun": 6,
        "sunday": 6,
    }

    parsed_days = []
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError("Watcher days must be a string, integer, or list") from exc

    for value in iterator:
        if isinstance(value, int):
            if value < 0 or value > 6:
                raise ValueError("Watcher day numbers must be between 0 and 6")
            parsed_days.append(value)
            continue

        key = str(value).strip().lower()
        if key not in day_map:
            allowed = ", ".join(sorted(day_map))
            raise ValueError(f"Unsupported active day {value!r}. Allowed values: {allowed}")
        parsed_days.append(day_map[key])

    if not parsed_days:
        return None
    return tuple(dict.fromkeys(parsed_days))


def _resolve(base_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return base_dir / path

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


class SeenStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def has_seen(self, watcher_name: str, post_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select 1 from seen_posts where watcher_name = ? and post_id = ?",
                (watcher_name, post_id),
            ).fetchone()
        return row is not None

    def mark_seen(self, watcher_name: str, post_id: str, post_title: str, post_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into seen_posts
                    (watcher_name, post_id, post_title, post_url)
                values (?, ?, ?, ?)
                """,
                (watcher_name, post_id, post_title, post_url),
            )

    def should_send_notification(self, key: str, cooldown_minutes: int) -> bool:
        now = datetime.now()
        with self._connect() as conn:
            row = conn.execute(
                "select sent_at from app_notifications where notification_key = ?",
                (key,),
            ).fetchone()
            if row:
                try:
                    sent_at = datetime.fromisoformat(str(row[0]))
                except ValueError:
                    sent_at = datetime.min
                if now - sent_at < timedelta(minutes=cooldown_minutes):
                    return False

            conn.execute(
                """
                insert into app_notifications (notification_key, sent_at)
                values (?, ?)
                on conflict(notification_key) do update set sent_at = excluded.sent_at
                """,
                (key, now.isoformat(timespec="seconds")),
            )
            return True

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists seen_posts (
                    watcher_name text not null,
                    post_id text not null,
                    post_title text not null,
                    post_url text not null,
                    seen_at text not null default current_timestamp,
                    primary key (watcher_name, post_id)
                )
                """
            )
            conn.execute(
                """
                create table if not exists app_notifications (
                    notification_key text not null primary key,
                    sent_at text not null
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

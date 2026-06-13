from __future__ import annotations

import sqlite3
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

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

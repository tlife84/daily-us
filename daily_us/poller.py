from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from daily_us.config import AppConfig, KST, WatcherConfig
from daily_us.site import (
    AudioNotAvailableYet,
    LoginRequired,
    PostBody,
    PostRef,
    UsInsightClient,
)
from daily_us.storage import SeenStore
from daily_us.telegram import MAX_ALBUM_ITEMS, TelegramClient
from daily_us.video import VideoNotAvailableYet, download_video

LOGGER = logging.getLogger(__name__)
DEFAULT_SEED_LIMIT = 100
LOGIN_ALERT_COOLDOWN_MINUTES = 60
POLL_FAILURE_ALERT_COOLDOWN_MINUTES = 60
ADMIN_ALERT_RETRY_INTERVAL_SECONDS = 600
ADMIN_ALERT_MAX_ATTEMPTS = 6


@dataclass(frozen=True)
class BodyDelivery:
    """Outcome of one body delivery attempt.

    ``ready`` separates "the post is still rendering, try again later" from a real failure, so an
    unfinished post does not alert the admin once an hour.
    """

    sent: bool
    ready: bool = True


def poll_once(
    config: AppConfig,
    ignore_schedule: bool = True,
    watcher_name: str | None = None,
) -> None:
    store = SeenStore(config.storage.database_path)
    telegram = TelegramClient(config.telegram)
    now = datetime.now()
    watchers = [
        watcher
        for watcher in config.watchers
        if (watcher_name is None or watcher.name == watcher_name)
        and (ignore_schedule or watcher.is_active_at(now))
    ]

    if not watchers:
        LOGGER.info("No watcher matched or is active now.")
        return

    with UsInsightClient(config.site) as client:
        for watcher in watchers:
            # 동영상은 인증 잠금 대기 후 시간대 재확인. 기존 워처는 선택한 조회를 그대로 수행
            if watcher.send_video_to_drive and not ignore_schedule and not watcher.is_active_at(datetime.now()):
                continue
            try:
                _process_watcher(client, store, telegram, config, watcher)
            except LoginRequired as exc:
                LOGGER.warning("Login is required while polling watcher %s: %s", watcher.name, exc)
                _notify_login_required(telegram, store, watcher.name, exc)
                break
            except Exception as exc:
                LOGGER.exception("Watcher failed: %s", watcher.name)
                _notify_poll_failure(telegram, watcher.name, "watcher failed", exc)


def send_latest_for_test(
    config: AppConfig,
    watcher_name: str | None = None,
    limit: int = 1,
    admin_only: bool = False,
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
            _process_latest_for_test(client, telegram, config, watcher, limit, admin_only)


def send_latest_body_for_test(
    config: AppConfig,
    watcher_name: str | None = None,
    limit: int = 1,
    admin_only: bool = False,
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
            _process_latest_body_for_test(client, telegram, watcher, limit, admin_only)


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
                if watcher.is_active_at(now) and now >= next_run[watcher.name]
            ]

            if due_watchers:
                with UsInsightClient(config.site) as client:
                    for watcher in due_watchers:
                        # 동영상만 시작 시각 재확인. 기존 워처는 이미 예정된 마지막 조회까지 수행
                        if watcher.send_video_to_drive and not watcher.is_active_at(datetime.now()):
                            continue
                        try:
                            _process_watcher(client, store, telegram, config, watcher)
                        except LoginRequired as exc:
                            LOGGER.warning(
                                "Login is required while polling watcher %s: %s",
                                watcher.name,
                                exc,
                            )
                            _notify_login_required(telegram, store, watcher.name, exc)
                            break
                        except Exception as exc:
                            LOGGER.exception("Watcher failed: %s", watcher.name)
                            _notify_poll_failure(
                                telegram,
                                watcher.name,
                                "watcher failed",
                                exc,
                            )
                        finally:
                            next_run[watcher.name] = _next_poll_at(watcher, datetime.now())
        except Exception as exc:
            LOGGER.exception("Poller loop failed; continuing after sleep.")
            try:
                _notify_poll_failure(telegram, "poller_loop", "poller loop failed", exc)
            except Exception:
                LOGGER.exception("Failed while sending poller loop failure alert.")
        finally:
            time_module.sleep(30)


def _next_poll_at(watcher: WatcherConfig, now: datetime) -> datetime:
    """정규수업은 고정 시각 격자, 기존 워처는 처리 완료 후 간격으로 다음 실행 계산.

    Args:
        watcher: 실행을 마친 워처 설정.
        now: 현재 시각. 반환값도 입력과 같은 시간대 사용.

    Returns:
        다음 폴링 시각.
    """
    interval = timedelta(minutes=watcher.interval_minutes)
    if not watcher.send_video_to_drive:
        return now + interval
    current = now.astimezone(KST)
    anchor = current.replace(hour=0, minute=0, second=0, microsecond=0)
    for window in watcher.schedules or ():
        if window.matches(current.replace(second=0, microsecond=0)) and window.hours:
            anchor = current.replace(
                hour=window.hours[0].hour, minute=window.hours[0].minute, second=0, microsecond=0,
            )
            break
    return now + interval - (current - anchor) % interval


def _process_watcher(
    client: UsInsightClient,
    store: SeenStore,
    telegram: TelegramClient,
    config: AppConfig,
    watcher: WatcherConfig,
) -> None:
    LOGGER.info("Checking watcher: %s", watcher.name)
    posts = client.find_posts(watcher.title_contains, watcher.max_posts_per_poll)
    _mark_prior_posts_seen(store, watcher, posts)
    posts = _filter_excluded_posts(watcher, posts)
    posts = _filter_posts_for_watcher(watcher, posts)

    for post in posts:
        if store.has_seen(watcher.name, post.post_id):
            LOGGER.info("Already sent: %s", post.title)
            continue

        if watcher.send_video_to_drive:
            try:
                _process_video_post(client, store, telegram, config, watcher, post)
            except VideoNotAvailableYet:
                LOGGER.info("Video is not available yet: %s", post.title)
            except LoginRequired:
                raise
            except Exception as exc:
                LOGGER.exception("Failed to deliver regular class video: %s", post.title)
                _notify_poll_failure_with_cooldown(
                    telegram, store, watcher.name, "failed to deliver video", exc, post,
                )
            continue

        if watcher.send_pdf:
            try:
                content = client.fetch_post_content(post, config.storage.download_dir)
            except Exception as exc:
                LOGGER.exception("Failed to fetch PDF content for post: %s", post.title)
                _notify_poll_failure(
                    telegram,
                    watcher.name,
                    "failed to fetch PDF content",
                    exc,
                    post,
                )
                continue

            has_pdf = bool(content.pdf_paths)
            if has_pdf and not _send_documents(telegram, content.pdf_paths, post.title):
                _notify_poll_failure(
                    telegram,
                    watcher.name,
                    "failed to send PDF document",
                    post=post,
                )
                continue

            if not content.pdf_paths:
                LOGGER.warning(
                    "No PDF attachment found; sending body only and marking seen: %s",
                    post.title,
                )

            body = _deliver_body(
                client,
                telegram,
                watcher,
                post,
                body=PostBody(text=content.body_text, is_ready=True),
            )
            if body.sent:
                store.mark_seen(watcher.name, post.post_id, post.title, post.url)
                if has_pdf:
                    LOGGER.info("Sent body and PDF(s) to Telegram: %s", post.title)
                else:
                    LOGGER.info("Sent PDF watcher post without PDF to Telegram: %s", post.title)
            elif body.ready:
                _notify_poll_failure(
                    telegram,
                    watcher.name,
                    "failed to send body message",
                    post=post,
                )
            continue

        if not watcher.send_audio:
            status = store.get_delivery_status(watcher.name, post.post_id)
            if status.body_sent:
                store.mark_seen(watcher.name, post.post_id, post.title, post.url)
                LOGGER.info(
                    "Completed previously sent body after audio delivery was disabled: %s",
                    post.title,
                )
                continue
            body = _deliver_body(client, telegram, watcher, post)
            if body.sent:
                store.mark_seen(watcher.name, post.post_id, post.title, post.url)
                LOGGER.info("Sent body-only post to Telegram: %s", post.title)
            elif body.ready:
                _notify_poll_failure(
                    telegram,
                    watcher.name,
                    "failed to send body-only message",
                    post=post,
                )
            continue

        _process_audio_post(client, store, telegram, config, watcher, post)


def _process_video_post(
    client: UsInsightClient,
    store: SeenStore,
    telegram: TelegramClient,
    config: AppConfig,
    watcher: WatcherConfig,
    post: PostRef,
) -> None:
    """본편 다운로드, 전 주 영상 삭제, Drive 업로드, 봇 링크 전달을 순서대로 수행.

    Args:
        client: 로그인된 게시글 조회 클라이언트.
        store: 중복 전송 및 업로드 ID 저장소.
        telegram: 기존 봇 수신자에게 링크를 전달할 클라이언트.
        config: 저장 경로와 Drive 설정.
        watcher: 정규수업 조회 설정.
        post: 이번에 확인할 게시글.
    """
    video = client.fetch_post_video(post)
    if video is None:
        return
    if watcher.only_today and video.published_at.astimezone(KST).date() != datetime.now(KST).date():
        LOGGER.info("Skipping video not published today: %s", post.title)
        return
    if config.drive is None:
        raise RuntimeError("Drive configuration is missing")
    # Drive 의존성은 실제 동영상 전달 경로에서만 로드
    from daily_us.drive import DriveClient

    # 게시글별 경로로 불완전 파일과 다른 게시글의 다운로드를 분리
    post_key = hashlib.sha256(post.post_id.encode()).hexdigest()[:20]
    directory = config.storage.download_dir / "regular-class" / post_key
    with DriveClient(config.drive) as drive:
        drive.check_folder()
        file_id = store.get_video_file_id(watcher.name, post.post_id)
        uploaded = drive.get_video(file_id, video.filename) if file_id else None
        if not file_id:
            existing = drive.find_videos(video.filename)
            if len(existing) > 1:
                raise RuntimeError(f"Drive에 같은 이름의 영상이 여러 개 있습니다: {video.filename}")
            if existing:
                uploaded = drive.get_video(existing[0]["id"], video.filename)
            file_id = uploaded["id"] if uploaded else drive.generate_id()
            # 삭제·업로드 전에 ID 기록. 완료 응답을 받지 못해도 다음 시도에서 같은 ID 조회
            store.save_video_file_id(watcher.name, post.post_id, file_id)
        if uploaded is None:
            path = download_video(video, directory)
            drive.delete_previous_week(video.filename)
            uploaded = drive.upload_video(path, file_id)
        # 완료 응답 유실 뒤에도 원격 크기를 확인한 다음 로컬 파일 제거
        local_path = directory / video.filename
        if local_path.exists() and int(uploaded.get("size", 0)) != local_path.stat().st_size:
            raise RuntimeError("Drive 영상과 로컬 영상의 크기가 달라 로컬 파일을 보존합니다.")
        local_path.unlink(missing_ok=True)
        link = uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        # 한계: 일부 수신자만 실패하면 다음 폴링에서 전체 수신자에게 재전송. 필요 시 수신자별 전달 이력으로 확장 가능
        telegram.send_message(f"정규수업 {video.filename.removesuffix('.mp4')}\n{link}")
        store.mark_seen(watcher.name, post.post_id, post.title, post.url)
        LOGGER.info("Delivered regular class video link: %s", video.filename)


def _process_audio_post(
    client: UsInsightClient,
    store: SeenStore,
    telegram: TelegramClient,
    config: AppConfig,
    watcher: WatcherConfig,
    post: PostRef,
) -> None:
    status = store.get_delivery_status(watcher.name, post.post_id)
    body_sent, audio_sent, failures = _deliver_audio_post(
        client,
        telegram,
        config,
        watcher,
        post,
        body_sent=status.body_sent,
        audio_sent=status.audio_sent,
        on_body_sent=lambda: store.mark_body_sent(
            watcher.name, post.post_id, post.title, post.url
        ),
        on_audio_sent=lambda: store.mark_audio_sent(
            watcher.name, post.post_id, post.title, post.url
        ),
    )

    if body_sent and audio_sent:
        store.mark_seen(watcher.name, post.post_id, post.title, post.url)
        LOGGER.info("Completed body and audio delivery: %s", post.title)

    for summary, exc in failures:
        _notify_poll_failure_with_cooldown(
            telegram,
            store,
            watcher.name,
            summary,
            exc,
            post,
        )


def _deliver_audio_post(
    client: UsInsightClient,
    telegram: TelegramClient,
    config: AppConfig,
    watcher: WatcherConfig,
    post: PostRef,
    *,
    body_sent: bool = False,
    audio_sent: bool = False,
    admin_only: bool = False,
    on_body_sent: Callable[[], None] | None = None,
    on_audio_sent: Callable[[], None] | None = None,
) -> tuple[bool, bool, list[tuple[str, Exception | None]]]:
    audio = None
    failures: list[tuple[str, Exception | None]] = []

    if not audio_sent:
        try:
            audio = client.download_audio_from_post(
                post,
                config.storage.download_dir,
                watcher.audio_filename_template,
            )
        except AudioNotAvailableYet:
            LOGGER.info("Audio is not available yet; checking body independently: %s", post.title)
        except Exception as exc:
            LOGGER.exception("Failed to fetch audio for post: %s", post.title)
            failures.append(("failed to fetch audio", exc))

    if not body_sent:
        # 오디오를 받으면서 같은 페이지에서 본문도 함께 읽어온다. 사진으로 보내는 워처는 캡처가
        # 별도 페이지를 열어야 하므로 이 본문을 쓰지 않는다.
        prepared = (
            PostBody(text=audio.body_text, is_ready=audio.body_ready)
            if audio is not None
            else None
        )

        try:
            delivery = _deliver_body(
                client, telegram, watcher, post, body=prepared, admin_only=admin_only
            )
        except Exception as exc:
            LOGGER.exception("Failed to fetch body for post: %s", post.title)
            failures.append(("failed to fetch audio watcher body", exc))
        else:
            if delivery.sent:
                if on_body_sent:
                    on_body_sent()
                body_sent = True
                LOGGER.info("Sent body to Telegram: %s", post.title)
            elif not delivery.ready:
                LOGGER.info("Body is not ready yet; will retry without marking sent: %s", post.title)
            else:
                failures.append(("failed to send audio watcher body message", None))

    if not audio_sent and audio is not None:
        try:
            telegram.send_audio(audio.path, audio.path.stem, admin_only=admin_only)
        except Exception as exc:
            LOGGER.exception("Failed to send audio for post: %s", post.title)
            failures.append(("failed to send audio", exc))
        else:
            if on_audio_sent:
                on_audio_sent()
            audio_sent = True
            LOGGER.info("Sent audio to Telegram: %s", post.title)

    return body_sent, audio_sent, failures


def _notify_login_required(
    telegram: TelegramClient,
    store: SeenStore,
    watcher_name: str,
    exc: Exception,
) -> None:
    if not store.should_send_notification("login_required", LOGIN_ALERT_COOLDOWN_MINUTES):
        LOGGER.info(
            "Login-required admin alert suppressed by %s minute cooldown.",
            LOGIN_ALERT_COOLDOWN_MINUTES,
        )
        return

    message = (
        "US Insight 로그인 세션이 만료된 것 같습니다.\n\n"
        f"감지 watcher: {watcher_name}\n"
        f"감지 시각: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        "다시 로그인:\n"
        "python -m daily_us login\n\n"
        "확인:\n"
        "python -m daily_us check-login\n\n"
        f"오류: {exc}"
    )
    _send_admin_alert_with_retry(telegram, message, "login-required")


def _notify_poll_failure_with_cooldown(
    telegram: TelegramClient,
    store: SeenStore,
    watcher_name: str,
    summary: str,
    exc: Exception | None = None,
    post: PostRef | None = None,
) -> None:
    post_key = post.post_id if post else "no-post"
    notification_key = f"poll_failure:{watcher_name}:{post_key}:{summary}"
    if not store.should_send_notification(
        notification_key,
        POLL_FAILURE_ALERT_COOLDOWN_MINUTES,
    ):
        LOGGER.info(
            "Poll-failure admin alert suppressed by %s minute cooldown: %s",
            POLL_FAILURE_ALERT_COOLDOWN_MINUTES,
            notification_key,
        )
        return
    _notify_poll_failure(telegram, watcher_name, summary, exc, post)


def _notify_poll_failure(
    telegram: TelegramClient,
    watcher_name: str,
    summary: str,
    exc: Exception | None = None,
    post: PostRef | None = None,
) -> None:
    lines = [
        "daily-us poller 실패",
        "",
        f"watcher: {watcher_name}",
        f"시각: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"요약: {summary}",
    ]
    if post:
        lines.extend(
            [
                "",
                f"게시글: {post.title}",
                f"URL: {post.url}",
            ]
        )
    if exc:
        lines.extend(["", f"오류: {type(exc).__name__}: {exc}"])

    _send_admin_alert_with_retry(telegram, "\n".join(lines), "poll failure")


def _send_admin_alert_with_retry(
    telegram: TelegramClient,
    message: str,
    context: str,
) -> bool:
    for attempt in range(1, ADMIN_ALERT_MAX_ATTEMPTS + 1):
        try:
            telegram.send_admin_message(message)
            LOGGER.info("Sent %s admin alert on attempt %s.", context, attempt)
            return True
        except Exception:
            LOGGER.exception(
                "Failed to send %s admin alert (attempt %s/%s).",
                context,
                attempt,
                ADMIN_ALERT_MAX_ATTEMPTS,
            )
        if attempt < ADMIN_ALERT_MAX_ATTEMPTS:
            time_module.sleep(ADMIN_ALERT_RETRY_INTERVAL_SECONDS)

    LOGGER.error(
        "Giving up on %s admin alert after %s attempt(s).",
        context,
        ADMIN_ALERT_MAX_ATTEMPTS,
    )
    return False


def _process_latest_for_test(
    client: UsInsightClient,
    telegram: TelegramClient,
    config: AppConfig,
    watcher: WatcherConfig,
    limit: int,
    admin_only: bool = False,
) -> None:
    # 이력 무시 테스트로 과거 수업의 원격 파일을 삭제하지 않도록 동영상 워처는 명시적으로 제외
    if watcher.send_video_to_drive:
        LOGGER.warning("Video upload is excluded from test-latest; use poll --watcher %s", watcher.name)
        return
    LOGGER.info("Checking latest %s test post(s) for watcher: %s", limit, watcher.name)
    posts = client.find_posts(watcher.title_contains, limit)
    posts = _filter_excluded_posts(watcher, posts)
    if not posts:
        LOGGER.info("No candidate post found for watcher: %s", watcher.name)
        return

    for index, post in enumerate(posts, start=1):
        if watcher.send_pdf:
            try:
                content = client.fetch_post_content(post, config.storage.download_dir)
            except Exception:
                LOGGER.exception("Failed to fetch latest PDF test post: %s", post.title)
                continue

            documents_sent = True
            if content.pdf_paths:
                documents_sent = _send_documents(
                    telegram, content.pdf_paths, post.title, admin_only=admin_only
                )
            else:
                LOGGER.warning(
                    "No PDF attachment found for latest test post; sending body only: %s",
                    post.title,
                )

            body_sent = False
            if documents_sent:
                body_sent = _deliver_body(
                    client,
                    telegram,
                    watcher,
                    post,
                    body=PostBody(text=content.body_text, is_ready=True),
                    admin_only=admin_only,
                ).sent

            if body_sent and documents_sent:
                LOGGER.info(
                    "Sent latest PDF test post %s/%s to Telegram without marking seen: %s",
                    index,
                    len(posts),
                    post.title,
                )
            else:
                LOGGER.warning(
                    "Failed to send latest PDF test post %s/%s to Telegram: %s",
                    index,
                    len(posts),
                    post.title,
                )
            continue

        if not watcher.send_audio:
            if _deliver_body(client, telegram, watcher, post, admin_only=admin_only).sent:
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

        body_sent, audio_sent, _failures = _deliver_audio_post(
            client,
            telegram,
            config,
            watcher,
            post,
            admin_only=admin_only,
        )
        sent_parts = [
            part for part, sent in (("body", body_sent), ("audio", audio_sent)) if sent
        ]
        if sent_parts:
            LOGGER.info(
                "Sent latest test post %s/%s parts=%s without marking seen: %s",
                index,
                len(posts),
                ",".join(sent_parts),
                post.title,
            )
        else:
            LOGGER.info("No ready content for latest test post: %s", post.title)


def _process_latest_body_for_test(
    client: UsInsightClient,
    telegram: TelegramClient,
    watcher: WatcherConfig,
    limit: int,
    admin_only: bool = False,
) -> None:
    LOGGER.info("Checking latest %s body-only test post(s) for watcher: %s", limit, watcher.name)
    posts = client.find_posts(watcher.title_contains, limit)
    posts = _filter_excluded_posts(watcher, posts)
    if not posts:
        LOGGER.info("No candidate post found for watcher: %s", watcher.name)
        return

    for index, post in enumerate(posts, start=1):
        if _deliver_body(client, telegram, watcher, post, admin_only=admin_only).sent:
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


def _deliver_body(
    client: UsInsightClient,
    telegram: TelegramClient,
    watcher: WatcherConfig,
    post: PostRef,
    body: PostBody | None = None,
    admin_only: bool = False,
) -> BodyDelivery:
    """Send a post body, as photos or as text depending on the watcher.

    Args:
        client: Site client used to capture or fetch the body.
        telegram: Telegram client the body is sent through.
        watcher: Watcher whose send_body_as_image decides the format.
        post: The post being delivered.
        body: Body the caller already fetched, to avoid loading the post twice. Ignored when the
            watcher sends photos, since a capture needs its own page.
        admin_only: Send to the admin chat instead of the normal recipients.

    Returns:
        Whether the body was sent, and whether the post was ready to send at all.
    """
    if watcher.send_body_as_image:
        with tempfile.TemporaryDirectory(prefix="daily-us-body-") as temp_dir:
            try:
                captured = client.capture_post_body_images(post, Path(temp_dir))
            except Exception:
                LOGGER.exception("Failed to capture body images for post: %s", post.title)
                return BodyDelivery(sent=False)

            if not captured.is_ready:
                LOGGER.info("Body is not ready to capture yet; will retry: %s", post.title)
                return BodyDelivery(sent=False, ready=False)

            sent = _send_body_images(
                telegram, captured.image_paths, post.title, admin_only=admin_only
            )
        return BodyDelivery(sent=sent)

    prepared = body if body is not None else client.fetch_post_body(post)
    if not prepared.is_ready:
        LOGGER.info("Body is not ready to send yet; will retry: %s", post.title)
        return BodyDelivery(sent=False, ready=False)

    return BodyDelivery(
        sent=_send_body_messages(telegram, prepared.text, post.title, admin_only=admin_only)
    )


def _send_body_images(
    telegram: TelegramClient,
    image_paths: list[Path],
    post_title: str,
    admin_only: bool = False,
) -> bool:
    """Send body slices as albums, ringing only once for the whole post.

    Args:
        telegram: Telegram client the photos are sent through.
        image_paths: Body slices in reading order.
        post_title: Post title, used for log messages only.
        admin_only: Send to the admin chat instead of the normal recipients.

    Returns:
        True when every album was delivered.
    """
    if not image_paths:
        LOGGER.warning("Body capture produced no images for post: %s", post_title)
        return False

    try:
        for start in range(0, len(image_paths), MAX_ALBUM_ITEMS):
            batch = image_paths[start : start + MAX_ALBUM_ITEMS]
            # Only the first album rings, so a post that arrives as a dozen photos notifies once.
            telegram.send_photo_album(
                batch,
                admin_only=admin_only,
                silent=start > 0,
            )
    except Exception:
        LOGGER.exception("Body image delivery failed for post: %s", post_title)
        return False
    return True


def _send_body_messages(
    telegram: TelegramClient,
    body_text: str,
    post_title: str,
    admin_only: bool = False,
) -> bool:
    try:
        for message in _telegram_body_messages(body_text):
            telegram.send_message(message, parse_mode="MarkdownV2", admin_only=admin_only)
    except Exception:
        LOGGER.exception("Body message failed for post: %s", post_title)
        return False
    return True


def _send_documents(
    telegram: TelegramClient,
    paths: list[Path],
    post_title: str,
    admin_only: bool = False,
) -> bool:
    try:
        for path in paths:
            telegram.send_document(path, admin_only=admin_only)
    except Exception:
        LOGGER.exception("Document send failed for post: %s", post_title)
        return False
    return True


def _filter_posts_for_watcher(watcher: WatcherConfig, posts: list[PostRef]) -> list[PostRef]:
    # 동영상 날짜는 제목에 없으므로 상세 API의 publishedAt으로 별도 판별
    if not watcher.only_today or watcher.send_video_to_drive:
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
    # 날짜가 없는 제목만으로 오늘 영상을 전송 완료 처리하지 않도록 seed 대상에서 제외
    if watcher.send_video_to_drive:
        return []
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


def _mark_prior_posts_seen(
    store: SeenStore,
    watcher: WatcherConfig,
    posts: list[PostRef],
) -> None:
    if not watcher.only_today or watcher.send_video_to_drive:
        return

    today = datetime.now()
    for post in posts:
        if _title_is_before_today(post.title, today) and not store.has_seen(watcher.name, post.post_id):
            store.mark_seen(watcher.name, post.post_id, post.title, post.url)
            LOGGER.info(
                "Marked prior-date post complete regardless of partial delivery: %s",
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
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(datetime(year=year, month=month, day=day))
        except ValueError:
            continue

    if not candidates:
        LOGGER.info("Post title has invalid Korean date; skipping date comparison: %s", title)
        return None
    return min(candidates, key=lambda candidate: abs(candidate - today))


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

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from daily_us.config import WatcherConfig
from daily_us.poller import (
    _date_from_title,
    _process_latest_for_test,
    _process_watcher,
    _title_is_before_today,
)
from daily_us.site import (
    AudioNotAvailableYet,
    DownloadedAudio,
    PostBody,
    PostRef,
    _extract_post_body_text_fallback,
    _is_post_body_ready,
)
from daily_us.storage import SeenStore


def _audio_watcher() -> WatcherConfig:
    return WatcherConfig(
        name="good_morning_damsaem",
        title_contains="굿모닝 담쌤",
        title_exclude_contains=(),
        send_audio=True,
        send_pdf=False,
        audio_filename_template="굿모닝 담쌤 {mm-dd}",
        only_today=True,
        active_days=None,
        active_hours=None,
        interval_minutes=10,
        max_posts_per_poll=5,
    )


def _dated_post(when: datetime, post_id: str = "post-1") -> PostRef:
    return PostRef(
        post_id=post_id,
        title=f"굿모닝 담쌤 {when.month}월 {when.day}일",
        url=f"https://example.test/{post_id}",
    )


class AudioDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = SeenStore(root / "seen.sqlite3")
        self.config = SimpleNamespace(storage=SimpleNamespace(download_dir=root / "downloads"))
        self.watcher = _audio_watcher()
        self.telegram = Mock()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sends_body_first_then_retries_only_audio(self) -> None:
        post = _dated_post(datetime.now())
        client = Mock()
        client.find_posts.return_value = [post]
        client.fetch_post_body.return_value = PostBody("본문", is_ready=True)
        client.download_audio_from_post.side_effect = AudioNotAvailableYet("not yet")

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        self.telegram.send_message.assert_called_once()
        self.telegram.send_audio.assert_not_called()
        self.assertFalse(self.store.has_seen(self.watcher.name, post.post_id))
        status = self.store.get_delivery_status(self.watcher.name, post.post_id)
        self.assertTrue(status.body_sent)
        self.assertFalse(status.audio_sent)

        client.fetch_post_body.reset_mock()
        audio_path = Path(self.temp_dir.name) / "굿모닝 담쌤.mp3"
        client.download_audio_from_post.side_effect = None
        client.download_audio_from_post.return_value = DownloadedAudio(audio_path, "본문")

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        client.fetch_post_body.assert_not_called()
        self.telegram.send_message.assert_called_once()
        self.telegram.send_audio.assert_called_once_with(audio_path, audio_path.stem)
        self.assertTrue(self.store.has_seen(self.watcher.name, post.post_id))
        self.assertEqual(
            self.store.get_delivery_status(self.watcher.name, post.post_id).body_sent,
            False,
        )

    def test_does_not_latch_placeholder_body_as_sent(self) -> None:
        post = _dated_post(datetime.now())
        client = Mock()
        client.find_posts.return_value = [post]
        client.download_audio_from_post.side_effect = AudioNotAvailableYet("not yet")
        client.fetch_post_body.return_value = PostBody(
            "스크립트 준비중",
            is_ready=False,
        )

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        self.telegram.send_message.assert_not_called()
        self.assertFalse(
            self.store.get_delivery_status(self.watcher.name, post.post_id).body_sent
        )

        client.fetch_post_body.return_value = PostBody("완성된 본문", is_ready=True)
        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        self.telegram.send_message.assert_called_once()
        self.assertTrue(
            self.store.get_delivery_status(self.watcher.name, post.post_id).body_sent
        )

    def test_body_failure_does_not_block_audio_and_alert_is_cooled_down(self) -> None:
        post = _dated_post(datetime.now())
        audio_path = Path(self.temp_dir.name) / "굿모닝 담쌤.mp3"
        client = Mock()
        client.find_posts.return_value = [post]
        client.download_audio_from_post.return_value = DownloadedAudio(audio_path, "본문")
        client.fetch_post_body.return_value = PostBody("본문", is_ready=True)
        self.telegram.send_message.side_effect = RuntimeError("bad markdown")

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)
        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        self.telegram.send_audio.assert_called_once_with(audio_path, audio_path.stem)
        self.telegram.send_admin_message.assert_called_once()
        self.assertEqual(
            [call[0] for call in self.telegram.method_calls[:3]],
            ["send_message", "send_audio", "send_admin_message"],
        )
        status = self.store.get_delivery_status(self.watcher.name, post.post_id)
        self.assertFalse(status.body_sent)
        self.assertTrue(status.audio_sent)
        self.assertFalse(self.store.has_seen(self.watcher.name, post.post_id))

    def test_immediate_audio_reuses_same_page_body_and_sends_body_first(self) -> None:
        post = _dated_post(datetime.now(), post_id="ready")
        audio_path = Path(self.temp_dir.name) / "ready.mp3"
        client = Mock()
        client.find_posts.return_value = [post]
        client.download_audio_from_post.return_value = DownloadedAudio(audio_path, "본문")

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        client.fetch_post_body.assert_not_called()
        self.assertEqual(
            [call[0] for call in self.telegram.method_calls if call[0] != "send_admin_message"],
            ["send_message", "send_audio"],
        )
        self.assertTrue(self.store.has_seen(self.watcher.name, post.post_id))

    def test_latest_audio_test_uses_body_first_production_order(self) -> None:
        post = _dated_post(datetime.now(), post_id="test-latest")
        audio_path = Path(self.temp_dir.name) / "test-latest.mp3"
        client = Mock()
        client.find_posts.return_value = [post]
        client.download_audio_from_post.return_value = DownloadedAudio(audio_path, "본문")

        _process_latest_for_test(
            client,
            self.telegram,
            self.config,
            self.watcher,
            limit=1,
            admin_only=True,
        )

        client.fetch_post_body.assert_not_called()
        self.assertEqual(
            [call[0] for call in self.telegram.method_calls],
            ["send_message", "send_audio"],
        )

    def test_body_fetch_error_does_not_stop_later_posts(self) -> None:
        first = _dated_post(datetime.now(), post_id="first")
        second = _dated_post(datetime.now(), post_id="second")
        client = Mock()
        client.find_posts.return_value = [first, second]
        client.download_audio_from_post.side_effect = AudioNotAvailableYet("not yet")
        client.fetch_post_body.side_effect = [
            RuntimeError("temporary page failure"),
            PostBody("두 번째 본문", is_ready=True),
        ]

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        self.assertEqual(client.fetch_post_body.call_count, 2)
        self.telegram.send_message.assert_called_once()
        self.assertTrue(
            self.store.get_delivery_status(self.watcher.name, second.post_id).body_sent
        )

    def test_prior_date_is_completed_without_sending_missing_parts(self) -> None:
        post = _dated_post(datetime.now() - timedelta(days=1), post_id="yesterday")
        self.store.mark_body_sent(
            self.watcher.name,
            post.post_id,
            post.title,
            post.url,
        )
        client = Mock()
        client.find_posts.return_value = [post]

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        self.assertTrue(self.store.has_seen(self.watcher.name, post.post_id))
        client.fetch_post_body.assert_not_called()
        client.download_audio_from_post.assert_not_called()
        self.telegram.send_message.assert_not_called()
        self.telegram.send_audio.assert_not_called()

    def test_disabling_audio_completes_already_sent_body_without_duplicate(self) -> None:
        post = _dated_post(datetime.now(), post_id="audio-disabled")
        self.store.mark_body_sent(
            self.watcher.name,
            post.post_id,
            post.title,
            post.url,
        )
        client = Mock()
        client.find_posts.return_value = [post]
        body_only_watcher = replace(self.watcher, send_audio=False)

        _process_watcher(client, self.store, self.telegram, self.config, body_only_watcher)

        self.assertTrue(self.store.has_seen(self.watcher.name, post.post_id))
        client.fetch_post_body_text.assert_not_called()
        self.telegram.send_message.assert_not_called()

    def test_previous_year_date_is_expired_on_new_years_day(self) -> None:
        self.assertTrue(
            _title_is_before_today(
                "굿모닝 담쌤 12월 31일",
                datetime(2027, 1, 1, 7, 0),
            )
        )

    def test_leap_day_year_inference_does_not_raise(self) -> None:
        self.assertEqual(
            _date_from_title("굿모닝 담쌤 2월 29일", datetime(2028, 9, 1)),
            datetime(2028, 2, 29),
        )

    def test_body_readiness_rejects_empty_and_script_placeholder(self) -> None:
        self.assertFalse(_is_post_body_ready(""))
        self.assertFalse(_is_post_body_ready("스크립트 준비 중"))
        self.assertTrue(_is_post_body_ready("실제 본문"))

        page = Mock()
        page.locator.return_value.inner_text.return_value = (
            "Beta\n스크립트 준비중\n투자 유의사항 펼치기"
        )
        self.assertEqual(_extract_post_body_text_fallback(page), "")


if __name__ == "__main__":
    unittest.main()

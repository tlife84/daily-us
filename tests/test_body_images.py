from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from daily_us.config import WatcherConfig
from daily_us.poller import _deliver_body, _process_watcher
from daily_us.site import CapturedPostBody, PostBody, PostRef
from daily_us.storage import SeenStore
from daily_us.telegram import MAX_ALBUM_ITEMS


def _image_watcher(send_body_as_image: bool = True) -> WatcherConfig:
    return WatcherConfig(
        name="always_date",
        title_contains="언제나 데이트",
        title_exclude_contains=("영상",),
        send_audio=False,
        send_pdf=False,
        send_body_as_image=send_body_as_image,
        audio_filename_template=None,
        only_today=False,
        schedules=None,
        interval_minutes=60,
        max_posts_per_poll=5,
    )


def _post() -> PostRef:
    return PostRef(
        post_id="29552",
        title="언제나 데이트 8월 25일",
        url="https://example.test/secrets/29552",
    )


class BodyImageDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = SeenStore(self.root / "seen.sqlite3")
        self.config = SimpleNamespace(
            storage=SimpleNamespace(download_dir=self.root / "downloads")
        )
        self.telegram = Mock()
        self.watcher = _image_watcher()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _client_capturing(self, count: int) -> Mock:
        client = Mock()
        client.find_posts.return_value = [_post()]

        def capture(_post: PostRef, output_dir: Path) -> CapturedPostBody:
            output_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for index in range(count):
                path = output_dir / f"29552-{index:02d}.png"
                path.write_bytes(b"png")
                paths.append(path)
            return CapturedPostBody(image_paths=paths, is_ready=True)

        client.capture_post_body_images.side_effect = capture
        return client

    def test_batches_into_albums_and_rings_only_once(self) -> None:
        client = self._client_capturing(16)

        result = _deliver_body(client, self.telegram, self.watcher, _post())

        self.assertTrue(result.sent)
        self.assertEqual(self.telegram.send_photo_album.call_count, 2)
        first, second = self.telegram.send_photo_album.call_args_list

        self.assertEqual(len(first.args[0]), MAX_ALBUM_ITEMS)
        self.assertFalse(first.kwargs["silent"])

        self.assertEqual(len(second.args[0]), 6)
        self.assertTrue(second.kwargs["silent"])

        # post.title is the raw feed-card text on this site, so nothing is
        # captioned onto the photos.
        for call in self.telegram.send_photo_album.call_args_list:
            self.assertNotIn("caption", call.kwargs)

        self.telegram.send_message.assert_not_called()

    def test_capture_directory_is_removed_after_sending(self) -> None:
        client = self._client_capturing(3)
        seen_dirs: list[Path] = []
        self.telegram.send_photo_album.side_effect = (
            lambda paths, **_kwargs: seen_dirs.append(paths[0].parent)
        )

        _deliver_body(client, self.telegram, self.watcher, _post())

        self.assertEqual(len(seen_dirs), 1)
        self.assertFalse(seen_dirs[0].exists())

    def test_unready_body_is_not_a_failure(self) -> None:
        client = Mock()
        client.capture_post_body_images.return_value = CapturedPostBody(
            image_paths=[], is_ready=False
        )

        result = _deliver_body(client, self.telegram, self.watcher, _post())

        self.assertFalse(result.sent)
        self.assertFalse(result.ready)
        self.telegram.send_photo_album.assert_not_called()

    def test_capture_error_is_a_failure(self) -> None:
        client = Mock()
        client.capture_post_body_images.side_effect = RuntimeError("browser died")

        result = _deliver_body(client, self.telegram, self.watcher, _post())

        self.assertFalse(result.sent)
        self.assertTrue(result.ready)

    def test_text_watcher_still_sends_messages(self) -> None:
        client = Mock()
        client.fetch_post_body.return_value = PostBody("본문", is_ready=True)

        result = _deliver_body(
            client, self.telegram, _image_watcher(send_body_as_image=False), _post()
        )

        self.assertTrue(result.sent)
        self.telegram.send_message.assert_called_once()
        self.telegram.send_photo_album.assert_not_called()
        client.capture_post_body_images.assert_not_called()

    def test_text_watcher_waits_for_an_unready_body(self) -> None:
        client = Mock()
        client.fetch_post_body.return_value = PostBody("스크립트 준비 중", is_ready=False)

        result = _deliver_body(
            client, self.telegram, _image_watcher(send_body_as_image=False), _post()
        )

        self.assertFalse(result.sent)
        self.assertFalse(result.ready)
        self.telegram.send_message.assert_not_called()

    def test_unready_post_is_retried_without_alerting_admin(self) -> None:
        post = _post()
        client = Mock()
        client.find_posts.return_value = [post]
        client.capture_post_body_images.return_value = CapturedPostBody(
            image_paths=[], is_ready=False
        )

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        self.assertFalse(self.store.has_seen(self.watcher.name, post.post_id))
        self.telegram.send_photo_album.assert_not_called()
        self.telegram.send_admin_message.assert_not_called()

    def test_sent_post_is_marked_seen(self) -> None:
        post = _post()
        client = self._client_capturing(3)

        _process_watcher(client, self.store, self.telegram, self.config, self.watcher)

        self.assertTrue(self.store.has_seen(self.watcher.name, post.post_id))
        self.telegram.send_photo_album.assert_called_once()


if __name__ == "__main__":
    unittest.main()

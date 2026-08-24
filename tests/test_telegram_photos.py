from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_us.config import TelegramConfig
from daily_us.telegram import MAX_ALBUM_ITEMS, TelegramClient


def _telegram_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token_env="TEST_BOT_TOKEN",
        chat_id_env="TEST_CHAT_ID",
        chat_ids_env="TEST_CHAT_IDS",
        admin_chat_id_env="TEST_ADMIN_CHAT_ID",
    )


class _FakeResponse:
    ok = True
    status_code = 200

    @staticmethod
    def json() -> dict[str, object]:
        return {"ok": True, "result": []}


class PhotoDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        env = {
            "TEST_BOT_TOKEN": "token",
            "TEST_CHAT_IDS": "111,222",
            "TEST_ADMIN_CHAT_ID": "999",
        }
        with patch.dict("os.environ", env, clear=False):
            self.client = TelegramClient(_telegram_config())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _photos(self, count: int) -> list[Path]:
        paths = []
        for index in range(count):
            path = self.root / f"{index:02d}.png"
            path.write_bytes(b"png-bytes")
            paths.append(path)
        return paths

    def test_album_rejects_empty_and_oversized_batches(self) -> None:
        with self.assertRaises(ValueError):
            self.client.send_photo_album([])
        with self.assertRaises(ValueError):
            self.client.send_photo_album(self._photos(MAX_ALBUM_ITEMS + 1))

    def test_single_photo_uses_send_photo(self) -> None:
        photos = self._photos(1)
        with patch("daily_us.telegram.requests.post", return_value=_FakeResponse()) as post:
            self.client.send_photo_album(photos, caption="본문", silent=True)

        self.assertEqual(post.call_count, 2)  # 수신자 두 명
        url, = post.call_args.args
        self.assertTrue(url.endswith("/sendPhoto"))
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["caption"], "본문")
        self.assertEqual(data["disable_notification"], "true")

    def test_album_captions_only_the_first_item(self) -> None:
        photos = self._photos(3)
        with patch("daily_us.telegram.requests.post", return_value=_FakeResponse()) as post:
            self.client.send_photo_album(photos, caption="첫 장 설명", admin_only=True)

        self.assertEqual(post.call_count, 1)  # admin_only 이므로 한 명
        url, = post.call_args.args
        self.assertTrue(url.endswith("/sendMediaGroup"))
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["chat_id"], "999")
        self.assertNotIn("disable_notification", data)

        media = json.loads(data["media"])
        self.assertEqual([item["media"] for item in media],
                         ["attach://photo0", "attach://photo1", "attach://photo2"])
        self.assertEqual(media[0]["caption"], "첫 장 설명")
        self.assertNotIn("caption", media[1])
        self.assertNotIn("caption", media[2])
        self.assertEqual(sorted(post.call_args.kwargs["files"]),
                         ["photo0", "photo1", "photo2"])

    def test_album_closes_file_handles(self) -> None:
        photos = self._photos(3)
        handles = []

        def capture(*_args, **kwargs):
            handles.extend(handle for _name, handle, _mime in kwargs["files"].values())
            return _FakeResponse()

        with patch("daily_us.telegram.requests.post", side_effect=capture):
            self.client.send_photo_album(photos, admin_only=True)

        self.assertEqual(len(handles), 3)
        self.assertTrue(all(handle.closed for handle in handles))

    def test_album_closes_file_handles_when_send_fails(self) -> None:
        photos = self._photos(3)
        handles = []

        def explode(*_args, **kwargs):
            handles.extend(handle for _name, handle, _mime in kwargs["files"].values())
            raise RuntimeError("boom")

        with patch("daily_us.telegram.requests.post", side_effect=explode):
            with patch.object(self.client, "send_admin_message"):
                with patch("daily_us.telegram.time.sleep"):
                    with self.assertRaises(Exception):
                        self.client.send_photo_album(photos, admin_only=True)

        self.assertTrue(handles)
        self.assertTrue(all(handle.closed for handle in handles))


if __name__ == "__main__":
    unittest.main()

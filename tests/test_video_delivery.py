from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import imageio_ffmpeg
from googleapiclient.errors import HttpError

from daily_us.config import DriveConfig, KST, _parse_watcher, load_config
from daily_us.drive import DriveClient, login_drive
from daily_us.poller import _next_poll_at, _process_latest_for_test, _process_watcher, poll_once, run_forever
from daily_us.site import PostRef, UsInsightClient
from daily_us.storage import SeenStore
from daily_us.video import PostVideo, VideoNotAvailableYet, download_video, video_from_payload


def _payload(**changes: object) -> dict:
    """실제 응답과 같은 키로 서명 정보 없는 테스트 데이터 생성."""
    return {"content": {
        "mediaType": "VIDEO", "hasAuthority": True, "isPreview": False,
        "title": "[정규수업 녹화본] 수업 영상", "publishedAt": "2026-09-22T11:00:31.390Z",
        "mediaConvertUrl": "https://video.us-insight.com/class.mp4.m3u8", "cookie": "",
        **changes,
    }}


class VideoMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        """화질 순서가 고정되지 않은 HLS 목록을 제공하고 외부 네트워크 차단."""
        request = patch("daily_us.video.requests.get")
        self.request = request.start()
        self.addCleanup(request.stop)
        self.playlist = self.request.return_value.__enter__.return_value
        self.playlist.status_code = 200
        self.playlist.text = (
            '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=854x480\n480.m3u8\n'
            '#EXT-X-STREAM-INF:RESOLUTION=1920x1080\n1080.m3u8\n'
            '#EXT-X-STREAM-INF:RESOLUTION=1280x720\n720.m3u8\n'
        )
        self.variant = SimpleNamespace(status_code=200, text='#EXTM3U\n#EXTINF:0.4,\nsegment.ts\n#EXT-X-ENDLIST')
        self.request.return_value.__enter__.side_effect = lambda: (
            self.variant if self.request.call_args.args[0].endswith('/1080.m3u8') else self.playlist
        )

    def test_download_selects_1080p_and_replaces_low_resolution_cache(self) -> None:
        """HLS 목록의 1080p를 선택하고 기존 480p 캐시를 실제 1080p 영상으로 교체."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.mkv"
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=size=1920x1080:rate=5",
                "-f", "lavfi", "-i", "sine=frequency=440",
                "-t", "0.4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source),
            ], check=True, capture_output=True, timeout=30)
            run = subprocess.run

            def use_local_source(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
                """입력 전송만 로컬 파일로 대체하고 실제 다운로드의 스트림 선택 옵션 실행."""
                command = command.copy()
                self.assertEqual(command[command.index("-i") + 1], "https://video.us-insight.com/1080.m3u8")
                command[command.index("-i") + 1] = str(source)
                command[command.index("-protocol_whitelist") + 1] += ",file"
                referer_index = command.index("-referer")
                del command[referer_index:referer_index + 2]
                return run(command, **kwargs)

            video = video_from_payload(_payload(), "https://us-insight.com/secrets/1")
            output = directory / "output"
            output.mkdir()
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
                "-vf", "scale=854:480", "-c:v", "libx264", "-preset", "ultrafast",
                str(output / video.filename),
            ], check=True, capture_output=True, timeout=30)
            with patch("daily_us.video.subprocess.run", side_effect=use_local_source):
                target = download_video(video, output)
            frames = imageio_ffmpeg.read_frames(str(target))
            try:
                metadata = next(frames)
                self.assertEqual(metadata["size"], (1920, 1080))
                self.assertEqual(metadata["audio_codec"], "aac")
            finally:
                frames.close()
            # 원본보다 짧은 1080p 파일은 완료 파일로 승격하지 않음
            self.variant.text = '#EXTM3U\n#EXTINF:30,\nsegment.ts\n#EXT-X-ENDLIST'
            with patch("daily_us.video.subprocess.run", side_effect=use_local_source):
                with self.assertRaisesRegex(RuntimeError, "전체 길이"):
                    download_video(video, directory / "incomplete")
            self.assertFalse((directory / "incomplete" / video.filename).exists())

    def test_missing_1080p_or_external_variant_does_not_start_download(self) -> None:
        """1080p 미제공·외부 CDN 주소·리다이렉트 응답에서 다운로드 중단."""
        video = video_from_payload(_payload(), "https://us-insight.com/secrets/1")
        for status, playlist, error in [
            (200, '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=854x480\n480.m3u8', VideoNotAvailableYet),
            (200, '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080\nhttps://example.com/video.m3u8', ValueError),
            (302, '', RuntimeError),
        ]:
            with self.subTest(status=status, playlist=playlist), tempfile.TemporaryDirectory() as temporary:
                self.playlist.status_code = status
                self.playlist.text = playlist
                with patch("daily_us.video.subprocess.run") as run, self.assertRaises(error):
                    download_video(video, Path(temporary))
                run.assert_not_called()

    def test_uses_published_date_instead_of_title_or_current_date(self) -> None:
        """게시일 하루 전 파일명과 연도·한국 시간 자정 경계 확인."""
        for published, filename in [
            ("2026-09-22T11:00:31Z", "2026-09-21.mp4"),
            ("2025-12-31T15:00:00Z", "2025-12-31.mp4"),
            ("2026-02-28T16:00:00Z", "2026-02-28.mp4"),
        ]:
            with self.subTest(published=published):
                video = video_from_payload(_payload(publishedAt=published), "https://us-insight.com/secrets/1")
                self.assertEqual(video.filename, filename)

    def test_skips_preview_and_text_posts(self) -> None:
        """제목 키워드가 같아도 미리보기와 안내 글은 다운로드하지 않음."""
        for changes in [{"isPreview": True}, {"title": "정규수업 미리보기"}, {"mediaType": "TEXT"}]:
            self.assertIsNone(video_from_payload(_payload(**changes), "https://us-insight.com/secrets/1"))

    def test_rejects_missing_authority_date_or_untrusted_media(self) -> None:
        """권한·게시일·미디어 호스트가 불명확한 경우 삭제 경로 진입 차단."""
        for changes in [
            {"hasAuthority": False}, {"publishedAt": "2026-09-22T20:00:00"},
            {"publishedAt": ""}, {"mediaConvertUrl": "https://example.org/class.m3u8"},
            {"mediaConvertUrl": "file:///tmp/video.mp4"},
        ]:
            with self.subTest(changes=changes), self.assertRaises((ValueError, RuntimeError)):
                video_from_payload(_payload(**changes), "https://us-insight.com/secrets/1")
        with self.assertRaises(VideoNotAvailableYet):
            video_from_payload(_payload(mediaConvertUrl=None), "https://us-insight.com/secrets/1")

    @patch("daily_us.video._is_1080p", return_value=True)
    def test_download_completion_cache_and_failure_cleanup(self, _quality: Mock) -> None:
        """완료 파일만 재사용하고 ffmpeg 실패 시 부분 파일 제거."""
        video = video_from_payload(_payload(), "https://us-insight.com/secrets/1")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def write_video(command: list[str], **_kwargs: object) -> SimpleNamespace:
                """ffmpeg의 임시 MP4 출력을 재현."""
                Path(command[-1]).write_bytes(b"video")
                return SimpleNamespace(returncode=0)

            with patch("daily_us.video.subprocess.run", side_effect=write_video) as run:
                path = download_video(video, directory)
                self.assertEqual(path.name, "2026-09-21.mp4")
                self.assertEqual(path.read_bytes(), b"video")
                download_video(video, directory)
                run.assert_called_once()
            path.unlink()
            partial = directory / "2026-09-21.part.mp4"
            partial.write_bytes(b"incomplete")
            with patch("daily_us.video.subprocess.run", return_value=SimpleNamespace(returncode=1)):
                with self.assertRaises(RuntimeError):
                    download_video(video, directory)
            self.assertFalse(partial.exists())
            self.assertFalse(path.exists())

    def test_timeout_does_not_expose_signed_command(self) -> None:
        """다운로드 시간 초과 메시지에서 명령 인수와 서명 쿠키 노출 방지."""
        video = video_from_payload(_payload(), "https://us-insight.com/secrets/1")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("daily_us.video.subprocess.run", side_effect=subprocess.TimeoutExpired("secret-cookie", 1)):
                with self.assertRaises(RuntimeError) as caught:
                    download_video(video, Path(temporary))
        self.assertNotIn("secret-cookie", str(caught.exception))


class VideoResponseTest(unittest.TestCase):
    def test_unreadable_response_retries_and_later_valid_response_recovers(self) -> None:
        """본문 읽기 실패·비객체 JSON은 재조회하고 뒤이은 정상 응답은 처리."""
        post = PostRef("1", "정규수업", "https://us-insight.com/secrets/1")
        for payload in [ValueError("invalid JSON"), RuntimeError("body unavailable"), []]:
            for recover in [False, True]:
                with self.subTest(payload=payload, recover=recover):
                    client = Mock(spec=UsInsightClient)
                    page = client._new_page.return_value
                    client._is_logged_out.return_value = False
                    response = Mock(url="https://api.us-insight.com/v2/contents/secret/1", ok=True)
                    if isinstance(payload, Exception):
                        response.json.side_effect = payload
                    else:
                        response.json.return_value = payload

                    def emit_responses(*_args: object) -> None:
                        """페이지 이동 중 실패 응답과 선택적인 정상 응답 전달."""
                        callback = page.on.call_args.args[1]
                        callback(response)
                        if recover:
                            response.json.side_effect = None
                            response.json.return_value = _payload()
                            callback(response)

                    client._goto.side_effect = emit_responses
                    if recover:
                        self.assertIsInstance(UsInsightClient.fetch_post_video(client, post), PostVideo)
                    else:
                        with self.assertRaises(VideoNotAvailableYet):
                            UsInsightClient.fetch_post_video(client, post)
                    page.close.assert_called_once()

    def test_existing_commands_import_without_google_dependencies(self) -> None:
        """별도 프로세스에서 Google 패키지를 차단해 CLI와 폴러의 의존성 분리 확인."""
        result = subprocess.run([
            sys.executable, "-c",
            "import sys; sys.modules.update({name: None for name in "
            "('google.auth', 'google.oauth2', 'google_auth_oauthlib', 'googleapiclient')}); "
            "import daily_us.__main__; import daily_us.poller; "
            "assert 'daily_us.drive' not in sys.modules",
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


class VideoScheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        """실제 운영 설정의 정규수업 워처 로드."""
        config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        self.watcher = next(watcher for watcher in config.watchers if watcher.name == "regular_class")

    def test_tuesday_window_includes_all_24_slots_and_last_minute(self) -> None:
        """24회 예약과 마지막 실행의 지연을 허용하고 22:05부터 제외."""
        first = datetime(2026, 9, 22, 20, 5, tzinfo=KST)
        slots = [first + timedelta(minutes=5 * index) for index in range(24)]
        self.assertEqual(slots[-1].strftime("%H:%M"), "22:00")
        self.assertTrue(all(self.watcher.is_active_at(slot) for slot in slots))
        self.assertTrue(self.watcher.is_active_at(slots[-1].replace(second=59)))
        self.assertTrue(self.watcher.is_active_at(first.astimezone(timezone.utc)))
        self.assertTrue(self.watcher.is_active_at(slots[-1] + timedelta(minutes=1, seconds=10)))
        self.assertTrue(self.watcher.is_active_at(slots[-1] + timedelta(minutes=4, seconds=59)))
        for outside in [first - timedelta(seconds=1), slots[-1] + timedelta(minutes=5), first + timedelta(days=1)]:
            self.assertFalse(self.watcher.is_active_at(outside))

    def test_polling_does_not_drift_after_processing_time(self) -> None:
        """완료 시각이 20:06:37이어도 다음 조회는 20:10에 예약."""
        now = datetime(2026, 9, 22, 20, 6, 37, tzinfo=KST)
        self.assertEqual(_next_poll_at(self.watcher, now), now.replace(minute=10, second=0))
        self.assertEqual(_next_poll_at(self.watcher, now.replace(hour=21, minute=59)), now.replace(hour=22, minute=0, second=0))

    def test_delivery_modes_and_interval_validation(self) -> None:
        """동영상과 다른 전송 방식 혼합 및 0분 간격 거부."""
        with self.assertRaises(ValueError):
            _parse_watcher({"name": "bad", "send_video_to_drive": True})
        with self.assertRaises(ValueError):
            _parse_watcher({"name": "bad", "interval_minutes": 0})

    def test_selected_existing_watchers_run_after_window_ends(self) -> None:
        """대기 중 창이 끝나도 기존 워처는 실행하고 동영상만 생략. 수동 조회는 모두 실행."""
        base = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        for continuous, ignore_schedule in [(True, False), (False, False), (False, True)]:
            for video in [False, True]:
                with self.subTest(continuous=continuous, ignore_schedule=ignore_schedule, video=video):
                    watcher = Mock(name="watcher", send_video_to_drive=video)
                    watcher.interval_minutes = 5
                    watcher.is_active_at.side_effect = [True, False]
                    config = replace(base, watchers=[watcher])
                    with patch("daily_us.poller.SeenStore"), patch("daily_us.poller.TelegramClient"), \
                            patch("daily_us.poller.UsInsightClient"), \
                            patch("daily_us.poller._process_watcher") as process, \
                            patch("daily_us.poller._next_poll_at"), \
                            patch("daily_us.poller.time_module.sleep", side_effect=KeyboardInterrupt):
                        if continuous:
                            with self.assertRaises(KeyboardInterrupt):
                                run_forever(config)
                        else:
                            poll_once(config, ignore_schedule=ignore_schedule)
                    self.assertEqual(process.call_count, int(not video or ignore_schedule))


class VideoDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        """외부 전송 없이 실제 SQLite 이력을 사용하는 전달 테스트 구성."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        base = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        self.config = replace(base, storage=replace(base.storage, database_path=root / "seen.db", download_dir=root / "downloads"))
        self.watcher = next(watcher for watcher in base.watchers if watcher.send_video_to_drive)
        self.store = SeenStore(self.config.storage.database_path)
        self.post = PostRef("post-1", "[정규수업 녹화본] 이번 주 수업", "https://us-insight.com/secrets/1")
        self.video = PostVideo(datetime.now(KST), "https://video.us-insight.com/test.m3u8", "", self.post.url)
        self.client = Mock()
        self.client.find_posts.return_value = [self.post]
        self.client.fetch_post_video.return_value = self.video
        self.telegram = Mock()
        self.drive = Mock(spec=DriveClient)
        self.drive.get_video.return_value = None
        self.drive.find_videos.return_value = []
        self.drive.generate_id.return_value = "file-1"
        self.remote = {"id": "file-1", "size": "14", "webViewLink": "https://drive.google.com/file/d/file-1/view"}
        self.drive.upload_video.return_value = self.remote
        drive_patch = patch("daily_us.drive.DriveClient")
        drive_patch.start().return_value.__enter__.return_value = self.drive
        self.addCleanup(drive_patch.stop)
        alert_patch = patch("daily_us.poller._notify_poll_failure_with_cooldown")
        self.alert = alert_patch.start()
        self.addCleanup(alert_patch.stop)
        download_patch = patch("daily_us.poller.download_video", side_effect=self._download)
        self.download = download_patch.start()
        self.addCleanup(download_patch.stop)

    def _download(self, video: PostVideo, directory: Path) -> Path:
        """완료된 로컬 파일을 생성하고 삭제 전에 ID가 저장되었는지 확인."""
        self.assertEqual(self.store.get_video_file_id(self.watcher.name, self.post.post_id), "file-1")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / video.filename
        path.write_bytes(b"complete-video")
        return path

    def _poll(self) -> None:
        """일반 운영과 같은 워처 처리 경로 실행."""
        _process_watcher(self.client, self.store, self.telegram, self.config, self.watcher)

    def test_download_delete_upload_notify_in_order_and_skip_seen(self) -> None:
        """다운로드 성공 뒤에만 삭제·업로드하고 링크 전달 후 이력 기록."""
        calls = Mock()
        for name, child in [("download", self.download), ("delete", self.drive.delete_previous_week),
                            ("upload", self.drive.upload_video), ("notify", self.telegram.send_message)]:
            calls.attach_mock(child, name)
        self._poll()
        self.assertEqual([call[0] for call in calls.mock_calls], ["download", "delete", "upload", "notify"])
        self.drive.delete_previous_week.assert_called_once_with(self.video.filename)
        self.assertTrue(self.store.has_seen(self.watcher.name, self.post.post_id))
        self.assertFalse(list(self.config.storage.download_dir.rglob("*.mp4")))
        self._poll()
        self.telegram.send_message.assert_called_once()

    def test_download_failure_preserves_previous_week(self) -> None:
        """새 영상 다운로드 실패 시 Drive 파일 삭제·업로드·완료 기록 생략."""
        self.download.side_effect = RuntimeError("download failed")
        self._poll()
        self.drive.delete_previous_week.assert_not_called()
        self.drive.upload_video.assert_not_called()
        self.telegram.send_message.assert_not_called()
        self.assertFalse(self.store.has_seen(self.watcher.name, self.post.post_id))

    def test_delete_failure_prevents_upload(self) -> None:
        """전 주 파일 삭제에 실패하면 업로드를 진행하지 않고 다운로드 보존."""
        self.drive.delete_previous_week.side_effect = RuntimeError("delete denied")
        self._poll()
        self.drive.upload_video.assert_not_called()
        self.assertEqual(len(list(self.config.storage.download_dir.rglob("*.mp4"))), 1)

    def test_upload_failure_retains_file_and_fixed_id_across_restart(self) -> None:
        """업로드 실패 뒤 프로세스가 재시작해도 동일한 사전 발급 ID 사용."""
        self.drive.upload_video.side_effect = RuntimeError("connection lost")
        self._poll()
        self.assertEqual(len(list(self.config.storage.download_dir.rglob("*.mp4"))), 1)
        self.store = SeenStore(self.config.storage.database_path)
        self.drive.upload_video.side_effect = None
        self._poll()
        self.drive.generate_id.assert_called_once()
        self.assertEqual([call.args[1] for call in self.drive.upload_video.call_args_list], ["file-1", "file-1"])
        self.assertTrue(self.store.has_seen(self.watcher.name, self.post.post_id))

    def test_lost_upload_response_reuses_completed_remote_file(self) -> None:
        """서버 업로드 완료 뒤 응답을 잃은 경우 재전송·삭제 없이 링크 전송."""
        self.drive.upload_video.side_effect = RuntimeError("response lost")
        self._poll()
        self.drive.get_video.return_value = self.remote
        self._poll()
        self.download.assert_called_once()
        self.drive.upload_video.assert_called_once()
        self.drive.delete_previous_week.assert_called_once()
        self.telegram.send_message.assert_called_once()

    def test_bot_failure_retries_only_link(self) -> None:
        """봇 실패 시 업로드 파일을 보존하고 다음 폴링에서 링크만 재시도."""
        self.telegram.send_message.side_effect = RuntimeError("telegram unavailable")
        self._poll()
        self.assertFalse(self.store.has_seen(self.watcher.name, self.post.post_id))
        self.drive.get_video.return_value = self.remote
        self.telegram.send_message.side_effect = None
        self._poll()
        self.download.assert_called_once()
        self.drive.upload_video.assert_called_once()
        self.drive.delete_previous_week.assert_called_once()
        self.assertTrue(self.store.has_seen(self.watcher.name, self.post.post_id))

    def test_remote_size_mismatch_preserves_local_copy(self) -> None:
        """완료 응답 유실 뒤 원격 파일 크기가 다르면 로컬 파일과 미전송 상태 보존."""
        self.drive.upload_video.side_effect = RuntimeError("response lost")
        self._poll()
        self.drive.get_video.return_value = {**self.remote, "size": "1"}
        self._poll()
        self.telegram.send_message.assert_not_called()
        self.assertEqual(len(list(self.config.storage.download_dir.rglob("*.mp4"))), 1)
        self.assertFalse(self.store.has_seen(self.watcher.name, self.post.post_id))

    def test_manual_existing_file_is_reused(self) -> None:
        """같은 날짜의 수동 업로드 영상이 있으면 중복 생성·전 주 삭제 생략."""
        self.drive.find_videos.return_value = [self.remote]
        self.drive.get_video.return_value = self.remote
        self._poll()
        self.download.assert_not_called()
        self.drive.delete_previous_week.assert_not_called()
        self.drive.upload_video.assert_not_called()
        self.telegram.send_message.assert_called_once()

    def test_not_ready_old_posts_and_preview_never_delete(self) -> None:
        """변환 중·과거 게시글·미리보기·안내 글은 삭제 경로 진입 차단."""
        self.client.fetch_post_video.side_effect = VideoNotAvailableYet("not ready")
        self._poll()
        self.alert.assert_not_called()
        self.client.fetch_post_video.side_effect = None
        self.client.fetch_post_video.return_value = replace(self.video, published_at=self.video.published_at - timedelta(days=7))
        self._poll()
        self.client.fetch_post_video.return_value = None
        self._poll()
        self.client.find_posts.return_value = [replace(self.post, title="정규수업 미리보기")]
        self.client.fetch_post_video.reset_mock()
        self._poll()
        self.client.fetch_post_video.assert_not_called()
        self.drive.delete_previous_week.assert_not_called()
        self.download.assert_not_called()

    def test_test_latest_cannot_delete_past_videos(self) -> None:
        """이력을 무시하는 테스트 명령에서 원격 파일 변경 방지."""
        _process_latest_for_test(self.client, self.telegram, self.config, self.watcher, 1)
        self.client.find_posts.assert_not_called()
        self.drive.delete_previous_week.assert_not_called()


class DriveVideoTest(unittest.TestCase):
    def test_transport_preserves_resumable_upload_response(self) -> None:
        """308 업로드 진행 응답을 리다이렉트하지 않고 SDK에 전달하도록 구성."""
        with tempfile.TemporaryDirectory() as temporary:
            token = Path(temporary) / "token.json"
            token.write_text("{}", encoding="utf-8")
            config = DriveConfig("folder-1", Path("client.json"), token)
            with patch("daily_us.drive.Credentials.from_authorized_user_file") as credentials, patch("daily_us.drive.build") as build:
                credentials.return_value.valid = True
                with DriveClient(config):
                    http = build.call_args.kwargs["http"].http
                    self.assertNotIn(308, http.redirect_codes)
                    self.assertIn(302, http.redirect_codes)
                    self.assertEqual(http.timeout, 120)

    def setUp(self) -> None:
        """인증·네트워크 없이 실제 Drive 요청 구성 검증."""
        self.drive = DriveClient.__new__(DriveClient)
        self.drive.config = DriveConfig("folder-1", Path("client.json"), Path("token.json"))
        self.drive.service = Mock()
        self.files = self.drive.service.files.return_value

    def test_web_credentials_are_rejected_before_opening_browser(self) -> None:
        """웹 앱용 인증 파일이면 브라우저를 열기 전에 데스크톱 앱 발급 안내."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "client.json"
            path.write_text("{}", encoding="utf-8")
            config = DriveConfig("folder-1", path, Path(temporary) / "token.json")
            with patch("daily_us.drive.InstalledAppFlow.from_client_secrets_file") as factory:
                factory.return_value.client_type = "web"
                with self.assertRaisesRegex(RuntimeError, "데스크톱 앱"):
                    login_drive(config)
                factory.return_value.run_local_server.assert_not_called()

    def test_only_previous_week_in_target_folder_is_deleted(self) -> None:
        """연도 경계에서도 정확히 7일 전 MP4를 조회하고 해당 ID만 삭제."""
        old = {"id": "old", "name": "2025-12-29.mp4", "mimeType": "video/mp4", "parents": ["folder-1"], "size": "42"}
        self.files.list.return_value.execute.return_value = {"files": [old]}
        self.files.get.return_value.execute.return_value = old
        self.drive.delete_previous_week("2026-01-05.mp4")
        query = self.files.list.call_args.kwargs["q"]
        self.assertEqual(query, "'folder-1' in parents and name = '2025-12-29.mp4' and mimeType = 'video/mp4' and trashed = false")
        self.files.delete.assert_called_once_with(fileId="old", supportsAllDrives=True)

    def test_moved_file_and_invalid_name_cannot_be_deleted(self) -> None:
        """조회 후 다른 폴더로 옮긴 파일과 잘못된 날짜는 삭제 거부."""
        self.files.list.return_value.execute.return_value = {"files": [{"id": "old"}]}
        self.files.get.return_value.execute.return_value = {"id": "old", "name": "2026-09-14.mp4", "mimeType": "video/mp4", "parents": ["another-folder"], "size": "42"}
        with self.assertRaises(RuntimeError):
            self.drive.delete_previous_week("2026-09-21.mp4")
        with self.assertRaises(ValueError):
            self.drive.delete_previous_week("2026-9-21.mp4")
        self.files.delete.assert_not_called()

    def test_pagination_and_missing_previous_week(self) -> None:
        """조회 페이지를 모두 확인하고 전 주 파일이 없으면 삭제 생략."""
        self.files.list.return_value.execute.side_effect = [
            {"files": [], "nextPageToken": "page-2"}, {"files": []},
        ]
        self.drive.delete_previous_week("2026-09-21.mp4")
        self.assertEqual(self.files.list.call_args.kwargs["pageToken"], "page-2")
        self.files.delete.assert_not_called()

    def test_only_not_found_allows_upload(self) -> None:
        """업로드 전 ID 확인에서 404만 미완료로 취급하고 권한 오류는 전파."""
        for status in [404, 403]:
            self.files.get.return_value.execute.side_effect = HttpError(SimpleNamespace(status=status, reason="error"), b"{}")
            if status == 404:
                self.assertIsNone(self.drive.get_video("new", "2026-09-21.mp4"))
            else:
                with self.assertRaises(HttpError):
                    self.drive.get_video("new", "2026-09-21.mp4")

    def test_chunk_upload_uses_fixed_id_and_checks_size(self) -> None:
        """완료 응답까지 청크를 전송하고 파일 ID·대상 폴더·파일 크기 검증."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "2026-09-21.mp4"
            path.write_bytes(b"video")
            request = self.files.create.return_value
            request.next_chunk.side_effect = [(None, None), (None, {"id": "fixed", "size": "5"})]
            self.drive.upload_video(path, "fixed")
            self.assertEqual(request.next_chunk.call_count, 2)
            self.assertEqual(self.files.create.call_args.kwargs["body"], {"id": "fixed", "name": path.name, "parents": ["folder-1"]})
            request.next_chunk.side_effect = [(None, {"id": "fixed", "size": "2"})]
            with self.assertRaises(RuntimeError):
                self.drive.upload_video(path, "fixed")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import tempfile
import unittest
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import Mock, call, patch

from daily_us.config import SiteConfig
from daily_us.site import AudioNotAvailableYet, LoginRequired, PlaywrightError, PostRef, UsInsightClient, _lock_auth_state


def _read_locked_state(path: Path, connection: Connection) -> None:
    """별도 프로세스에서 잠금을 얻은 뒤 인증 파일을 읽어 부모에게 전달.

    Args:
        path: 인증 상태 파일 경로.
        connection: 준비 여부와 읽은 값을 전달할 파이프.
    """
    with connection:
        connection.send("ready")
        with _lock_auth_state(path):
            connection.send(json.loads(path.read_text(encoding="utf-8")))


class AuthSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        """실제 인증 파일과 분리된 테스트 저장소 준비."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = SiteConfig(
            feed_url="https://us-insight.com/feed?type=all",
            profile_dir=self.root / "profile",
            auth_state_path=self.root / "auth_state.json",
            session_storage_path=self.root / "session_storage.json",
            headless=True,
            navigation_timeout_ms=1000,
        )
        self.client = UsInsightClient(self.config)

    def test_waiting_process_reads_state_saved_by_previous_process(self) -> None:
        """다음 작업은 앞 작업의 저장이 끝난 뒤 최신 토큰으로 시작하는지 확인."""
        self.config.auth_state_path.write_text('{"token": "old"}', encoding="utf-8")
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_read_locked_state, args=(self.config.auth_state_path, sender))
        try:
            with _lock_auth_state(self.config.auth_state_path):
                process.start()
                sender.close()
                self.assertTrue(receiver.poll(10), "Child did not start")
                self.assertEqual(receiver.recv(), "ready")
                self.assertFalse(receiver.poll(0.2), "Child read state while another session held the lock")
                self.config.auth_state_path.write_text('{"token": "new"}', encoding="utf-8")
            self.assertTrue(receiver.poll(10), "Child did not acquire the released lock")
            self.assertEqual(receiver.recv(), {"token": "new"})
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(10)
            receiver.close()
            sender.close()

    def test_browser_start_failure_releases_session_lock(self) -> None:
        """브라우저 시작 실패 후에도 다른 작업이 세션을 사용할 수 있는지 확인."""
        playwright = Mock()
        playwright.chromium.launch.side_effect = RuntimeError("launch failed")
        with patch("daily_us.site.sync_playwright") as start, patch("daily_us.site._lock_auth_state") as lock:
            start.return_value.start.return_value = playwright
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                self.client.__enter__()
            lock.return_value.__exit__.assert_called_once()
            playwright.stop.assert_called_once()

    def test_windows_lock_retries_contention_on_the_first_byte(self) -> None:
        """Windows 잠금 경합을 재시도하고 고정 파일의 첫 바이트를 잠그는지 모의 검증."""
        lock_path = self.config.auth_state_path.with_name(f"{self.config.auth_state_path.name}.lock")
        for initial in (b"", b"existing-lock"):
            with self.subTest(initial=initial):
                lock_path.write_bytes(initial)
                # 한계: msvcrt 호출을 모의하므로 실제 OS의 상호 배제는 Windows에서 프로세스 간 잠금 테스트로 검증 필요
                windows_os = Mock(wraps=os)
                windows_os.name = "nt"
                msvcrt = Mock(LK_NBLCK=1)
                msvcrt.locking.side_effect = [
                    OSError(errno.EACCES, "locked"),
                    OSError(errno.EAGAIN, "locked"),
                    OSError(errno.EDEADLK, "locked"),
                    None,
                ]
                with patch("daily_us.site.os", windows_os), \
                        patch.dict("sys.modules", {"msvcrt": msvcrt}), \
                        patch("daily_us.site.time.sleep") as sleep:
                    with _lock_auth_state(self.config.auth_state_path):
                        fd = msvcrt.locking.call_args.args[0]
                        self.assertEqual(msvcrt.locking.call_args_list, [call(fd, msvcrt.LK_NBLCK, 1)] * 4)
                        self.assertEqual(sleep.call_args_list, [call(0.1)] * 3)
                        self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
                        self.assertEqual(os.fstat(fd).st_size, len(initial or b"\0"))
                    # 잠금 구간 종료 시 파일 핸들 정리 확인
                    with self.assertRaises(OSError) as caught:
                        os.fstat(fd)
                    self.assertEqual(caught.exception.errno, errno.EBADF)
                self.assertEqual(lock_path.read_bytes(), initial or b"\0")

    def test_windows_lock_does_not_retry_unrelated_errors(self) -> None:
        """Windows 잠금에서 경합 외 오류는 재시도하지 않고 전달하는지 모의 검증."""
        windows_os = Mock(wraps=os)
        windows_os.name = "nt"
        msvcrt = Mock(LK_NBLCK=1)
        error = OSError(errno.EINVAL, "invalid lock")
        msvcrt.locking.side_effect = error
        with patch("daily_us.site.os", windows_os), \
                patch.dict("sys.modules", {"msvcrt": msvcrt}), \
                patch("daily_us.site.time.sleep") as sleep:
            with self.assertRaises(OSError) as caught:
                with _lock_auth_state(self.config.auth_state_path):
                    self.fail("Lock failure must not enter the protected section")
            self.assertIs(caught.exception, error)
            msvcrt.locking.assert_called_once()
            sleep.assert_not_called()
            with self.assertRaises(OSError) as closed:
                os.fstat(msvcrt.locking.call_args.args[0])
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_mobile_session_is_saved_and_desktop_reloads_it(self) -> None:
        """모바일 토큰을 저장하고 다음 데스크톱 페이지가 이를 복원하는지 확인."""
        desktop, mobile = Mock(), Mock()
        page = Mock(context=mobile)
        page.evaluate.side_effect = ["https://us-insight.com", {"session": "latest"}]
        state = {"cookies": [], "origins": [{"origin": "https://us-insight.com", "localStorage": []}]}
        mobile.storage_state.return_value = state
        self.client.context = desktop
        self.client.body_capture_context = mobile
        self.client.browser = Mock()

        self.client._save_auth_state(page)

        self.assertEqual(json.loads(self.config.auth_state_path.read_text()), state)
        self.assertEqual(json.loads(self.config.session_storage_path.read_text()),
                         {"https://us-insight.com": {"session": "latest"}})
        desktop.close.assert_called_once()
        mobile.close.assert_not_called()
        self.assertIsNone(self.client.context)
        self.client._new_page()
        self.assertEqual(self.client.browser.new_context.call_args.kwargs["storage_state"], state)

    def test_desktop_save_invalidates_older_mobile_context(self) -> None:
        """데스크톱에서 저장한 뒤에는 이전 모바일 토큰을 재사용하지 않는지 확인."""
        desktop, mobile = Mock(), Mock()
        desktop.storage_state.return_value = {"cookies": [], "origins": []}
        page = Mock(context=desktop)
        page.evaluate.side_effect = ["https://us-insight.com", {}]
        self.client.context = desktop
        self.client.body_capture_context = mobile

        self.client._save_auth_state(page)

        mobile.close.assert_called_once()
        desktop.close.assert_not_called()
        self.assertIsNone(self.client.body_capture_context)

    def test_login_check_saves_only_verified_session(self) -> None:
        """로그인 확인 중 갱신된 토큰은 저장하고 실패 상태는 저장하지 않는지 확인."""
        page = Mock(url=self.config.feed_url)
        with patch.object(self.client, "_new_page", return_value=page), \
                patch.object(self.client, "_goto"), \
                patch.object(self.client, "_wait_for_network_idle"), \
                patch.object(self.client, "_wait_for_page_settle"), \
                patch.object(self.client, "_is_logged_out", side_effect=[False, True]), \
                patch.object(self.client, "_save_auth_state") as save:
            self.assertTrue(self.client._verify_feed_access()[0])
            save.assert_called_once_with(page)
            self.assertFalse(self.client._verify_feed_access()[0])
            save.assert_called_once_with(page)

    def test_logged_out_or_external_page_does_not_replace_saved_state(self) -> None:
        """로그아웃 화면과 외부 로그인 페이지는 저장 대상에서 제외하는지 확인."""
        with patch.object(self.client, "_save_auth_state") as save:
            page = Mock(url="https://us-insight.com/signin")
            self.client._refresh_saved_session(page)
            page.url = "https://nid.naver.com/login"
            self.client._refresh_saved_session(page)
            save.assert_not_called()

    def test_login_waits_for_authentication_and_feed_without_terminal_input(self) -> None:
        """인증 성공과 피드 진입이 모두 확인돼야 입력 없이 세션을 저장하는지 확인."""
        page = Mock(url=self.config.feed_url)
        page.is_closed.return_value = False
        page.context.pages = [page]
        responses = iter([
            (self.config.feed_url, "https://example.test/v3/auth/me", 200),
            (self.config.feed_url, "https://api.us-insight.com/v3/auth/me", 401),
            ("https://us-insight.com/signin", "https://api.us-insight.com/v3/auth/me", 200),
            (self.config.feed_url, "https://api.us-insight.com/v3/auth/me", 403),
            (self.config.feed_url, "https://api.us-insight.com/v3/auth/me", 200),
        ])

        def advance_login(_timeout: int) -> None:
            """브라우저 대기마다 로그인 진행 단계와 인증 응답 전달.

            Args:
                _timeout: 브라우저 이벤트 처리 대기 시간.
            """
            save.assert_not_called()
            page.url, response_url, status = next(responses)
            page.context.on.call_args.args[1](Mock(url=response_url, status=status))

        page.wait_for_timeout.side_effect = advance_login
        with patch.object(self.client, "_new_page", return_value=page), \
                patch.object(self.client, "_goto"), \
                patch.object(self.client, "_is_logged_out", return_value=False), \
                patch.object(self.client, "_save_auth_state") as save, \
                patch.object(self.client, "_log_session_expiry"), \
                patch("builtins.input", side_effect=AssertionError("Unexpected terminal input")), \
                patch("builtins.print") as output:
            self.client.open_login_page()
            save.assert_called_once_with(page)
            self.assertEqual(page.wait_for_timeout.call_count, 5)
            self.assertIn(call("로그인 세션이 저장되었습니다."), output.call_args_list)
            page.context.remove_listener.assert_called_once_with("response", page.context.on.call_args.args[1])

    def test_login_popup_save_failure_is_not_reported_as_success(self) -> None:
        """인증된 팝업 페이지의 저장에 실패하면 성공 안내 없이 오류를 전달하는지 확인."""
        page = Mock(url="https://us-insight.com/signin")
        page.is_closed.return_value = False
        popup = Mock(url=self.config.feed_url)
        page.context.pages = [page, popup]

        def authenticate(_page: Mock, _url: str) -> None:
            """페이지를 여는 동안 수신한 인증 성공 응답 전달.

            Args:
                _page: 로그인 페이지.
                _url: 처음 접근한 피드 주소.
            """
            page.context.on.call_args.args[1](Mock(url="https://api.us-insight.com/v3/auth/me", status=200))

        with patch.object(self.client, "_new_page", return_value=page), \
                patch.object(self.client, "_goto", side_effect=authenticate), \
                patch.object(self.client, "_is_logged_out", return_value=False), \
                patch.object(self.client, "_save_auth_state") as save, \
                patch.object(self.client, "_log_session_expiry"), \
                patch("builtins.input", side_effect=AssertionError("Unexpected terminal input")), \
                patch("builtins.print") as output:
            # 자동 저장이 실패하면 성공 안내 없이 오류 전달
            save.side_effect = OSError("disk full")
            with self.assertRaisesRegex(OSError, "disk full"):
                self.client.open_login_page()
            save.assert_called_once_with(popup)
            self.assertNotIn(call("로그인 세션이 저장되었습니다."), output.call_args_list)

    def test_login_rechecks_authentication_after_reading_body(self) -> None:
        """본문 조회 도중 인증 실패 응답이 도착하면 세션 저장과 성공 안내를 막는지 확인."""
        for status in (401, 403):
            with self.subTest(status=status):
                page = Mock(url=self.config.feed_url)
                page.is_closed.return_value = False
                page.context.pages = [page]
                # 다음 대기에서 사용자가 취소한 것으로 처리해 저장하지 않은 로그인 루프 종료
                page.wait_for_timeout.side_effect = KeyboardInterrupt

                def authenticate(_page: Mock, _url: str) -> None:
                    """피드 접근 중 인증 성공 응답 전달.

                    Args:
                        _page: 접근 중인 페이지.
                        _url: 피드 주소.
                    """
                    page.context.on.call_args.args[1](Mock(url="https://api.us-insight.com/v3/auth/me", status=200))

                def read_body(**_options: object) -> str:
                    """본문 조회 중 인증 실패 응답을 전달하고 기존 피드 본문 반환.

                    Args:
                        _options: 본문 조회의 대기 시간 옵션.

                    Returns:
                        로그인 화면 문구가 없는 피드 본문.
                    """
                    page.context.on.call_args.args[1](Mock(url="https://api.us-insight.com/v3/auth/me", status=status))
                    return "게시글 피드"

                page.locator.return_value.inner_text.side_effect = read_body
                with patch.object(self.client, "_new_page", return_value=page), \
                        patch.object(self.client, "_goto", side_effect=authenticate), \
                        patch.object(self.client, "_save_auth_state") as save, \
                        patch.object(self.client, "_log_session_expiry"), \
                        patch("builtins.print") as output:
                    with self.assertRaises(KeyboardInterrupt):
                        self.client.open_login_page()
                    page.locator.return_value.inner_text.assert_called_once()
                    save.assert_not_called()
                    self.assertNotIn(call("로그인 세션이 저장되었습니다."), output.call_args_list)

    def test_closed_login_tab_explains_how_to_retry(self) -> None:
        """로그인 완료 전에 창을 닫으면 저장 성공 대신 재실행 안내를 반환하는지 확인."""
        for login_tab_closed in (False, True):
            with self.subTest(login_tab_closed=login_tab_closed):
                page = Mock()
                page.is_closed.return_value = login_tab_closed
                self.client.browser = Mock()
                self.client.browser.is_connected.return_value = True
                page.wait_for_timeout.side_effect = PlaywrightError("Target page, context or browser has been closed")
                with patch.object(self.client, "_new_page", return_value=page), \
                        patch.object(self.client, "_goto"), \
                        patch("builtins.input", side_effect=AssertionError("Unexpected terminal input")), \
                        patch("builtins.print") as output:
                    with self.assertRaisesRegex(LoginRequired, "자동 저장"):
                        self.client.open_login_page()
                    self.assertNotIn(call("로그인 세션이 저장되었습니다."), output.call_args_list)
                self.assertEqual(page.close.call_count, 0 if login_tab_closed else 1)

    def test_interactive_login_preserves_unrelated_browser_errors(self) -> None:
        """네트워크 등 다른 브라우저 오류를 창 종료 안내로 바꾸지 않는지 확인."""
        page = Mock()
        page.is_closed.return_value = False
        self.client.browser = Mock()
        self.client.browser.is_connected.return_value = True
        error = PlaywrightError("net::ERR_CONNECTION_RESET")
        with patch.object(self.client, "_new_page", return_value=page), \
                patch.object(self.client, "_goto", side_effect=error), \
                patch("builtins.print"):
            with self.assertRaises(PlaywrightError) as caught:
                self.client.open_login_page()
            self.assertIs(caught.exception, error)

    def test_login_cli_reports_interruption_without_traceback(self) -> None:
        """로그인 중단 안내를 출력할 실패 종료 코드를 CLI가 전달하는지 확인."""
        from daily_us.__main__ import main

        with patch("sys.argv", ["daily_us", "login"]), \
                patch("daily_us.__main__.logging.basicConfig"), \
                patch("daily_us.__main__.load_dotenv"), \
                patch("daily_us.__main__.load_config", return_value=Mock(site=self.config)), \
                patch("daily_us.__main__.UsInsightClient") as client:
            client.return_value.__enter__.return_value.open_login_page.side_effect = LoginRequired("다시 로그인하세요.")
            with self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, "다시 로그인하세요.")
            self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_audio_still_saves_refreshed_session(self) -> None:
        """아직 오디오가 없는 게시글에서도 페이지 종료 전 토큰을 저장하는지 확인."""
        page = Mock()
        post = PostRef("123", "test", "https://us-insight.com/secrets/123")
        with patch.object(self.client, "_new_page", return_value=page), \
                patch.object(self.client, "_goto"), \
                patch.object(self.client, "_wait_for_network_idle"), \
                patch.object(self.client, "_collect_dom_media_urls", return_value=[]), \
                patch.object(self.client, "_trigger_player"), \
                patch.object(self.client, "_refresh_saved_session") as save:
            with self.assertRaises(AudioNotAvailableYet):
                self.client.download_audio_from_post(post, self.root)
            save.assert_called_once_with(page)
            page.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

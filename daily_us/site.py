from __future__ import annotations

import errno
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import unquote, urljoin, urlparse

import imageio_ffmpeg
import requests
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Response,
    TimeoutError,
    sync_playwright,
)

from daily_us.config import SiteConfig

LOGGER = logging.getLogger(__name__)

REFRESH_TOKEN_COOKIE = "us_refreshToken"
TOKEN_EXPIRY_WARNING_DAYS = 3

# Capture uses the site's mobile layout, whose body column is 430px wide against the desktop 660px.
# A desktop shot shows the same post that much smaller once it reaches a phone.
BODY_CAPTURE_VIEWPORT = {"width": 430, "height": 932}
BODY_CAPTURE_SCALE_FACTOR = 2
# Telegram shrinks a photo's long side to 2560px, so slices stay under it to arrive untouched.
BODY_CAPTURE_MAX_PIXEL_HEIGHT = 2560
BODY_CAPTURE_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
# 굿모닝 담쌤의 "뉴스 브리핑" 섹션은 보내지 않는다. 이 섹션은 제목 띠 이미지로 시작하는데
# 띠 이미지에 alt 나 data 속성이 없어서, 매일 같은 파일을 쓰는 이미지 주소로 찾는다.
BODY_CAPTURE_SKIPPED_SECTION_MARKER = "1753653851925_daccba1e"


@dataclass(frozen=True)
class PostRef:
    post_id: str
    title: str
    url: str


@dataclass(frozen=True)
class DownloadedAudio:
    path: Path
    body_text: str
    body_ready: bool = True


@dataclass(frozen=True)
class PostBody:
    text: str
    is_ready: bool


@dataclass(frozen=True)
class CapturedPostBody:
    image_paths: list[Path]
    is_ready: bool


@dataclass(frozen=True)
class DownloadedPostContent:
    body_text: str
    pdf_paths: list[Path]


class AudioNotAvailableYet(RuntimeError):
    """Raised when a post exists but its audio player/media URL is not available yet."""


class LoginRequired(RuntimeError):
    """Raised when US Insight no longer accepts the saved login session."""


class UsInsightClient:
    def __init__(self, config: SiteConfig) -> None:
        self.config = config
        self._playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.body_capture_context: BrowserContext | None = None
        # 인증 상태 읽기부터 브라우저 종료까지 유지하는 프로세스 간 잠금
        self._session_lock = ExitStack()

    def __enter__(self) -> "UsInsightClient":
        # 같은 인증 파일을 사용하는 로그인·점검·폴링을 순서대로 실행
        self._session_lock.enter_context(_lock_auth_state(self.config.auth_state_path))
        try:
            self._playwright = sync_playwright().start()
            self.browser = self._playwright.chromium.launch(
                headless=self.config.headless,
            )
            self.context = self._create_context()
        except BaseException:
            # 초기화 중 실패한 경우에도 브라우저 자원과 세션 잠금 해제
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            if self.body_capture_context:
                self.body_capture_context.close()
        except Exception:
            LOGGER.exception("Failed to close body capture context.")

        try:
            if self.context:
                self.context.close()
        except Exception:
            LOGGER.exception("Failed to close browser context.")
        finally:
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                LOGGER.exception("Failed to close browser.")
            finally:
                try:
                    if self._playwright:
                        self._playwright.stop()
                finally:
                    self._session_lock.close()

    def open_login_page(self) -> None:
        """서버의 인증 성공과 피드 진입을 감지해 로그인 세션 자동 저장."""
        page = self._new_page()
        authenticated = False

        def on_response(response: Response) -> None:
            """사이트의 현재 사용자 조회 응답으로 인증 성공 여부 확인.

            Args:
                response: 로그인 중 브라우저 컨텍스트에서 받은 응답.
            """
            nonlocal authenticated
            parsed_url = urlparse(response.url)
            # US Insight의 사용자 인증 응답만 사용. 만료된 쿠키나 로그인 전 피드 주소만으로 저장하지 않도록 제한
            if (parsed_url.scheme, parsed_url.netloc, parsed_url.path) == (
                "https", "api.us-insight.com", "/v3/auth/me"
            ):
                authenticated = response.status == 200

        # 로그인 팝업에서 돌아온 피드도 확인할 수 있도록 같은 컨텍스트의 응답 수신
        context = page.context
        context.on("response", on_response)
        try:
            print("브라우저에서 네이버 로그인과 동의·회원 연결을 완료하세요. 로그인 세션은 자동으로 저장됩니다.")
            print("저장이 끝나면 브라우저가 자동으로 닫힙니다. 그때까지 창을 열어 두세요.")
            self._goto(page, self.config.feed_url)
            while True:
                if authenticated:
                    for candidate in context.pages:
                        parsed_url = urlparse(candidate.url)
                        is_feed = parsed_url.netloc == "us-insight.com" and parsed_url.path.startswith("/feed")
                        # 본문 조회를 기다리는 동안 응답 콜백이 인증 상태를 바꿀 수 있으므로, 조회 완료 후 authenticated 재확인
                        if is_feed and not self._is_logged_out(candidate) and authenticated:
                            # 인증된 피드의 실제 쿠키와 저장소를 저장한 뒤에만 완료 안내
                            self._save_auth_state(candidate)
                            self._log_session_expiry(context)
                            print("로그인 세션이 저장되었습니다.")
                            return
                # 터미널 입력 없이 브라우저 이벤트를 처리하며 로그인 완료 대기
                page.wait_for_timeout(500)
        except PlaywrightError as exc:
            # 창 종료로 중단된 로그인에는 재실행 안내 제공. 다른 브라우저 오류는 그대로 전달
            # 로그인 팝업만 닫힌 경우도 구분할 수 있도록 Playwright의 종료 메시지 확인
            target_closed = "Target page, context or browser has been closed" in str(exc)
            if not target_closed and not page.is_closed() and self.browser and self.browser.is_connected():
                raise
            raise LoginRequired(
                "로그인 창이 닫혀 세션 저장을 완료하지 못했습니다. "
                "`python -m daily_us login`을 다시 실행하고 브라우저에서 로그인을 완료하세요. "
                "세션이 자동 저장되어 브라우저가 닫힐 때까지 창을 열어 두세요."
            ) from None
        finally:
            context.remove_listener("response", on_response)
            if not page.is_closed():
                page.close()

    def find_posts(self, title_contains: str, max_posts: int) -> list[PostRef]:
        page = self._new_page()
        try:
            LOGGER.info("Opening feed: %s", self.config.feed_url)
            self._goto(page, self.config.feed_url)
            self._wait_for_network_idle(page)
            self._wait_for_page_settle(page)
            if self._is_logged_out(page):
                raise LoginRequired(
                    "US Insight is not available as a logged-in feed page. "
                    "This may mean the session expired, the sign-in page is showing, "
                    "or the page could not be inspected during a temporary load/render issue. "
                    "Run `python -m daily_us check-login` first; if it fails consistently, "
                    "run `python -m daily_us login` and complete Naver login before polling."
                )
            posts = self._extract_post_links(page, title_contains, max_posts)
            LOGGER.info("Found %s candidate posts for title filter %r", len(posts), title_contains)
            self._refresh_saved_session(page)
            return posts
        finally:
            page.close()

    def download_audio_from_post(
        self,
        post: PostRef,
        download_dir: Path,
        audio_filename_template: str | None = None,
    ) -> DownloadedAudio:
        page = self._new_page()
        media_urls: list[str] = []

        def on_response(response: Response) -> None:
            if _looks_like_audio_response(response):
                media_urls.append(response.url)

        page.on("response", on_response)
        try:
            LOGGER.info("Opening post: %s", post.url)
            self._goto(page, post.url)
            self._wait_for_network_idle(page)
            media_urls.extend(self._collect_dom_media_urls(page, post.url))

            if not media_urls:
                self._trigger_player(page)
                media_urls.extend(self._collect_dom_media_urls(page, post.url))

            media_urls = _dedupe(media_urls)
            if not media_urls:
                raise AudioNotAvailableYet(
                    f"Audio is not available yet for post: {post.title} ({post.url})"
                )

            LOGGER.info("Using media URL: %s", media_urls[0])
            user_agent = page.evaluate("() => navigator.userAgent")
            audio_path = self._download_media(
                media_urls[0],
                post,
                download_dir,
                output_stem=_audio_stem_from_post(post, audio_filename_template),
                referer_url=post.url,
                user_agent=user_agent,
            )
            body = _post_body_from_page(page, post)
            return DownloadedAudio(
                path=audio_path,
                body_text=body.text,
                body_ready=body.is_ready,
            )
        finally:
            # 오디오 처리 중 갱신된 인증 상태도 페이지를 닫기 전에 저장
            self._refresh_saved_session(page)
            page.close()

    def fetch_post_body_text(self, post: PostRef) -> str:
        return self.fetch_post_body(post).text

    def fetch_post_body(self, post: PostRef) -> PostBody:
        page = self._new_page()
        try:
            LOGGER.info("Opening post for body text: %s", post.url)
            self._goto(page, post.url)
            self._wait_for_network_idle(page)
            self._wait_for_page_settle(page)
            return _post_body_from_page(page, post)
        finally:
            # 본문 준비 여부와 관계없이 유효한 로그인 상태 저장
            self._refresh_saved_session(page)
            page.close()

    def capture_post_body_images(self, post: PostRef, output_dir: Path) -> CapturedPostBody:
        """Screenshot a post body as a sequence of Telegram-ready photos.

        Tables and charts on this site are posted as images, which the Markdown extraction drops,
        leaving the reader with headings and no numbers. Shooting the rendered body keeps them.

        Args:
            post: The post to capture.
            output_dir: Directory the PNG slices are written to.

        Returns:
            The slices in reading order. Empty when the post body is not ready yet.
        """
        page = self._body_capture_context().new_page()
        try:
            LOGGER.info("Opening post for body capture: %s", post.url)
            self._goto(page, post.url)
            self._wait_for_network_idle(page)
            self._wait_for_page_settle(page)
            _scroll_to_load_lazy_images(page)

            if not _is_post_body_ready(_extract_post_body_text(page)):
                LOGGER.info("Body is not ready for capture yet: %s", post.title)
                return CapturedPostBody(image_paths=[], is_ready=False)

            prepared = _prepare_page_for_body_capture(page)
            if not prepared.get("editorFound"):
                LOGGER.warning("Post body element was not found for capture: %s", post.url)
                return CapturedPostBody(image_paths=[], is_ready=False)

            LOGGER.info(
                "Prepared body capture for %s: repaired %s data URI(s), hid %s overlay(s), "
                "leading images removed=%s, skipped section blocks=%s, unloaded images=%s",
                post.title,
                prepared.get("repairedDataUris"),
                prepared.get("hiddenOverlays"),
                prepared.get("leadingImagesRemoved"),
                prepared.get("skippedSectionBlocks"),
                prepared.get("brokenImages"),
            )
            if prepared.get("brokenImages"):
                LOGGER.warning(
                    "%s image(s) never finished loading and will appear blank: %s",
                    prepared.get("brokenImages"),
                    post.url,
                )

            max_css_height = BODY_CAPTURE_MAX_PIXEL_HEIGHT // BODY_CAPTURE_SCALE_FACTOR
            layout = _body_capture_layout(page, max_css_height)
            if not layout or not layout["chunks"]:
                LOGGER.warning("Post body has no visible blocks to capture: %s", post.url)
                return CapturedPostBody(image_paths=[], is_ready=False)

            output_dir.mkdir(parents=True, exist_ok=True)
            stem = _safe_filename(post.post_id) or "body"
            image_paths = []
            for index, chunk in enumerate(layout["chunks"], start=1):
                target = output_dir / f"{stem}-{index:02d}.png"
                page.screenshot(
                    path=str(target),
                    full_page=True,
                    clip={
                        "x": layout["left"],
                        "y": chunk["y"],
                        "width": layout["width"],
                        "height": chunk["height"],
                    },
                )
                image_paths.append(target)

            LOGGER.info(
                "Captured %s body image(s) at %spx wide: %s",
                len(image_paths),
                int(layout["width"] * BODY_CAPTURE_SCALE_FACTOR),
                post.title,
            )
            return CapturedPostBody(image_paths=image_paths, is_ready=True)
        finally:
            # 모바일 캡처 컨텍스트에서 발급받은 최신 토큰 저장
            self._refresh_saved_session(page)
            page.close()

    def _body_capture_context(self) -> BrowserContext:
        """Open the phone-sized browser context used for capture, creating it on first use.

        The desktop context drives feed parsing and the audio player, so capture takes its own
        context instead of resizing that one.

        Returns:
            The mobile context, reused until the desktop context saves newer authentication state.
        """
        if self.body_capture_context is not None:
            return self.body_capture_context
        if not self.browser:
            raise RuntimeError("Browser is not open.")

        context_options: dict[str, object] = {
            "viewport": dict(BODY_CAPTURE_VIEWPORT),
            "device_scale_factor": BODY_CAPTURE_SCALE_FACTOR,
            "is_mobile": True,
            "has_touch": True,
            "user_agent": BODY_CAPTURE_MOBILE_USER_AGENT,
            "locale": "ko-KR",
        }
        context = self._create_context(**context_options)
        self.body_capture_context = context
        return context

    def fetch_post_content(
        self,
        post: PostRef,
        download_dir: Path,
    ) -> DownloadedPostContent:
        page = self._new_page()
        content_payloads: list[dict[str, object]] = []
        post_cms_id = _post_cms_id_from_url(post.url)

        def on_response(response: Response) -> None:
            if f"/v2/contents/secret/{post_cms_id}" not in response.url:
                return
            try:
                content_payloads.append(json.loads(response.text()))
            except Exception:
                LOGGER.exception("Could not read post content API response: %s", response.url)

        page.on("response", on_response)
        try:
            LOGGER.info("Opening post for content and PDFs: %s", post.url)
            self._goto(page, post.url)
            self._wait_for_network_idle(page)
            self._wait_for_page_settle(page)

            body_text = _extract_post_body_text(page) or _escape_markdown_v2(post.title)
            pdf_paths = []
            for pdf in _extract_pdf_items(content_payloads):
                pdf_paths.append(self._download_pdf(pdf["url"], pdf["filename"], download_dir))
            return DownloadedPostContent(body_text=body_text, pdf_paths=pdf_paths)
        finally:
            # PDF와 본문을 읽는 동안 갱신된 인증 상태 저장
            self._refresh_saved_session(page)
            page.close()

    def _download_pdf(self, pdf_url: str, filename: str, download_dir: Path) -> Path:
        download_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(unicodedata.normalize("NFC", filename))
        if not safe_name.lower().endswith(".pdf"):
            safe_name = f"{safe_name}.pdf"

        target = _unique_path(download_dir / safe_name)
        partial_target = _unique_path(target.with_name(f"{target.name}.part"))
        first_bytes = b""

        try:
            with requests.get(pdf_url, stream=True, timeout=120) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()

                with partial_target.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        if len(first_bytes) < 8:
                            first_bytes += chunk[: 8 - len(first_bytes)]
                        handle.write(chunk)

                if "pdf" not in content_type and not first_bytes.startswith(b"%PDF"):
                    raise RuntimeError(
                        f"Downloaded file is not a PDF: {content_type} {pdf_url}"
                    )

            partial_target.replace(target)
            return target
        except Exception:
            if partial_target.exists():
                partial_target.unlink()
            raise

    def _download_media(
        self,
        media_url: str,
        post: PostRef,
        download_dir: Path,
        output_stem: str,
        referer_url: str,
        user_agent: str,
    ) -> Path:
        if not self.context:
            raise RuntimeError("Browser context is not open.")

        download_dir.mkdir(parents=True, exist_ok=True)
        if _is_hls_playlist(media_url):
            return self._download_hls_as_mp3(
                media_url,
                download_dir,
                output_stem,
                referer_url,
                user_agent,
            )

        response = self.context.request.get(media_url, timeout=120000)
        if not response.ok:
            raise RuntimeError(f"Failed to download audio: HTTP {response.status} {media_url}")

        filename = _filename_from_headers(response.headers) or _filename_from_url(media_url)
        if not filename or "." not in filename:
            filename = f"{output_stem}.mp3"
        elif not filename.lower().endswith((".mp3", ".m4a", ".mpeg", ".mpga")):
            filename = f"{output_stem}.mp3"

        target = _unique_path(download_dir / filename)
        target.write_bytes(response.body())
        return target

    def _download_hls_as_mp3(
        self,
        media_url: str,
        download_dir: Path,
        output_stem: str,
        referer_url: str,
        user_agent: str,
    ) -> Path:
        if not self.context:
            raise RuntimeError("Browser context is not open.")

        target = _unique_path(download_dir / f"{output_stem}.mp3")
        headers = self._ffmpeg_headers(media_url, referer_url)
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-user_agent",
            user_agent,
        ]
        if headers:
            command.extend(["-headers", headers])
        command.extend(
            [
                "-i",
                media_url,
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(target),
            ]
        )

        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(
                "Failed to convert HLS audio to mp3 with ffmpeg: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return target

    def _ffmpeg_headers(self, media_url: str, referer_url: str) -> str:
        if not self.context:
            return ""

        header_lines = [f"Referer: {referer_url}"]
        cookies = self.context.cookies([media_url, referer_url])
        cookie_header = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
        if cookie_header:
            header_lines.append(f"Cookie: {cookie_header}")
        return "\r\n".join(header_lines) + "\r\n"

    def _extract_post_links(self, page: Page, title_contains: str, max_posts: int) -> list[PostRef]:
        LOGGER.info("Extracting post links from feed.")
        raw_links = page.evaluate(
            """
            ({ titleContains, maxPosts }) => {
              const links = [];
              for (const anchor of Array.from(document.querySelectorAll('a[href]'))) {
                const title = (anchor.innerText || anchor.textContent || '')
                  .replace(/\\s+/g, ' ')
                  .trim();
                const href = anchor.getAttribute('href');
                if (!title || !href) continue;
                if (titleContains && !title.includes(titleContains)) continue;
                links.push({ title, href });
                if (links.length >= maxPosts) break;
              }
              return links;
            }
            """,
            {"titleContains": title_contains, "maxPosts": max_posts},
        )

        posts: list[PostRef] = []
        for raw_link in raw_links:
            if not isinstance(raw_link, dict):
                continue

            title = _normalize_text(str(raw_link.get("title") or ""))
            href = raw_link.get("href")

            if not title or not href:
                continue
            url = urljoin(page.url, href)
            posts.append(PostRef(post_id=url, title=title, url=url))
            if len(posts) >= max_posts:
                break

        return posts

    def _collect_dom_media_urls(self, page: Page, base_url: str) -> list[str]:
        urls = page.evaluate(
            """
            () => {
              const urls = [];
              for (const el of document.querySelectorAll('audio, audio source, video source')) {
                const src = el.currentSrc || el.src || el.getAttribute('src');
                if (src) urls.push(src);
              }
              for (const el of document.querySelectorAll('a[href]')) {
                const href = el.getAttribute('href');
                if (href && /\\.(mp3|m4a|mpeg|mpga|m3u8)(\\?|#|$)/i.test(href)) urls.push(href);
              }
              return urls;
            }
            """
        )
        return [urljoin(base_url, item) for item in urls if isinstance(item, str)]

    def _trigger_player(self, page: Page) -> None:
        LOGGER.info("No media URL found yet; trying to trigger the audio player.")
        try:
            page.evaluate(
                """
                async () => {
                  const audio = document.querySelector('audio');
                  if (audio && audio.play) {
                    try { await audio.play(); } catch (_) {}
                  }
                }
                """
            )
        except Exception as exc:
            LOGGER.debug("Could not trigger native audio element: %s", exc)

        selectors = [
            "button[aria-label*='play' i]",
            "button[title*='play' i]",
            "button:has-text('재생')",
            "[role='button']:has-text('재생')",
            "button:has-text('Play')",
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0:
                    locator.click(timeout=2000)
                    page.wait_for_timeout(5000)
                    return
            except Exception as exc:
                LOGGER.debug("Could not click player selector %s: %s", selector, exc)

        page.wait_for_timeout(3000)

    def _create_context(self, **options: object) -> BrowserContext:
        """최신 인증 파일로 브라우저 컨텍스트 생성.

        Args:
            options: 기본 데스크톱 설정을 덮어쓸 캡처용 브라우저 옵션.

        Returns:
            쿠키와 저장소를 복원한 컨텍스트.
        """
        if not self.browser:
            raise RuntimeError("Browser is not open.")
        context_options = {
            "viewport": {"width": 1366, "height": 900},
            "accept_downloads": True,
            **options,
        }
        auth_state = self._load_saved_auth_state()
        if auth_state is not None:
            context_options["storage_state"] = auth_state
        context = self.browser.new_context(**context_options)
        self._restore_session_storage(context)
        context.set_default_timeout(self.config.navigation_timeout_ms)
        return context

    def _new_page(self) -> Page:
        """최신 인증 상태를 사용하는 데스크톱 페이지 생성."""
        # 모바일에서 인증 상태를 저장한 뒤에는 최신 파일로 다시 생성
        if self.context is None:
            self.context = self._create_context()
        return self.context.new_page()

    def _wait_for_network_idle(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except TimeoutError:
            LOGGER.debug("networkidle timed out; continuing with current DOM.")

    def _goto(self, page: Page, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            if "is interrupted by another navigation" not in str(exc):
                raise
            LOGGER.debug("Navigation to %s was interrupted by redirect; waiting on current page.", url)
            self._wait_for_domcontentloaded(page)

    def _wait_for_domcontentloaded(self, page: Page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except TimeoutError:
            LOGGER.debug("domcontentloaded timed out after interrupted navigation.")

    def _verify_feed_access(self, save_state: bool = True) -> tuple[bool, str]:
        """피드 접근을 확인하고 성공한 점검에서 갱신된 인증 상태 저장.

        Args:
            save_state: 점검 중 갱신된 토큰을 다음 실행에서도 사용할지 여부.

        Returns:
            로그인 확인 결과와 확인한 페이지 주소.
        """
        page = self._new_page()
        try:
            self._goto(page, self.config.feed_url)
            self._wait_for_network_idle(page)
            self._wait_for_page_settle(page)
            parsed_url = urlparse(page.url)
            is_feed = parsed_url.netloc == "us-insight.com" and parsed_url.path.startswith("/feed")
            verified = is_feed and not self._is_logged_out(page)
            if verified and save_state:
                self._save_auth_state(page)
            if verified:
                self._log_session_expiry()
            return verified, page.url
        finally:
            page.close()

    def _wait_for_page_settle(self, page: Page) -> None:
        previous_url = ""
        for _ in range(10):
            current_url = page.url
            if current_url == previous_url:
                return
            previous_url = current_url
            page.wait_for_timeout(1000)
        LOGGER.warning("Page did not settle after 10s, current URL: %s", page.url)

    def _is_logged_out(self, page: Page) -> bool:
        parsed_url = urlparse(page.url)
        if parsed_url.netloc in {"nid.naver.com", "api.us-insight.com"}:
            return True
        if "/signin" in parsed_url.path:
            return True

        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            LOGGER.exception("Could not inspect page body while checking login state.")
            return True

        return "계정으로 로그인" in body and "비밀번호 찾기" in body

    def _refresh_saved_session(self, page: Page) -> None:
        """로그인된 사이트 페이지의 최신 인증 상태 저장.

        Args:
            page: 피드 또는 게시글을 읽은 페이지.
        """
        try:
            # 다른 사이트나 로그인 화면의 상태로 인증 파일을 덮어쓰지 않도록 제한
            if urlparse(page.url).netloc != urlparse(self.config.feed_url).netloc:
                return
            if self._is_logged_out(page):
                return
            self._save_auth_state(page)
            LOGGER.info("Refreshed saved login session state.")
        except Exception:
            LOGGER.exception("Could not refresh the saved login session state.")
            return
        self._log_session_expiry(page.context)

    def _log_session_expiry(self, context: BrowserContext | None = None) -> None:
        """지정한 컨텍스트의 갱신 토큰 만료 시각 기록.

        Args:
            context: 토큰을 확인할 컨텍스트. 생략하면 데스크톱 컨텍스트 사용.
        """
        context = context or self.context
        if not context:
            return

        try:
            cookies = {
                cookie["name"]: cookie
                for cookie in context.cookies([self.config.feed_url])
            }
        except Exception:
            LOGGER.exception("Could not read cookies while checking session expiry.")
            return

        refresh_cookie = cookies.get(REFRESH_TOKEN_COOKIE)
        expires = float(refresh_cookie.get("expires", -1)) if refresh_cookie else -1.0
        if expires <= 0:
            LOGGER.warning(
                "Saved session has no %s expiry; login may be required soon.",
                REFRESH_TOKEN_COOKIE,
            )
            return

        expires_at = datetime.fromtimestamp(expires)
        remaining = expires_at - datetime.now()
        remaining_days = remaining.total_seconds() / 86400
        if remaining <= timedelta(days=TOKEN_EXPIRY_WARNING_DAYS):
            LOGGER.warning(
                "Login refresh token expires soon: %s (%.1f day(s) left). "
                "Run `python -m daily_us login` before it expires.",
                expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                remaining_days,
            )
        else:
            LOGGER.info(
                "Login refresh token expires at %s (%.1f day(s) left).",
                expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                remaining_days,
            )

    def _load_saved_auth_state(self) -> dict | None:
        if not self.config.auth_state_path.exists():
            return None

        try:
            return json.loads(self.config.auth_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning(
                "Saved auth state file is unreadable or corrupted; starting without it: %s",
                self.config.auth_state_path,
            )
            return None

    def _save_auth_state(self, page: Page) -> None:
        """페이지가 실제로 사용한 컨텍스트의 인증 상태 저장.

        Args:
            page: 최신 쿠키와 저장소를 가진 페이지.
        """
        context = page.context
        state = context.storage_state()
        _write_json_atomically(self.config.auth_state_path, state)

        # 다른 컨텍스트의 토큰은 재사용하지 않고 다음 접근 시 저장된 최신 상태로 복원
        for attribute in ("context", "body_capture_context"):
            other_context = getattr(self, attribute)
            if other_context is not None and other_context is not context:
                setattr(self, attribute, None)
                other_context.close()

        origin = page.evaluate("() => window.location.origin")
        session_storage = page.evaluate(
            """
            () => {
              const items = {};
              for (let index = 0; index < window.sessionStorage.length; index += 1) {
                const key = window.sessionStorage.key(index);
                items[key] = window.sessionStorage.getItem(key);
              }
              return items;
            }
            """
        )
        _write_json_atomically(self.config.session_storage_path, {origin: session_storage})

    def _restore_session_storage(self, context: BrowserContext) -> None:
        if not self.config.session_storage_path.exists():
            return

        try:
            raw = self.config.session_storage_path.read_text(encoding="utf-8")
            storage_by_origin = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            LOGGER.warning(
                "Saved session storage file is unreadable or corrupted; ignoring it: %s",
                self.config.session_storage_path,
            )
            return
        script = f"""
        (() => {{
          const storageByOrigin = {json.dumps(storage_by_origin, ensure_ascii=False)};
          const items = storageByOrigin[window.location.origin];
          if (!items) return;
          for (const [key, value] of Object.entries(items)) {{
            window.sessionStorage.setItem(key, value);
          }}
        }})();
        """
        context.add_init_script(script=script)


@contextmanager
def _lock_auth_state(path: Path) -> Iterator[None]:
    """인증 파일을 공유하는 프로세스의 브라우저 실행을 직렬화.

    Args:
        path: 실행 중 읽고 갱신할 인증 상태 파일 경로.

    Yields:
        읽기·토큰 갱신·저장이 모두 끝날 때까지 유지할 잠금 구간.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 고정 파일의 OS 잠금 사용. 프로세스가 강제 종료돼도 OS에서 잠금 해제
    # 파일은 삭제하지 않아 대기 중인 프로세스도 동일한 파일을 잠그도록 유지
    with lock_path.open("a+b") as handle:
        LOGGER.info("Waiting for exclusive login session access: %s", lock_path)
        if os.name == "nt":
            import msvcrt

            # Windows의 바이트 범위 잠금을 위한 첫 바이트 확보
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        LOGGER.info("Acquired exclusive login session access.")
        yield


def _write_json_atomically(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _looks_like_audio_response(response: Response) -> bool:
    url = response.url.lower()
    content_type = response.headers.get("content-type", "").lower()
    return (
        ".m3u8" in url
        or ".mp3" in url
        or ".m4a" in url
        or "audio/" in content_type
        or "mpegurl" in content_type
        or "mpeg" in content_type
    )


def _is_hls_playlist(url: str) -> bool:
    return ".m3u8" in urlparse(url).path.lower()


def _audio_stem_from_post(post: PostRef, template: str | None = None) -> str:
    date_slug = _date_slug_from_title(post.title)
    if template:
        return _safe_filename(
            template.format(
                title=post.title,
                date=date_slug,
                **{"mm-dd": date_slug},
            )
        )
    return _safe_filename(post.title)


def _date_slug_from_title(title: str) -> str:
    match = re.search(r"(\d{1,2})월\s*(\d{1,2})일", title)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        return f"{month:02d}-{day:02d}"
    return "unknown-date"


def _post_cms_id_from_url(url: str) -> str:
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    if not path_parts:
        raise RuntimeError(f"Could not extract post id from URL: {url}")
    return path_parts[-1]


def _extract_pdf_items(payloads: list[dict[str, object]]) -> list[dict[str, str]]:
    pdf_items: list[dict[str, str]] = []
    for payload in payloads:
        content = payload.get("content")
        if not isinstance(content, dict):
            continue
        raw_pdfs = content.get("pdf")
        if not isinstance(raw_pdfs, list):
            continue
        for raw_pdf in raw_pdfs:
            if not isinstance(raw_pdf, dict):
                continue
            pdf_url = raw_pdf.get("pdfUrl")
            filename = raw_pdf.get("fileName")
            if not isinstance(pdf_url, str) or not pdf_url:
                continue
            if not isinstance(filename, str) or not filename:
                filename = _filename_from_url(pdf_url) or "attachment.pdf"
            pdf_items.append({"url": pdf_url, "filename": filename})
    return _dedupe_pdf_items(pdf_items)


def _post_body_from_page(page: Page, post: PostRef) -> PostBody:
    body_text = _extract_post_body_text(page)
    is_ready = _is_post_body_ready(body_text)
    return PostBody(
        text=body_text or _escape_markdown_v2(post.title),
        is_ready=is_ready,
    )


def _is_post_body_ready(body_text: str) -> bool:
    normalized = _normalize_text(body_text)
    if not normalized:
        return False
    return not _is_script_preparing_marker(normalized)


def _is_script_preparing_marker(value: str) -> bool:
    return re.fullmatch(r"스크립트\s*준비\s*중", _normalize_text(value)) is not None


def _scroll_to_load_lazy_images(page: Page) -> None:
    """Scroll the whole page once so lazily loaded images start fetching.

    Args:
        page: The post page to scroll.
    """
    page.evaluate(
        """
        async () => {
          const step = window.innerHeight || 900;
          for (let y = 0; y < document.body.scrollHeight; y += step) {
            window.scrollTo(0, y);
            await new Promise((resolve) => setTimeout(resolve, 120));
          }
          window.scrollTo(0, 0);
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
        """
    )


def _prepare_page_for_body_capture(page: Page) -> dict[str, object]:
    """Repair broken images and strip everything that does not belong in the capture.

    Removes the page chrome, the images above the first line of text and the news briefing.

    Args:
        page: The post page to prepare.

    Returns:
        Counts of what was repaired, hidden and removed, for logging.
    """
    return page.evaluate(
        """
        async (skippedSectionMarker) => {
          const editor = document.querySelector('.tiptap.ProseMirror');
          if (!editor) return { editorFound: false };

          // us-insight appends ?w=1080 to every image src, data: URIs included, and that suffix
          // corrupts the base64 payload. Those images are blank on the site too.
          let repairedDataUris = 0;
          for (const image of editor.querySelectorAll('img')) {
            const src = image.getAttribute('src') || '';
            if (!src.startsWith('data:')) continue;
            const query = src.indexOf('?');
            if (query === -1) continue;
            image.setAttribute('src', src.slice(0, query));
            repairedDataUris += 1;
          }

          // Fixed and sticky chrome is painted into full-page screenshots and covers the body.
          // Matching on position rather than class names survives the site restyling its banners.
          // The scroll-to-top button and the toast only exist while the page is scrolled down, and
          // the site rebuilds them as the screenshot moves through the page, so an observer keeps
          // sweeping until the capture is done rather than hiding what happens to be there now.
          let hiddenOverlays = 0;
          const sweepOverlays = () => {
            for (const element of document.querySelectorAll('body *')) {
              const style = getComputedStyle(element);
              if (style.position !== 'fixed' && style.position !== 'sticky') continue;
              if (style.display === 'none') continue;
              if (editor.contains(element) || element.contains(editor)) continue;
              element.style.setProperty('display', 'none', 'important');
              hiddenOverlays += 1;
            }
          };

          let sweepQueued = false;
          const observer = new MutationObserver(() => {
            if (sweepQueued) return;
            sweepQueued = true;
            requestAnimationFrame(() => {
              sweepQueued = false;
              sweepOverlays();
            });
          });
          observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class', 'style'],
          });

          const scrollHeight = document.body.scrollHeight;
          for (const offset of [scrollHeight / 2, scrollHeight, 0]) {
            window.scrollTo(0, offset);
            await new Promise((resolve) => setTimeout(resolve, 400));
            sweepOverlays();
          }

          // 굿모닝 담쌤은 표지 그림 아래에 섹션 제목 띠까지 붙는다. 글자가 시작되기 전의 그림은
          // 모두 겉치레라 함께 지운다.
          let leadingImagesRemoved = 0;
          for (const child of editor.children) {
            if ((child.innerText || '').trim()) break;
            if (!child.querySelector('img')) continue;
            child.style.setProperty('display', 'none', 'important');
            leadingImagesRemoved += 1;
          }

          // The skipped section is its title banner followed by a run of news items and blank
          // lines. It ends at the first block that carries text and is not a news item, which is
          // where an attachment section or the closing line begins.
          let skippedSectionBlocks = 0;
          const blocks = [...editor.children];
          const sectionStart = blocks.findIndex((child) => {
            const banner = child.querySelector('img');
            return !!banner && (banner.getAttribute('src') || '').includes(skippedSectionMarker);
          });
          if (sectionStart !== -1) {
            for (const child of blocks.slice(sectionStart)) {
              const isNewsItem = (child.className || '').toString().includes('node-callout');
              const hasText = !!(child.innerText || '').trim();
              if (skippedSectionBlocks > 0 && hasText && !isNewsItem) break;
              child.style.setProperty('display', 'none', 'important');
              skippedSectionBlocks += 1;
            }
          }

          // Only images that survived the trimming are worth waiting for.
          const visibleImages = () => [...editor.querySelectorAll('img')]
            .filter((image) => image.getBoundingClientRect().width > 0);

          const pending = visibleImages()
            .filter((image) => !image.complete)
            .map((image) => new Promise((resolve) => {
              image.addEventListener('load', resolve, { once: true });
              image.addEventListener('error', resolve, { once: true });
            }));
          await Promise.race([
            Promise.all(pending),
            new Promise((resolve) => setTimeout(resolve, 15000)),
          ]);

          const brokenImages = visibleImages()
            .filter((image) => !image.complete || image.naturalWidth === 0).length;

          return {
            editorFound: true,
            repairedDataUris,
            hiddenOverlays,
            leadingImagesRemoved,
            skippedSectionBlocks,
            brokenImages,
          };
        }
        """,
        BODY_CAPTURE_SKIPPED_SECTION_MARKER,
    )


def _body_capture_layout(page: Page, max_css_height: int) -> dict[str, object] | None:
    """Group top-level body blocks into slices no taller than the given height.

    Args:
        page: The prepared post page.
        max_css_height: Tallest slice allowed, in CSS pixels.

    Returns:
        The body's position and its slices, or None when the body has no visible block.
    """
    return page.evaluate(
        """
        (maxCssHeight) => {
          const editor = document.querySelector('.tiptap.ProseMirror');
          if (!editor) return null;

          const rect = editor.getBoundingClientRect();
          const rows = [...editor.children]
            .filter((child) => child.getBoundingClientRect().height > 0)
            .map((child) => {
              const box = child.getBoundingClientRect();
              return { top: box.top + window.scrollY, bottom: box.bottom + window.scrollY };
            });
          if (!rows.length) return null;

          // Telegram refuses a photo whose sides differ by more than 20 times, so a sliver of a
          // slice fails the whole album. Every slice stays well clear of that ratio.
          const minCssHeight = Math.ceil(rect.width / 6);

          const boundaries = rows.map((row) => row.top);
          boundaries.push(rows[rows.length - 1].bottom);

          // Take as many whole blocks as fit, then cut at that boundary. A table split across two
          // photos loses its header on the second one, so a block is only ever cut when the block
          // alone is taller than the cap, and such a range is marked to be divided below.
          const ranges = [];
          let start = boundaries[0];
          let index = 1;
          while (index < boundaries.length) {
            let end = -1;
            let next = index;
            while (next < boundaries.length && boundaries[next] - start <= maxCssHeight) {
              end = boundaries[next];
              next += 1;
            }
            const oversizedBlock = end === -1;
            if (oversizedBlock) {
              end = boundaries[index];
              next = index + 1;
            }
            ranges.push({ from: start, to: end, oversizedBlock });
            start = end;
            index = next;
          }

          const last = ranges[ranges.length - 1];
          if (ranges.length > 1 && last.to - last.from < minCssHeight) {
            ranges.pop();
            ranges[ranges.length - 1].to = last.to;
          }

          // Only a single block taller than the cap is divided, and into equal parts so that no
          // part is a sliver.
          const chunks = [];
          for (const range of ranges) {
            const span = range.to - range.from;
            const parts = range.oversizedBlock ? Math.max(1, Math.ceil(span / maxCssHeight)) : 1;
            const step = span / parts;
            for (let part = 0; part < parts; part += 1) {
              chunks.push({ y: range.from + step * part, height: Math.max(step, minCssHeight) });
            }
          }

          return { left: rect.left + window.scrollX, width: rect.width, chunks };
        }
        """,
        max_css_height,
    )


def _extract_post_body_text(page: Page) -> str:
    body_markdown = page.evaluate(
        """
        () => {
          const editor = document.querySelector('.tiptap.ProseMirror');
          if (!editor) return '';

          const escapeMarkdown = (value) => String(value || '')
            .replace(/\\u00a0/g, ' ')
            .replace(/([\\\\_*\\[\\]()~`>#+\\-=|{}.!])/g, '\\\\$1');

          const normalizeText = (value) => String(value || '')
            .replace(/\\u00a0/g, ' ')
            .replace(/[ \\t\\r\\n]+/g, ' ')
            .trim();

          const escapeLinkUrl = (value) => String(value || '').replace(/[()\\\\]/g, '\\\\$&');

          const renderPlain = (node) => escapeMarkdown(normalizeText(node.innerText || node.textContent || ''));

          const renderInline = (node) => {
            if (node.nodeType === Node.TEXT_NODE) {
              return escapeMarkdown(node.nodeValue || '');
            }
            if (node.nodeType !== Node.ELEMENT_NODE) {
              return '';
            }

            const tagName = node.tagName.toLowerCase();
            if (tagName === 'br') {
              return '\\n';
            }

            const content = Array.from(node.childNodes).map(renderInline).join('');
            if (!content.trim()) {
              return '';
            }

            if (tagName === 'strong' || tagName === 'b') {
              return `*${content}*`;
            }
            if (tagName === 'em' || tagName === 'i') {
              return `_${content}_`;
            }
            if (tagName === 'u') {
              return `__${content}__`;
            }
            if (tagName === 's' || tagName === 'strike' || tagName === 'del') {
              return `~${content}~`;
            }
            if (tagName === 'code') {
              const codeText = String(node.innerText || node.textContent || '')
                .replace(/[\\\\`]/g, '\\\\$&');
              return `\\`${codeText}\\``;
            }
            if (tagName === 'a') {
              const href = node.getAttribute('href');
              if (!href) {
                return content;
              }
              return `[${content}](${escapeLinkUrl(node.href || href)})`;
            }
            return content;
          };

          const renderBlockquote = (element) => {
            const lines = String(element.innerText || element.textContent || '')
              .replace(/\\u00a0/g, ' ')
              .split(/\\n+/)
              .map((line) => normalizeText(line))
              .filter(Boolean);
            return lines.map((line) => `>${escapeMarkdown(line)}`).join('\\n');
          };

          const renderCallout = (element) => {
            const content = element.querySelector('[data-node-view-content]') || element;
            const lines = String(content.innerText || content.textContent || '')
              .replace(/\\u00a0/g, ' ')
              .split(/\\n+/)
              .map((line) => normalizeText(line))
              .filter(Boolean);
            return lines.map((line) => `>${escapeMarkdown(line)}`).join('\\n');
          };

          const renderListItem = (element, prefix) => {
            const parts = Array.from(element.children)
              .map((child) => renderBlock(child))
              .filter(Boolean);
            const body = parts.length ? parts.join('\\n') : renderPlain(element);
            if (!body) {
              return '';
            }
            const lines = body.split('\\n');
            return [prefix + lines[0], ...lines.slice(1)].join('\\n');
          };

          const renderList = (element) => {
            const ordered = element.tagName.toLowerCase() === 'ol';
            return Array.from(element.children)
              .filter((child) => child.tagName && child.tagName.toLowerCase() === 'li')
              .map((child, index) => {
                const prefix = ordered ? `${index + 1}\\\\. ` : '\\\\- ';
                return renderListItem(child, prefix);
              })
              .filter(Boolean)
              .join('\\n');
          };

          const renderBlock = (element) => {
            if (!element || !element.tagName) {
              return '';
            }

            const className = element.className || '';
            if (typeof className === 'string' && className.includes('node-imageBlock')) {
              return '';
            }
            if (typeof className === 'string' && className.includes('node-callout')) {
              return renderCallout(element);
            }
            if (element.dataset && element.dataset.type === 'horizontalRule') {
              return '────────';
            }

            const tagName = element.tagName.toLowerCase();
            if (tagName === 'h1' || tagName === 'h2' || tagName === 'h3') {
              const title = renderPlain(element);
              return title ? `_*${title}*_` : '';
            }
            if (tagName === 'p') {
              return renderInline(element).replace(/[ \\t]+\\n/g, '\\n').trim();
            }
            if (tagName === 'blockquote') {
              return renderBlockquote(element);
            }
            if (tagName === 'hr') {
              return '────────';
            }
            if (tagName === 'ol' || tagName === 'ul') {
              return renderList(element);
            }
            if (tagName === 'pre') {
              const code = String(element.innerText || element.textContent || '')
                .replace(/[\\\\`]/g, '\\\\$&')
                .trim();
              return code ? `\\`\\`\\`\\n${code}\\n\\`\\`\\`` : '';
            }

            const childBlocks = Array.from(element.children)
              .map((child) => renderBlock(child))
              .filter(Boolean);
            if (childBlocks.length) {
              return childBlocks.join('\\n');
            }
            return renderInline(element).trim();
          };

          const blocks = [];
          let hasStarted = false;
          const children = Array.from(editor.children);
          const hasMeaningfulContentAfter = (index) => {
            for (const child of children.slice(index + 1)) {
              const className = child.className || '';
              if (typeof className === 'string' && className.includes('node-imageBlock')) {
                continue;
              }
              if (child.dataset && child.dataset.type === 'horizontalRule') {
                continue;
              }
              const text = normalizeText(child.innerText || child.textContent || '');
              if (text) {
                return true;
              }
            }
            return false;
          };

          for (const [index, child] of children.entries()) {
            const className = child.className || '';
            if (typeof className === 'string' && className.includes('node-callout')) {
              if (hasStarted && !hasMeaningfulContentAfter(index)) {
                const calloutText = normalizeText(child.innerText || child.textContent || '');
                if (!calloutText || calloutText.includes('투자 유의사항') || calloutText.includes('유사투자자문')) {
                  break;
                }
              }
            }
            if (typeof className === 'string' && className.includes('node-imageBlock')) {
              continue;
            }

            const block = renderBlock(child);
            if (!block) continue;

            hasStarted = true;
            blocks.push(block);
          }
          while (blocks.length > 0 && blocks[blocks.length - 1] === '────────') {
            blocks.pop();
          }
          return blocks.join('\\n\\n').trim();
        }
        """
    )
    if body_markdown:
        return str(body_markdown).strip()

    return _escape_markdown_v2(_extract_post_body_text_fallback(page))


def _extract_post_body_text_fallback(page: Page) -> str:
    raw_text = page.locator("body").inner_text(timeout=10000)
    lines = [_normalize_text(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]

    start_index = 0
    for index, line in enumerate(lines):
        if line == "Beta":
            start_index = index + 1
            break

    for index in range(start_index, len(lines)):
        if _is_script_preparing_marker(lines[index]):
            start_index = index + 1
            break

    end_index = len(lines)
    for index in range(start_index, len(lines)):
        if lines[index] in {"투자 유의사항 펼치기", "유사투자자문 고지 안내"}:
            end_index = index
            break

    return "\n".join(lines[start_index:end_index]).strip()


def _filename_from_headers(headers: dict[str, str]) -> str | None:
    disposition = headers.get("content-disposition", "")
    match = re.search(r'filename\*=UTF-8\'\'([^;]+)', disposition, flags=re.I)
    if match:
        return _safe_filename(unquote(match.group(1)))

    match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.I)
    if match:
        return _safe_filename(unquote(match.group(1)))
    return None


def _filename_from_url(url: str) -> str | None:
    name = Path(urlparse(url).path).name
    return _safe_filename(unquote(name)) if name else None


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "audio"


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _escape_markdown_v2(text: str) -> str:
    special_chars = "\\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{char}" if char in special_chars else char for char in text)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _dedupe_pdf_items(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for value in values:
        pdf_url = value["url"]
        if pdf_url not in seen:
            seen.add(pdf_url)
            result.append(value)
    return result


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1

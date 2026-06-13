from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

import imageio_ffmpeg
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


@dataclass(frozen=True)
class PostRef:
    post_id: str
    title: str
    url: str


@dataclass(frozen=True)
class DownloadedAudio:
    path: Path
    body_text: str


class AudioNotAvailableYet(RuntimeError):
    """Raised when a post exists but its audio player/media URL is not available yet."""


class UsInsightClient:
    def __init__(self, config: SiteConfig) -> None:
        self.config = config
        self._playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    def __enter__(self) -> "UsInsightClient":
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=self.config.headless,
        )

        context_options = {
            "viewport": {"width": 1366, "height": 900},
            "accept_downloads": True,
        }
        if self.config.auth_state_path.exists():
            context_options["storage_state"] = str(self.config.auth_state_path)

        self.context = self.browser.new_context(**context_options)
        self._restore_session_storage()
        self.context.set_default_timeout(self.config.navigation_timeout_ms)
        return self

    def __exit__(self, *_exc: object) -> None:
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
                if self._playwright:
                    self._playwright.stop()

    def open_login_page(self) -> None:
        page = self._new_page()
        try:
            self._goto(page, self.config.feed_url)
            while True:
                print("브라우저에서 네이버 로그인을 완료한 뒤, 이 터미널에서 Enter를 누르세요.")
                input()
                self._wait_for_auth_redirects(page)
                verified, verify_url = self._verify_feed_access(save_state=True)
                if verified:
                    print("로그인 세션이 저장되었습니다.")
                    return
                print(f"아직 로그인된 피드가 아닙니다. 확인 URL: {verify_url}")
                print("네이버 로그인, 동의, 회원 연결을 끝까지 완료한 뒤 다시 Enter를 누르세요.")
        finally:
            page.close()

    def find_posts(self, title_contains: str, max_posts: int) -> list[PostRef]:
        page = self._new_page()
        try:
            LOGGER.info("Opening feed: %s", self.config.feed_url)
            self._goto(page, self.config.feed_url)
            self._wait_for_network_idle(page)
            self._wait_for_page_settle(page)
            if self._is_logged_out(page):
                raise RuntimeError(
                    "US Insight is not available as a logged-in feed page. "
                    "This may mean the session expired, the sign-in page is showing, "
                    "or the page could not be inspected during a temporary load/render issue. "
                    "Run `python -m daily_us check-login` first; if it fails consistently, "
                    "run `python -m daily_us login` and complete Naver login before polling."
                )
            posts = self._extract_post_links(page, title_contains, max_posts)
            LOGGER.info("Found %s candidate posts for title filter %r", len(posts), title_contains)
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
            body_text = _extract_post_body_text(page) or post.title
            return DownloadedAudio(path=audio_path, body_text=body_text)
        finally:
            page.close()

    def fetch_post_body_text(self, post: PostRef) -> str:
        page = self._new_page()
        try:
            LOGGER.info("Opening post for body text: %s", post.url)
            self._goto(page, post.url)
            self._wait_for_network_idle(page)
            self._wait_for_page_settle(page)
            return _extract_post_body_text(page) or post.title
        finally:
            page.close()

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

    def _new_page(self) -> Page:
        if not self.context:
            raise RuntimeError("Browser context is not open.")
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

    def _wait_for_auth_redirects(self, page: Page) -> None:
        for _ in range(30):
            parsed_url = urlparse(page.url)
            if parsed_url.netloc not in {"nid.naver.com", "api.us-insight.com"}:
                return
            page.wait_for_timeout(1000)

    def _verify_feed_access(self, save_state: bool = False) -> tuple[bool, str]:
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

    def _save_auth_state(self, page: Page) -> None:
        if not self.context:
            raise RuntimeError("Browser context is not open.")

        self.config.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.config.auth_state_path))

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
        self.config.session_storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.session_storage_path.write_text(
            json.dumps({origin: session_storage}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _restore_session_storage(self) -> None:
        if not self.context or not self.config.session_storage_path.exists():
            return

        raw = self.config.session_storage_path.read_text(encoding="utf-8")
        storage_by_origin = json.loads(raw)
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
        self.context.add_init_script(script=script)


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


def _extract_post_body_text(page: Page) -> str:
    lines = page.evaluate(
        """
        () => {
          const editor = document.querySelector('.tiptap.ProseMirror');
          if (!editor) return [];

          const lines = [];
          let hasStarted = false;
          for (const child of Array.from(editor.children)) {
            const className = child.className || '';
            if (typeof className === 'string' && className.includes('node-callout')) {
              if (hasStarted) break;
              continue;
            }
            if (typeof className === 'string' && className.includes('node-imageBlock')) {
              continue;
            }

            const text = (child.innerText || child.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!text) continue;

            hasStarted = true;
            lines.push(text);
          }
          return lines;
        }
        """
    )
    if lines:
        return "\n".join(_normalize_text(line) for line in lines if line).strip()

    return _extract_post_body_text_fallback(page)


def _extract_post_body_text_fallback(page: Page) -> str:
    raw_text = page.locator("body").inner_text(timeout=10000)
    lines = [_normalize_text(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]

    start_index = 0
    for index, line in enumerate(lines):
        if line == "Beta" or "스크립트 준비중" in line:
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


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
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

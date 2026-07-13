from __future__ import annotations

import json
import logging
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
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


@dataclass(frozen=True)
class PostRef:
    post_id: str
    title: str
    url: str


@dataclass(frozen=True)
class DownloadedAudio:
    path: Path
    body_text: str


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

    def __enter__(self) -> "UsInsightClient":
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=self.config.headless,
        )

        context_options = {
            "viewport": {"width": 1366, "height": 900},
            "accept_downloads": True,
        }
        auth_state = self._load_saved_auth_state()
        if auth_state is not None:
            context_options["storage_state"] = auth_state

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
            body_text = _extract_post_body_text(page) or _escape_markdown_v2(post.title)
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
            return _extract_post_body_text(page) or _escape_markdown_v2(post.title)
        finally:
            page.close()

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
        try:
            self._save_auth_state(page)
            LOGGER.info("Refreshed saved login session state.")
        except Exception:
            LOGGER.exception("Could not refresh the saved login session state.")
            return
        self._log_session_expiry()

    def _log_session_expiry(self) -> None:
        if not self.context:
            return

        try:
            cookies = {
                cookie["name"]: cookie
                for cookie in self.context.cookies([self.config.feed_url])
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
        if not self.context:
            raise RuntimeError("Browser context is not open.")

        state = self.context.storage_state()
        _write_json_atomically(self.config.auth_state_path, state)

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

    def _restore_session_storage(self) -> None:
        if not self.context or not self.config.session_storage_path.exists():
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
        self.context.add_init_script(script=script)


def _write_json_atomically(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


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

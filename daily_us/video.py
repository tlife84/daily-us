from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urljoin, urlparse

import imageio_ffmpeg
import requests

from daily_us.config import KST


class VideoNotAvailableYet(RuntimeError):
    """게시글 또는 영상 변환이 아직 준비되지 않은 경우 다음 폴링으로 연기."""


@dataclass(frozen=True)
class PostVideo:
    """API에서 확인한 실제 게시일과 인증된 영상 정보. 서명 쿠키는 출력에서 제외."""

    published_at: datetime
    media_url: str = field(repr=False)
    cookie: str = field(repr=False)
    referer: str

    @property
    def filename(self) -> str:
        """한국 시간 게시일의 하루 전 날짜를 MP4 파일명으로 반환."""
        lesson_date = self.published_at.astimezone(KST).date() - timedelta(days=1)
        return f"{lesson_date.isoformat()}.mp4"


def video_from_payload(payload: dict, post_url: str) -> PostVideo | None:
    """권한이 있는 본편 영상에서 실제 게시일과 다운로드 정보 추출.

    Args:
        payload: 해당 게시글의 /v2/contents/secret API 응답.
        post_url: 영상 요청의 Referer로 사용할 게시글 주소.

    Returns:
        본편 영상 정보. 글 또는 미리보기인 경우 None.
    """
    content = payload.get("content")
    if not isinstance(content, dict):
        raise VideoNotAvailableYet("Post content is not available yet")
    if content.get("isPreview") or "미리보기" in str(content.get("title", "")):
        return None
    if content.get("mediaType") != "VIDEO":
        return None
    if content.get("hasAuthority") is not True:
        raise RuntimeError("정규수업 영상 열람 권한을 확인할 수 없습니다.")
    # 게시일이 없거나 시간대가 불명확하면 파일명·삭제 날짜를 추정하지 않고 중단
    published_at = datetime.fromisoformat(str(content.get("publishedAt", "")).replace("Z", "+00:00"))
    if published_at.tzinfo is None:
        raise ValueError("Video publishedAt must include a timezone")
    media_url = content.get("mediaConvertUrl")
    if not media_url:
        raise VideoNotAvailableYet("Video conversion is not available yet")
    parsed = urlparse(str(media_url))
    if parsed.scheme != "https" or parsed.netloc != "video.us-insight.com":
        raise ValueError("Video URL must use the US Insight HTTPS video host")
    if not parsed.path.lower().endswith((".m3u8", ".mp4")):
        raise ValueError("Unsupported video URL format")
    return PostVideo(published_at, str(media_url), str(content.get("cookie") or ""), post_url)


def _read_playlist(url: str, video: PostVideo, cookies: SimpleCookie) -> list[str]:
    """동일 CDN의 HLS 목록을 리다이렉트 없이 읽고 인증 정보가 포함된 오류 숨김.

    Args:
        url: 읽을 재생 목록 주소.
        video: 사이트 API가 제공한 재생 정보.
        cookies: 미디어 CDN 접근용 서명 쿠키.

    Returns:
        재생 목록의 행 목록.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "video.us-insight.com":
        raise ValueError("Video URL must use the US Insight HTTPS video host")
    try:
        # 리다이렉트로 다른 호스트에 서명 쿠키가 전달되지 않도록 직접 응답만 허용
        with requests.get(
            url, headers={"Referer": video.referer}, timeout=30, allow_redirects=False,
            cookies={key: value.value for key, value in cookies.items() if key in {
                "CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id",
            }},
        ) as response:
            if response.status_code != 200:
                raise RuntimeError("Video playlist request failed")
            return response.text.splitlines()
    except requests.RequestException:
        raise RuntimeError("Video playlist request failed") from None


def _select_1080p_url(video: PostVideo, cookies: SimpleCookie) -> tuple[str, float | None]:
    """1080p 재생 주소와 원본 길이 선택. 미제공·변환 중이면 다음 폴링으로 연기.

    Args:
        video: 사이트 API가 제공한 재생 정보.
        cookies: 미디어 CDN 접근용 서명 쿠키.

    Returns:
        재생 주소와 HLS 전체 길이(초). 직접 MP4 주소의 길이는 None.
    """
    parsed = urlparse(video.media_url)
    if parsed.scheme != "https" or parsed.netloc != "video.us-insight.com":
        raise ValueError("Video URL must use the US Insight HTTPS video host")
    if parsed.path.lower().endswith(".mp4"):
        return video.media_url, None
    lines = _read_playlist(video.media_url, video, cookies)
    for index, line in enumerate(lines[:-1]):
        if line.startswith("#EXT-X-STREAM-INF:") and re.search(r"(?:[:,])RESOLUTION=1920x1080(?:,|$)", line):
            selected = urljoin(video.media_url, lines[index + 1].strip())
            parsed = urlparse(selected)
            if parsed.scheme != "https" or parsed.netloc != "video.us-insight.com" or not parsed.path.lower().endswith(".m3u8"):
                raise ValueError("1080p playlist must use the US Insight HTTPS video host")
            segments = _read_playlist(selected, video, cookies)
            duration = sum(float(line.split(":", 1)[1].split(",")[0]) for line in segments if line.startswith("#EXTINF:"))
            if "#EXT-X-ENDLIST" not in segments or not math.isfinite(duration) or duration <= 0:
                raise VideoNotAvailableYet("1080p 재생 목록이 아직 완성되지 않았습니다.")
            return selected, duration
    raise VideoNotAvailableYet("1080p 영상이 아직 제공되지 않습니다.")


def _is_1080p(path: Path, duration: float | None = None) -> bool:
    """MP4 해상도와 원본 길이를 검사하여 저화질·부분 영상 업로드 방지.

    Args:
        path: 검사할 로컬 영상 경로.
        duration: HLS 원본 길이(초). 지정 시 분할 경계 오차 2초 이내인지 검사.

    Returns:
        정상적으로 읽을 수 있는 1080p 영상인지 여부.
    """
    frames = imageio_ffmpeg.read_frames(str(path))
    try:
        metadata = next(frames)
        return metadata["size"] == (1920, 1080) and (duration is None or abs(metadata["duration"] - duration) <= 2)
    except (OSError, RuntimeError, StopIteration):
        return False
    finally:
        frames.close()


def download_video(video: PostVideo, directory: Path) -> Path:
    """인증된 1080p HLS·MP4 영상을 재인코딩 없이 저장하고 완료 파일만 재사용.

    Args:
        video: 게시일과 사이트가 발급한 미디어 접근 정보.
        directory: 게시글별 다운로드 디렉터리.

    Returns:
        완전히 다운로드한 yyyy-mm-dd.mp4 경로.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / video.filename
    if target.is_file() and target.stat().st_size > 0 and _is_1080p(target):
        return target
    partial = target.with_suffix(".part.mp4")
    # CloudFront 미디어 쿠키만 CDN 도메인에 전달. 사이트 로그인 쿠키나 다른 속성은 전달하지 않음
    cookies = SimpleCookie()
    cookies.load(video.cookie)
    media_url, duration = _select_1080p_url(video, cookies)
    cookie_header = "".join(
        f"{key}={morsel.value}; path=/; domain=video.us-insight.com;\n"
        for key, morsel in cookies.items()
        if key in {"CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id"}
        and not any(char in morsel.value for char in "\r\n;")
    )
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rw_timeout", "120000000", "-protocol_whitelist", "https,tls,tcp,crypto",
        "-referer", video.referer,
    ]
    if cookie_header:
        command.extend(["-cookies", cookie_header])
    # 1080p 재생 목록의 영상·오디오만 재인코딩 없이 저장
    command.extend([
        "-i", media_url, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-movflags", "+faststart", str(partial),
    ])
    try:
        # 한계: 프로세스가 종료된 다운로드는 처음부터 재시도. 필요 시 HLS 세그먼트별 캐시로 확장 가능
        try:
            result = subprocess.run(command, capture_output=True, timeout=3600)
        except subprocess.TimeoutExpired:
            # TimeoutExpired의 기본 문자열에 포함되는 명령 인수·서명 쿠키 노출 방지
            raise RuntimeError("Video download exceeded the one-hour time limit") from None
        if result.returncode or not partial.is_file() or partial.stat().st_size == 0:
            # ffmpeg 원문에는 서명된 주소가 포함될 수 있으므로 종료 코드만 노출
            raise RuntimeError(f"Video download failed (ffmpeg exit={result.returncode})")
        # FFmpeg의 중복 타임스탬프 보정을 허용하되 전체 길이까지 확인한 파일만 완료 처리
        if not _is_1080p(partial, duration):
            raise RuntimeError("다운로드한 영상의 해상도 또는 전체 길이가 원본과 다릅니다.")
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)

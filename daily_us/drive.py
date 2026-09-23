from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload, build_http

from daily_us.config import DriveConfig

LOGGER = logging.getLogger(__name__)
# 수동으로 올린 전 주 파일도 삭제해야 하므로 앱 생성 파일에만 한정되는 drive.file 범위는 사용 불가
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
VIDEO_FIELDS = "id,name,mimeType,size,parents,trashed,webViewLink"


def _save_credentials(path: Path, credentials: Credentials) -> None:
    """OAuth 토큰을 사용자 전용 임시 파일에 쓴 뒤 원자적으로 교체.

    Args:
        path: Git에서 제외되는 인증 파일 경로.
        credentials: 새로 발급하거나 갱신한 사용자 인증 정보.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".drive-token-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def login_drive(config: DriveConfig) -> None:
    """브라우저에서 최초 사용자 동의를 받고 예약 실행용 갱신 토큰 저장.

    Args:
        config: Google Cloud 데스크톱 앱 JSON 및 토큰 저장 경로.
    """
    if not config.client_secret_path.is_file():
        raise RuntimeError(
            f"Google Cloud 데스크톱 앱 OAuth JSON을 {config.client_secret_path}에 저장하세요. "
            "설정 방법: GUIDE.md의 정규수업 동영상 항목"
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.client_secret_path), DRIVE_SCOPES, autogenerate_code_verifier=True,
    )
    # 임의 로컬 포트로 인증 결과를 받는 실행 방식에는 데스크톱 앱 클라이언트만 허용
    if flow.client_type != "installed":
        raise RuntimeError(
            "데스크톱 앱용 OAuth JSON이 필요합니다. Google Auth Platform → 클라이언트에서 "
            "유형을 '데스크톱 앱'으로 새로 만들고 google_client_secret.json을 교체하세요."
        )
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, timeout_seconds=300, prompt="consent", access_type="offline",
        authorization_prompt_message="Google Drive 인증을 진행하세요: {url}",
        success_message="Google Drive 인증이 완료되었습니다. 이 창을 닫아도 됩니다.",
    )
    if not credentials.refresh_token:
        raise RuntimeError("Drive 갱신 토큰이 없습니다. drive-login을 다시 실행하세요.")
    _save_credentials(config.token_path, credentials)
    with DriveClient(config) as drive:
        drive.check_folder()


class DriveClient:
    """지정 폴더의 주차별 영상 조회·삭제·분할 업로드를 담당하는 클라이언트."""

    def __init__(self, config: DriveConfig) -> None:
        """저장한 개인 OAuth 토큰으로 Drive 클라이언트 구성.

        Args:
            config: 업로드 대상 폴더와 인증 파일 경로.
        """
        self.config = config
        if not config.token_path.is_file():
            raise RuntimeError("Drive 인증이 필요합니다. `python -m daily_us drive-login`을 실행하세요.")
        credentials = Credentials.from_authorized_user_file(str(config.token_path), DRIVE_SCOPES)
        if not credentials.valid:
            credentials.refresh(Request())
            _save_credentials(config.token_path, credentials)
        # 공식 전송 객체로 308 응답을 분할 업로드 진행 상태로 처리하고 요청별 제한 시간 적용
        http = build_http()
        http.timeout = 120
        self.service = build(
            "drive", "v3", http=AuthorizedHttp(credentials, http=http),
            cache_discovery=False,
        )

    def __enter__(self) -> DriveClient:
        """업로드 작업에서 클라이언트 컨텍스트 반환."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """인증된 HTTP 연결 종료."""
        self.service.close()

    def check_folder(self) -> None:
        """삭제·다운로드에 앞서 지정 폴더와 업로드 권한 확인."""
        folder = self.service.files().get(
            fileId=self.config.folder_id, supportsAllDrives=True,
            fields="id,mimeType,trashed,capabilities(canAddChildren)",
        ).execute(num_retries=3)
        if (folder.get("mimeType") != "application/vnd.google-apps.folder"
                or folder.get("trashed") or not folder.get("capabilities", {}).get("canAddChildren")):
            raise RuntimeError("지정 Drive 폴더에 업로드할 수 없습니다. 로그인 계정과 폴더 권한을 확인하세요.")

    def find_videos(self, filename: str) -> list[dict]:
        """지정 폴더 바로 아래의 정확한 날짜 파일명과 MP4 형식으로 조회.

        Args:
            filename: yyyy-mm-dd.mp4 형식의 조회 대상.

        Returns:
            이름과 형식이 일치하는 휴지통 밖의 파일 목록.
        """
        # 파일명 검증을 삭제 경계에도 적용하여 임의 검색식·다른 종류 파일 접근 방지
        _lesson_date(filename)
        query = (
            f"'{self.config.folder_id}' in parents and name = '{filename}' "
            "and mimeType = 'video/mp4' and trashed = false"
        )
        result: list[dict] = []
        page_token = None
        while True:
            response = self.service.files().list(
                q=query, fields=f"nextPageToken,files({VIDEO_FIELDS})", pageSize=100,
                pageToken=page_token, supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute(num_retries=3)
            result.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return result

    def get_video(self, file_id: str, filename: str) -> dict | None:
        """사전 저장한 ID로 업로드 완료 여부를 확인하여 응답 유실 시 중복 생성 방지.

        Args:
            file_id: DB에 저장한 Drive 파일 ID.
            filename: 해당 영상의 예상 파일명.

        Returns:
            완료된 영상 정보. 아직 존재하지 않으면 None.
        """
        try:
            item = self.service.files().get(
                fileId=file_id, fields=VIDEO_FIELDS, supportsAllDrives=True,
            ).execute(num_retries=3)
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise
        if (item.get("name") != filename or item.get("mimeType") != "video/mp4"
                or item.get("trashed") or self.config.folder_id not in item.get("parents", [])
                or int(item.get("size", 0)) <= 0):
            raise RuntimeError("저장한 Drive 파일의 이름·폴더·완료 상태가 예상과 다릅니다.")
        return item

    def generate_id(self) -> str:
        """업로드 전에 영속 저장할 파일 ID 발급."""
        return self.service.files().generateIds(count=1, space="drive").execute(num_retries=3)["ids"][0]

    def delete_previous_week(self, filename: str) -> None:
        """정확히 전 주 날짜의 MP4만 영구 삭제하여 저장 공간 확보.

        Args:
            filename: 새로 업로드할 영상 파일명. 이 날짜에서 7일 전만 삭제.
        """
        previous = _lesson_date(filename) - timedelta(days=7)
        for candidate in self.find_videos(f"{previous.isoformat()}.mp4"):
            # 조회 이후 이동·이름 변경 여부를 재확인한 뒤 해당 파일만 삭제
            item = self.get_video(candidate["id"], f"{previous.isoformat()}.mp4")
            if item is None:
                continue
            LOGGER.info("Deleting previous week's video: %s (%s)", item["name"], item["id"])
            try:
                self.service.files().delete(fileId=item["id"], supportsAllDrives=True).execute(num_retries=3)
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise

    def upload_video(self, path: Path, file_id: str) -> dict:
        """고정 ID로 영상을 분할 업로드하고 완료된 원격 파일의 크기 확인.

        Args:
            path: 완료된 로컬 MP4 경로.
            file_id: 재시도에서도 유지할 사전 발급 파일 ID.

        Returns:
            업로드 완료된 파일 메타데이터.
        """
        _lesson_date(path.name)
        with path.open("rb") as handle:
            media = MediaIoBaseUpload(handle, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
            request = self.service.files().create(
                body={"id": file_id, "name": path.name, "parents": [self.config.folder_id]},
                media_body=media, fields=VIDEO_FIELDS, supportsAllDrives=True,
            )
            result = None
            try:
                # 한계: 프로세스 재시작 시 청크 전송은 처음부터 진행. 완료 파일은 고정 ID로 재사용하며, 필요 시 재개 URI 영속 저장으로 확장 가능
                while result is None:
                    # 네트워크 중단은 서버에 저장된 위치를 확인하여 청크 단위로 재전송
                    status, result = request.next_chunk(num_retries=3)
                    if status:
                        LOGGER.info("Video upload: %.0f%%", status.progress() * 100)
            except HttpError as exc:
                if exc.resp.status != 409:
                    raise
                # 완료 응답만 유실된 재시도는 같은 ID의 기존 파일 확인
                result = self.get_video(file_id, path.name)
        if not result or int(result.get("size", 0)) != path.stat().st_size:
            raise RuntimeError("Drive 업로드 완료 파일의 크기를 확인할 수 없습니다.")
        return result


def _lesson_date(filename: str) -> date:
    """삭제 대상 계산 전에 날짜 형식과 MP4 확장자를 엄격하게 검증.

    Args:
        filename: yyyy-mm-dd.mp4 형식의 파일명.

    Returns:
        파일명에 기록된 수업 날짜.
    """
    parsed = date.fromisoformat(filename.removesuffix(".mp4"))
    if filename != f"{parsed.isoformat()}.mp4":
        raise ValueError("Video filename must be yyyy-mm-dd.mp4")
    return parsed

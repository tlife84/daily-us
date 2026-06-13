# daily-us

US Insight 피드에서 특정 제목의 게시글을 확인하고, 게시글 안의 오디오 파일을 텔레그램 개인 채팅으로 보내는 자동화 도구입니다.

자세한 운영 방법은 [GUIDE.md](GUIDE.md)를 참고하세요.

## 설치

Python 3.11~3.13을 권장합니다. Python 3.14는 일부 Playwright 하위 패키지가 아직 미리 빌드된 휠을 제공하지 않으면 설치 중 C++ 빌드를 요구할 수 있습니다.

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 텔레그램 설정

`.env.example`을 참고해서 `.env` 파일을 만들고 값을 채웁니다.

```dotenv
TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
TELEGRAM_CHAT_ID=123456789
```

개인 채팅 `chat_id`는 봇에게 아무 메시지나 보낸 뒤 아래 명령으로 확인할 수 있습니다.

```bash
python -m daily_us telegram-updates
```

`TELEGRAM_CHAT_ID`는 봇 토큰 앞 숫자가 아니라, `telegram-updates`에서 `type=private`로 나온 `chat_id`여야 합니다.

또는 아래 주소를 브라우저에서 열어 확인할 수도 있습니다.

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

## 최초 로그인

네이버 아이디/비밀번호는 코드에 저장하지 않습니다. 처음 한 번만 브라우저에서 직접 로그인하면, 이후에는 `data/auth_state.json`과 `data/session_storage.json`에 저장된 인증 상태를 재사용합니다.

```bash
python -m daily_us login
```

브라우저에서 네이버 로그인을 완료한 다음 터미널에서 Enter를 누르세요. 로그인 검증이 성공해야 인증 상태 파일이 저장됩니다.

## 한 번만 테스트 실행

```bash
python -m daily_us poll
```

이 명령은 시간 조건을 무시하고 즉시 한 번 확인합니다. 다운로드한 파일은 `downloads/`에 저장되고, 성공적으로 텔레그램에 보낸 글은 `data/seen.sqlite3`에 기록되어 중복 전송되지 않습니다.

DB의 전송 이력을 무시하고 최신 글 1개만 반복 테스트하려면:

```bash
python -m daily_us test-latest --watcher good_morning_damsaem
```

이 명령은 `data/seen.sqlite3`를 읽거나 쓰지 않습니다.

mp3 없이 본문 메시지만 테스트하려면:

```bash
python -m daily_us test-latest-body --watcher good_morning_damsaem
```

## 계속 실행

```bash
python -m daily_us run
```

기본 설정은 `config.yaml`에 있습니다.

```yaml
watchers:
  - name: "good_morning_damsaem"
    title_contains: "굿모닝 담샘"
    active_hours: ["07:00", "09:10"]
    interval_minutes: 10
    max_posts_per_poll: 5
```

`굿모닝 담샘`은 오전 7시부터 9시 10분까지 10분 간격으로 확인합니다. 다른 게시글을 확장하려면 `watchers`에 항목을 추가하면 됩니다.

```yaml
  - name: "another_post"
    title_contains: "확인할 제목"
    interval_minutes: 30
    max_posts_per_poll: 5
```

## OS별 자동 실행

가장 단순한 방법은 컴퓨터가 켜져 있을 때 터미널에서 `python -m daily_us run`을 계속 띄워 두는 것입니다.

운영체제 스케줄러에 등록하려면:

- Windows: 작업 스케줄러에서 가상환경의 `python.exe`로 `-m daily_us run` 실행
- macOS: `launchd`에 같은 명령 등록

## 문제 확인

로그를 더 자세히 보려면:

```bash
python -m daily_us --log-level DEBUG poll
```

로그인 뒤 게시글 DOM이나 mp3 플레이어 구조가 다르면, DEBUG 로그를 보고 제목 링크 탐색 또는 플레이어 버튼 선택자를 조정하면 됩니다.

# daily-us 운영 가이드

US Insight의 `굿모닝 담쌤` 게시글을 확인하고, 오디오와 본문을 텔레그램 개인 채팅으로 보내는 방법을 정리한 가이드입니다.

## 1. 기본 준비

프로젝트 폴더:

```powershell
C:\Workspace\daily-us
```

Windows PowerShell:

```powershell
cd C:\Workspace\daily-us
.\.venv\Scripts\Activate.ps1
```

macOS:

```bash
cd /path/to/daily-us
source .venv/bin/activate
```

처음 설치가 필요하면:

Windows:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

`.env` 파일에는 텔레그램 값이 필요합니다.

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_CHAT_IDS=...
TELEGRAM_ADMIN_CHAT_ID=...
```

개인 채팅 ID 확인:

```bash
python -m daily_us telegram-updates
```

여러 사람에게 동시에 보내려면 `TELEGRAM_CHAT_IDS`에 쉼표로 구분해서 채팅 ID를 넣습니다. 이 값이 있으면 일반 게시글은 모든 채팅으로 전송되고, 없으면 기존 `TELEGRAM_CHAT_ID` 한 곳으로 전송됩니다.

`TELEGRAM_ADMIN_CHAT_ID`는 로그인 세션 만료 같은 운영 알림을 받을 개인 채팅 ID입니다. 비워 두면 `TELEGRAM_CHAT_ID`로 알림을 보냅니다.

## 2. 로그인

네이버/US Insight 로그인은 최초 1회 브라우저에서 직접 합니다.

Windows:

```powershell
.\.venv\Scripts\python.exe -m daily_us login
```

macOS:

```bash
.venv/bin/python -m daily_us login
```

브라우저가 열리면 네이버 로그인을 끝까지 완료한 뒤, 터미널에서 Enter를 누릅니다.

로그인 상태 확인:

Windows:

```powershell
.\.venv\Scripts\python.exe -m daily_us check-login
```

macOS:

```bash
.venv/bin/python -m daily_us check-login
```

정상 예:

```text
verified=True
url=https://us-insight.com/feed?type=all
```

## 3. 수동 폴링

운영용 1회 실행:

Windows:

```powershell
.\.venv\Scripts\python.exe -m daily_us poll
```

macOS:

```bash
.venv/bin/python -m daily_us poll
```

`poll`은 `seen.sqlite3`를 확인합니다. 이미 보낸 글은 다시 보내지 않고, 새로 보낸 글은 DB에 기록합니다.

특정 watcher만 실행:

Windows:

```powershell
.\.venv\Scripts\python.exe -m daily_us poll --watcher good_morning_damsaem
.\.venv\Scripts\python.exe -m daily_us poll --watcher always_date
.\.venv\Scripts\python.exe -m daily_us poll --watcher company_analysis_guide
```

macOS:

```bash
.venv/bin/python -m daily_us poll --watcher good_morning_damsaem
.venv/bin/python -m daily_us poll --watcher always_date
.venv/bin/python -m daily_us poll --watcher company_analysis_guide
```

주의: `poll`은 `config.yaml`의 `active_hours`를 무시하고 즉시 1회 실행합니다. 따라서 OS 스케줄러에서 실행 시간을 정확히 잡아야 합니다.

## 4. 테스트 명령

텔레그램 연결 테스트:

```bash
python -m daily_us test-telegram
```

텔레그램 MarkdownV2 렌더링 테스트:

```bash
python -m daily_us test-telegram-markdown
```

최신 글을 DB 기록 없이 반복 테스트:

```bash
python -m daily_us test-latest --watcher good_morning_damsaem
python -m daily_us test-latest --watcher good_morning_damsaem --limit 3
python -m daily_us test-latest --watcher company_analysis_guide
```

mp3 없이 본문만 테스트:

```bash
python -m daily_us test-latest-body --watcher good_morning_damsaem
python -m daily_us test-latest-body --watcher always_date
python -m daily_us test-latest-body --watcher always_date --limit 3
```

본문 테스트를 admin 채팅에만 보내기 (일반 수신자에게는 전송하지 않음):

```bash
python -m daily_us test-latest-body --watcher good_morning_damsaem --admin
```

`--admin`을 붙이면 `test-latest-body`가 본문을 `TELEGRAM_ADMIN_CHAT_ID`로만 보냅니다. 일반 수신자(`TELEGRAM_CHAT_ID`, `TELEGRAM_CHAT_IDS`)에게는 전송하지 않으므로, 실제 게시글 본문 렌더링을 다른 사람을 방해하지 않고 혼자 확인할 때 유용합니다.

`--limit`은 최근 매칭 게시글을 몇 개 보낼지 정합니다. 생략하면 1개만 보냅니다. `test-latest`와 `test-latest-body`는 `seen.sqlite3`를 읽거나 쓰지 않습니다.

본문 메시지는 텔레그램 `MarkdownV2` 형식으로 전송합니다. 본문 안의 일반 텍스트 특수문자는 텔레그램 문법 오류가 나지 않도록 자동으로 이스케이프합니다.

본문 HTML은 텔레그램 MarkdownV2로 변환됩니다. `h1`, `h2`, `h3`는 모두 기울임+굵게 제목으로 보내고, 굵게/기울임/밑줄/취소선/인용문/목록/링크/구분선은 텔레그램에서 보이는 문법으로 변환합니다.

## 5. 기존 글 완료 처리

스케줄러를 처음 켜기 전에 이미 올라와 있는 글을 보내고 싶지 않다면 `seed-seen`으로 DB에만 기록합니다. 텔레그램으로는 아무것도 보내지 않습니다.

Windows:

```powershell
.\.venv\Scripts\python.exe -m daily_us seed-seen --watcher always_date
.\.venv\Scripts\python.exe -m daily_us seed-seen --watcher good_morning_damsaem
```

macOS:

```bash
.venv/bin/python -m daily_us seed-seen --watcher always_date
.venv/bin/python -m daily_us seed-seen --watcher good_morning_damsaem
```

`--limit`을 생략하면 최근 100개까지 확인합니다. 이미 DB에 있는 글은 건너뜁니다.

`only_today: true`인 watcher는 오늘 날짜 게시글을 seed하지 않습니다. 예를 들어 `굿모닝 담쌤` 오늘 글이 이미 올라와 있어도 `seed-seen`은 그 글을 건너뛰고, 이전 날짜 글만 완료 처리합니다. 이렇게 해야 운영 폴링이 오늘 글을 정상 전송할 수 있습니다.

## 6. Windows 작업 스케줄러 등록

목표: `굿모닝 담쌤`은 매일 오전 7:00부터 9:10까지 10분마다 실행합니다.

### GUI로 등록

1. Windows 작업 스케줄러를 엽니다.
2. `작업 만들기`를 선택합니다.
3. `일반` 탭:
   - 이름: `daily-us good morning`
   - 사용자가 로그온되어 있든 아니든 실행을 선택할 수 있습니다.
4. `트리거` 탭:
   - 새로 만들기
   - 매일
   - 시작 시간: `07:00`
   - 반복 간격: `10분`
   - 반복 기간: `2시간 15분`
5. `동작` 탭:
   - 프로그램/스크립트:

```text
C:\Workspace\daily-us\.venv\Scripts\python.exe
```

   - 인수 추가:

```text
-m daily_us poll --watcher good_morning_damsaem
```

   - 시작 위치:

```text
C:\Workspace\daily-us
```

6. 저장 후 작업을 한 번 수동 실행해서 동작을 확인합니다.

### PowerShell로 등록

```powershell
$Action = New-ScheduledTaskAction `
  -Execute "C:\Workspace\daily-us\.venv\Scripts\python.exe" `
  -Argument "-m daily_us poll --watcher good_morning_damsaem" `
  -WorkingDirectory "C:\Workspace\daily-us"

$Trigger = New-ScheduledTaskTrigger `
  -Daily `
  -At 7:00AM

$Trigger.Repetition = New-ScheduledTaskRepetitionSettings `
  -Interval (New-TimeSpan -Minutes 10) `
  -Duration (New-TimeSpan -Hours 2 -Minutes 15)

Register-ScheduledTask `
  -TaskName "daily-us good morning" `
  -Action $Action `
  -Trigger $Trigger `
  -Description "Poll US Insight good morning post and send Telegram audio."
```

작업 수동 실행:

```powershell
Start-ScheduledTask -TaskName "daily-us good morning"
```

`언제나 데이트`는 매일 7:00부터 22:00까지 1시간마다 실행하는 작업을 따로 만듭니다. 동작의 인수는 아래처럼 지정합니다.

```text
-m daily_us poll --watcher always_date
```

`기업분석도감`은 일요일 12:00부터 20:00까지 1시간마다 실행하는 작업을 따로 만듭니다. 동작의 인수는 아래처럼 지정합니다.

```text
-m daily_us poll --watcher company_analysis_guide
```

## 7. macOS 스케줄러 등록

macOS에서는 `launchd` LaunchAgent를 권장합니다. `cron`은 잠자기 중 놓친 실행을 아예 건너뛰지만, launchd는 깨어날 때 놓친 시각들을 1회로 합쳐 즉시 실행해 줍니다(Windows의 StartWhenAvailable 대응).

### 스크립트로 설치 (권장)

레포에 포함된 스크립트가 `config.yaml`의 스케줄과 동일한 고정 시각 트리거로 LaunchAgent 3개를 생성하고 등록합니다. 레포를 클론하고 `.venv`를 만든 뒤 실행합니다.

```bash
bash scripts/macos/install-launch-agents.sh
```

- plist 생성 위치: `~/Library/LaunchAgents/com.daily-us.*.plist` (경로는 레포 위치에서 자동 계산)
- 실행 래퍼: [scripts/macos/poll-watcher.sh](scripts/macos/poll-watcher.sh) — 시작/종료 로그, 복귀 직후 네트워크 대기(최대 90초), 실행 시간 제한(굿모닝 9분, 나머지 50분)을 처리합니다.
- 로그: `logs/good-morning.log`, `logs/always-date.log`, `logs/company-analysis-guide.log` (launchd 자체 오류는 `logs/launchd-*.log`)

### 자동 코드 갱신 (git pull)

집에서 푸시한 코드를 회사 Mac이 자동으로 받도록, `poll-watcher.sh`가 매 폴링 전에 `git pull --ff-only`를 실행합니다.

- 같은 시각에 여러 watcher가 떠도 잠금으로 한 작업만 pull하고, 나머지는 기존 코드로 바로 실행합니다.
- pull이 실패하면(네트워크 없음, 브랜치 분기 등) 로그에 남기고 기존 코드로 폴링을 진행합니다.
- pull로 `requirements.txt`가 바뀌면 `pip install -r requirements.txt`와 `playwright install chromium`을 자동 실행합니다.
- pull로 `install-launch-agents.sh`나 `config.yaml`(스케줄)이 바뀌면 로그에 `NOTICE: schedule changed`를 남깁니다. 이때는 Mac에서 `bash scripts/macos/install-launch-agents.sh`를 한 번 다시 실행해 트리거를 갱신해야 합니다.

사전 조건: Mac에서 `git pull`이 인증 프롬프트 없이 동작해야 합니다(SSH 키 등록 또는 credential helper 저장). 클론 후 `git pull`을 한 번 수동 실행해서 비밀번호를 묻지 않는지 확인하세요. 또한 추적 파일을 로컬에서 수정하면 pull이 계속 실패하므로, Mac 쪽에서는 코드를 직접 고치지 않는 것을 전제로 합니다(`.env`, `data/`, `logs/`는 gitignore라 무관).

수동 실행 테스트:

```bash
launchctl kickstart "gui/$(id -u)/com.daily-us.always-date"
```

해제:

```bash
bash scripts/macos/uninstall-launch-agents.sh
```

### 잠자기 주의사항

launchd도 잠든 Mac을 깨우지는 못합니다. 뚜껑이 닫혀 완전히 잠들면 그 시각의 실행은 건너뛰고, 깨어날 때 놓친 실행이 1회 합쳐져 즉시 실행됩니다. 전원 어댑터 연결 + Power Nap 환경에서는 주기적인 dark wake 중에 실행되는 경우가 많아 실사용에서는 크게 밀리지 않습니다. 아침 첫 실행(07:00)을 정시에 보장하고 싶다면 깨우기를 예약할 수 있습니다.

```bash
# 매일 06:58에 깨우기 (관리자 권한 필요, 규칙 1개만 지원)
sudo pmset repeat wakeorpoweron MTWRFSU 06:58:00
```

시간별 정시 실행까지 전부 보장해야 한다면 전원 연결 시 잠자기를 끄는 편이 간단합니다: `sudo pmset -c sleep 0`

### cron 간단 버전

```bash
crontab -e
```

아래 내용을 추가합니다.

```cron
*/10 7-8 * * * cd /path/to/daily-us && /path/to/daily-us/.venv/bin/python -m daily_us poll --watcher good_morning_damsaem >> /path/to/daily-us/daily-us.log 2>&1
0,10 9 * * * cd /path/to/daily-us && /path/to/daily-us/.venv/bin/python -m daily_us poll --watcher good_morning_damsaem >> /path/to/daily-us/daily-us.log 2>&1
0 7-22 * * * cd /path/to/daily-us && /path/to/daily-us/.venv/bin/python -m daily_us poll --watcher always_date >> /path/to/daily-us/daily-us.log 2>&1
0 12-20 * * 0 cd /path/to/daily-us && /path/to/daily-us/.venv/bin/python -m daily_us poll --watcher company_analysis_guide >> /path/to/daily-us/daily-us.log 2>&1
```

이렇게 하면 `굿모닝 담쌤`은 7:00~8:50은 10분마다, 9:00과 9:10에도 실행되고, `언제나 데이트`는 7:00~22:00 정각마다 실행됩니다. `기업분석도감`은 일요일 12:00~20:00 정각마다 실행됩니다.

## 8. 상시 실행 모드

스케줄러 대신 프로세스를 계속 켜둘 수도 있습니다.

```bash
python -m daily_us run
```

`run`은 `config.yaml`의 `active_hours`와 `interval_minutes`를 사용합니다. 단, 터미널이나 프로세스가 꺼지면 멈춥니다.

## 9. 현재 watcher 설정

[config.yaml](config.yaml):

```yaml
watchers:
  - name: "good_morning_damsaem"
    title_contains: "굿모닝 담쌤"
    send_audio: true
    audio_filename_template: "굿모닝 담쌤 {mm-dd}"
    only_today: true
    active_hours: ["07:00", "09:10"]
    interval_minutes: 10
    max_posts_per_poll: 5

  - name: "always_date"
    title_contains: "언제나 데이트"
    title_exclude_contains: ["영상"]
    send_audio: false
    active_hours: ["07:00", "22:00"]
    interval_minutes: 60
    max_posts_per_poll: 5

  - name: "company_analysis_guide"
    title_contains: "기업분석도감"
    send_audio: false
    send_pdf: true
    active_days: ["sun"]
    active_hours: ["12:00", "20:00"]
    interval_minutes: 60
    max_posts_per_poll: 5
```

`only_today: true`는 제목의 `M월 D일`이 오늘 날짜인 게시글만 처리합니다. 이전 날짜 글은 본문이나 오디오의 전송 상태와 관계없이 완료 처리하여 더 이상 붙잡지 않습니다.

오디오 watcher는 본문과 오디오의 전송 상태를 각각 저장합니다. 본문이 먼저 올라오면 본문을 즉시 한 번 보내고, 오디오가 아직 없으면 다음 폴링부터 본문은 건너뛰고 오디오만 확인합니다. 빈 본문이나 `스크립트 준비중` 문구는 전송 완료로 기록하지 않으므로 실제 본문이 올라오면 다시 확인합니다. 본문 전송에 실패해도 준비된 오디오는 독립적으로 전송합니다.

당일 게시글은 본문과 오디오가 모두 전송되어야 `seen_posts`에 최종 완료 기록이 생깁니다. 단, 위의 `only_today` 규칙에 따라 날짜가 지난 게시글은 미완료 항목이 있더라도 강제로 완료 처리합니다.

`send_audio: false`는 미디어 다운로드를 시도하지 않고 본문만 텔레그램으로 보냅니다.

`send_pdf: true`는 게시글 API의 PDF 첨부를 다운로드해서 텔레그램 문서로 보냅니다.

`active_days`는 watcher가 실행될 요일을 제한합니다. `["sun"]`은 일요일만 실행한다는 뜻입니다.

`title_exclude_contains`는 제목/피드 카드 텍스트에 해당 키워드가 포함된 글을 제외합니다. `언제나 데이트` watcher는 영상 글을 제외하기 위해 `["영상"]`을 사용합니다.

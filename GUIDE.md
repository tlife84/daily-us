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
```

개인 채팅 ID 확인:

```bash
python -m daily_us telegram-updates
```

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
```

macOS:

```bash
.venv/bin/python -m daily_us poll --watcher good_morning_damsaem
.venv/bin/python -m daily_us poll --watcher always_date
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
```

mp3 없이 본문만 테스트:

```bash
python -m daily_us test-latest-body --watcher good_morning_damsaem
python -m daily_us test-latest-body --watcher always_date
python -m daily_us test-latest-body --watcher always_date --limit 3
```

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

## 7. macOS 스케줄러 등록

macOS에서는 `launchd`를 권장합니다. 간단히 쓰려면 `cron`도 가능합니다.

### launchd 권장

아래 파일을 만듭니다.

```bash
nano ~/Library/LaunchAgents/com.daily-us.goodmorning.plist
```

내용:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.daily-us.goodmorning</string>

  <key>WorkingDirectory</key>
  <string>/path/to/daily-us</string>

  <key>ProgramArguments</key>
  <array>
    <string>/path/to/daily-us/.venv/bin/python</string>
    <string>-m</string>
    <string>daily_us</string>
    <string>poll</string>
    <string>--watcher</string>
    <string>good_morning_damsaem</string>
  </array>

  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>40</integer></dict>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>40</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>10</integer></dict>
  </array>

  <key>StandardOutPath</key>
  <string>/path/to/daily-us/daily-us.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/daily-us/daily-us.err.log</string>
</dict>
</plist>
```

`/path/to/daily-us`는 실제 경로로 바꿉니다.

등록:

```bash
launchctl load ~/Library/LaunchAgents/com.daily-us.goodmorning.plist
```

수동 실행:

```bash
launchctl start com.daily-us.goodmorning
```

해제:

```bash
launchctl unload ~/Library/LaunchAgents/com.daily-us.goodmorning.plist
```

### cron 간단 버전

```bash
crontab -e
```

아래 내용을 추가합니다.

```cron
*/10 7-8 * * * cd /path/to/daily-us && /path/to/daily-us/.venv/bin/python -m daily_us poll --watcher good_morning_damsaem >> /path/to/daily-us/daily-us.log 2>&1
0,10 9 * * * cd /path/to/daily-us && /path/to/daily-us/.venv/bin/python -m daily_us poll --watcher good_morning_damsaem >> /path/to/daily-us/daily-us.log 2>&1
0 7-22 * * * cd /path/to/daily-us && /path/to/daily-us/.venv/bin/python -m daily_us poll --watcher always_date >> /path/to/daily-us/daily-us.log 2>&1
```

이렇게 하면 `굿모닝 담쌤`은 7:00~8:50은 10분마다, 9:00과 9:10에도 실행되고, `언제나 데이트`는 7:00~22:00 정각마다 실행됩니다.

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
```

`only_today: true`는 제목의 `M월 D일`이 오늘 날짜인 게시글만 처리합니다. 오늘 글이 발견되면 이전 날짜 글은 완료 처리하여 더 이상 붙잡지 않습니다.

`send_audio: false`는 미디어 다운로드를 시도하지 않고 본문만 텔레그램으로 보냅니다.

`title_exclude_contains`는 제목/피드 카드 텍스트에 해당 키워드가 포함된 글을 제외합니다. `언제나 데이트` watcher는 영상 글을 제외하기 위해 `["영상"]`을 사용합니다.

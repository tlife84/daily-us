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

브라우저가 열리면 네이버 로그인과 동의·회원 연결을 끝까지 완료합니다. 로그인 완료를 감지하면 세션을 자동으로 저장하므로 터미널에서 Enter를 누를 필요가 없습니다. `로그인 세션이 저장되었습니다` 안내와 함께 브라우저가 자동으로 닫힐 때까지 기다리세요.

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

`기업분석도감`은 일요일 12:00부터 20:00까지, 화요일 19:00부터 22:00까지 1시간마다 실행하는 작업을 따로 만듭니다. 동작의 인수는 아래처럼 지정합니다.

```text
-m daily_us poll --watcher company_analysis_guide
```

## 7. macOS 스케줄러 등록

macOS에서는 `launchd` LaunchAgent를 권장합니다. `cron`은 잠자기 중 놓친 실행을 아예 건너뛰지만, launchd는 깨어날 때 놓친 시각들을 1회로 합쳐 즉시 실행해 줍니다(Windows의 StartWhenAvailable 대응).

### 스크립트로 설치 (권장)

레포에 포함된 스크립트가 `config.yaml`의 스케줄과 동일한 고정 시각 트리거로 LaunchAgent 4개를 생성하고 등록합니다. 레포를 클론하고 `.venv`를 만든 뒤 실행합니다. 정규수업을 포함하려면 아래 10절의 Drive 인증도 먼저 완료하세요.

```bash
bash scripts/macos/install-launch-agents.sh
```

- plist 생성 위치: `~/Library/LaunchAgents/com.daily-us.*.plist` (경로는 레포 위치에서 자동 계산)
- 실행 래퍼: [scripts/macos/poll-watcher.sh](scripts/macos/poll-watcher.sh) — 시작/종료 로그, 복귀 직후 네트워크 대기(최대 90초), 실행 시간 제한(굿모닝 9분, 정규수업 2시간, 나머지 50분)을 처리합니다.
- 로그: `logs/good-morning.log`, `logs/always-date.log`, `logs/company-analysis-guide.log`, `logs/regular-class.log` (launchd 자체 오류는 `logs/launchd-*.log`)

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

이렇게 하면 `굿모닝 담쌤`은 7:00~8:50은 10분마다, 9:00과 9:10에도 실행되고, `언제나 데이트`는 7:00~22:00 정각마다 실행됩니다. `기업분석도감`은 일요일 12:00~20:00과 화요일 19:00~22:00 정각마다 실행됩니다.

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
    send_body_as_image: true
    audio_filename_template: "굿모닝 담쌤 {mm-dd}"
    only_today: true
    active_hours: ["07:00", "09:10"]
    interval_minutes: 10
    max_posts_per_poll: 5

  - name: "always_date"
    title_contains: "언제나 데이트"
    title_exclude_contains: ["영상"]
    send_audio: false
    send_body_as_image: true
    active_hours: ["07:00", "22:00"]
    interval_minutes: 60
    max_posts_per_poll: 5

  - name: "company_analysis_guide"
    title_contains: "기업분석도감"
    send_audio: false
    send_pdf: true
    schedules:
      - days: ["sun"]
        hours: ["12:00", "20:00"]
      - days: ["tue"]
        hours: ["19:00", "22:00"]
    interval_minutes: 60
    max_posts_per_poll: 5

  - name: "regular_class"
    title_contains: "정규수업"
    title_exclude_contains: ["미리보기"]
    send_audio: false
    send_video_to_drive: true
    only_today: true
    active_days: ["tue"]
    active_hours: ["20:05", "22:04"]
    interval_minutes: 5
    max_posts_per_poll: 5
```

`only_today: true`는 제목의 `M월 D일`이 오늘 날짜인 게시글만 처리합니다. 이전 날짜 글은 본문이나 오디오의 전송 상태와 관계없이 완료 처리하여 더 이상 붙잡지 않습니다. 정규수업 영상은 예외로 제목 대신 API의 `publishedAt`을 한국 시간으로 비교하며, 과거 글을 완료 처리하지 않고 건너뜁니다.

오디오 watcher가 `send_body_as_image: true`이면 본문은 오디오를 받은 페이지가 아니라 캡처 전용 페이지에서 따로 가져옵니다. 캡처에는 모바일 화면이 필요하기 때문입니다. 본문 사진을 먼저 보내고 오디오를 뒤이어 보내는 순서는 그대로입니다.

오디오 watcher는 본문과 오디오의 전송 상태를 각각 저장합니다. 본문이 먼저 올라오면 본문을 즉시 한 번 보내고, 오디오가 아직 없으면 다음 폴링부터 본문은 건너뛰고 오디오만 확인합니다. `스크립트 준비중`은 fallback 본문의 시작 구분자로 건너뛰며, 그 뒤에 실제 본문이 없을 때만 미준비로 판단하여 다음 폴링에서 다시 확인합니다. 본문 전송에 실패해도 준비된 오디오는 독립적으로 전송합니다.

오디오 watcher의 당일 게시글은 본문과 오디오가 모두 전송되어야 `seen_posts`에 최종 완료 기록이 생깁니다. 단, 위의 `only_today` 규칙에 따라 날짜가 지난 게시글은 미완료 항목이 있더라도 강제로 완료 처리합니다.

`send_audio: false`이고 `send_pdf`, `send_video_to_drive`도 꺼져 있으면 본문만 텔레그램으로 보냅니다.

`send_pdf: true`는 게시글 API의 PDF 첨부를 다운로드해서 텔레그램 문서로 보냅니다.

`send_body_as_image: true`는 본문을 텍스트 대신 화면 그대로 캡처한 사진으로 보냅니다. 이 사이트는 표와 그래프를 이미지로 넣기 때문에, 본문을 텍스트로 뽑으면 소제목만 남고 숫자가 통째로 빠집니다.

캡처는 사이트의 모바일 레이아웃(본문 폭 430px)으로 찍습니다. 데스크톱 레이아웃은 본문 폭이 660px라 같은 글이 폰에서 그만큼 작게 보입니다. 텔레그램이 사진의 긴 변을 2560px로 줄이므로, 블록 경계를 따라 그보다 짧게 잘라야 축소 없이 도착합니다. 이 과정에서 맨 위 표지 이미지와 화면에 고정된 배너, 헤더, 버튼을 함께 제거합니다. 굿모닝 담쌤의 `뉴스 브리핑` 섹션도 제외합니다. 이 섹션은 제목 띠 이미지로 시작하는데, 띠 이미지에 alt 같은 표시가 없어서 매일 같은 파일을 쓰는 이미지 주소로 찾습니다. 섹션의 끝은 기사 묶음이 끊기는 지점, 즉 기사가 아니면서 글자가 있는 첫 블록입니다. 뉴스 브리핑 뒤에 붙임자료가 이어지는 글이 있어서 글 끝까지 자르지 않습니다.

사진은 10장씩 앨범으로 묶어 보내고, 첫 앨범만 알림이 울립니다. 본문이 아직 준비되지 않은 글은 전송하지 않고 다음 폴링에서 다시 확인합니다.

`active_days`는 watcher가 실행될 요일을 제한합니다. `["sun"]`은 일요일만 실행한다는 뜻입니다.

요일마다 시간대가 다르면 `active_days`/`active_hours` 대신 `schedules`를 씁니다. 항목마다 `days`와 `hours`를 한 벌로 적고, 그중 하나라도 맞으면 실행합니다. `기업분석도감`처럼 일요일 낮과 화요일 저녁을 함께 쓰는 경우입니다. `schedules`와 `active_days`/`active_hours`를 한 watcher에 같이 쓰면 오류가 납니다.

`title_exclude_contains`는 제목/피드 카드 텍스트에 해당 키워드가 포함된 글을 제외합니다. `언제나 데이트` watcher는 영상 글을 제외하기 위해 `["영상"]`을 사용합니다.

## 10. 정규수업 동영상

`regular_class` watcher는 매주 화요일 20:05, 20:10, …, 22:00에 총 24회 확인합니다. 게시글 제목/피드 카드에 `정규수업`이 포함된 후보에서 미리보기와 안내 글을 제외하고, 당일 게시된 본편 영상만 처리합니다. 게시일은 API의 `publishedAt`을 한국 시간으로 바꿔 판별합니다.

영상은 **1080p(1920×1080)** 재생 목록을 선택해 재인코딩 없이 `downloads/regular-class/`의 게시글별 폴더에 MP4로 저장합니다. 1080p가 아직 제공되지 않으면 다음 폴링에서 재확인하며, 다운로드한 파일의 해상도와 HLS 원본 길이도 검사합니다. 파일명은 **게시일 하루 전 날짜**입니다. 예를 들어 2026-09-22에 게시된 영상은 `2026-09-21.mp4`가 됩니다.

다운로드가 완료되면 [지정 Drive 폴더](https://drive.google.com/drive/folders/1MkoUwi6JhIvt1NmAFXPfDjIDPqS6_ZEc) 바로 아래에서 `2026-09-14.mp4`처럼 정확히 7일 전 날짜의 MP4만 영구 삭제하고 새 영상을 업로드합니다. 다른 날짜의 파일이나 다른 폴더는 정리하지 않습니다. 휴지통의 파일도 용량을 차지하므로 공간 확보에는 영구 삭제가 필요합니다. [Google Drive 삭제 안내](https://support.google.com/drive/answer/14933051?hl=en)

업로드한 파일의 Drive 링크를 기존 `TELEGRAM_CHAT_IDS` 또는 `TELEGRAM_CHAT_ID` 수신자에게 보냅니다. 공유 권한은 대상 폴더에서 상속받습니다.

### 최초 Google 인증

Codex의 Google Drive 연결과 예약 실행하는 Python 프로그램의 인증은 별개입니다. Google 계정 비밀번호를 `.env`에 넣지 않습니다.

1. [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트를 만들거나 선택하고 **Google Drive API**를 사용 설정합니다.
2. **Google Auth Platform → 브랜딩**에서 앱 이름·지원 이메일·개발자 연락처를 설정합니다. 개인 Google 계정은 대상 유형을 **외부(External)**로 정하고, 테스트 중에는 자신의 계정을 테스트 사용자로 추가합니다. 프로덕션 전환을 위해 앱 소개 홈페이지와 개인정보처리방침의 실제 공개 URL, 해당 승인된 도메인도 등록합니다. 기존 공개 저장소의 GitHub Pages로 두 페이지를 제공할 수 있습니다. **앱 게시**가 비활성화되어 있으면 버튼에 마우스를 올려 누락된 항목을 확인합니다. [Google 브랜딩 설정 안내](https://support.google.com/cloud/answer/15549049?hl=en)
3. **데이터 액세스(Data Access)**에 `https://www.googleapis.com/auth/drive` 범위를 추가합니다. 기존에 직접 올린 전 주 파일도 삭제해야 하므로 앱이 만든 파일만 다루는 `drive.file` 범위로는 부족합니다. 실제 프로그램의 파일 조회·삭제 범위는 `config.yaml`의 폴더와 날짜 파일명으로 제한합니다.
4. **클라이언트(Clients) → 클라이언트 만들기 → 데스크톱 앱(Desktop app)**을 선택하고 JSON을 내려받아 `data/google_client_secret.json`으로 저장합니다. [Google의 데스크톱 앱 인증 정보 발급 안내](https://developers.google.com/workspace/guides/create-credentials#desktop-app)
5. 주간 무인 실행 전에는 대상 설정의 게시 상태를 **프로덕션(In production)**으로 전환합니다. 외부 앱을 Testing 상태로 두면 Drive 권한을 가진 갱신 토큰은 7일 뒤 만료됩니다. 전환 후 아래 `drive-login`을 실행해 다시 인증합니다. [Google OAuth 토큰 만료 안내](https://developers.google.com/identity/protocols/oauth2#expiration)
6. 예약 실행할 컴퓨터에서 가상환경을 활성화하고 다음 명령을 실행합니다. 폴더에 업로드하고 전 주 파일을 삭제할 수 있는 계정으로 브라우저 인증을 완료합니다.

게시 상태의 **프로덕션 전환**과 **브랜딩 검증**은 별개입니다. 본인 계정으로 사용하는 개인용 앱은 OAuth 검증을 완료하지 않아도 사용할 수 있습니다. 본인이 만든 앱인지 확인한 뒤 로그인 화면의 미확인 앱 경고에서 계속 진행합니다. GitHub Pages의 HTML 인증 파일만으로 Google Cloud의 도메인 검증까지 완료되는 것은 아니며, 개인용 자동화를 위해 별도 도메인을 구입하거나 브랜딩 심사를 진행할 필요는 없습니다. [Google 개인용 앱 검증 예외 안내](https://support.google.com/cloud/answer/13464323?hl=en)

```bash
pip install -r requirements.txt
python -m daily_us drive-login
python -m daily_us check-drive
```

`drive-login`은 브라우저 동의 후 로컬 콜백을 받아 `data/google_drive_token.json`을 저장합니다. 인증 창은 최대 5분 기다립니다. `check-drive`는 토큰과 폴더 업로드 권한만 확인하며 파일을 변경하지 않습니다. 두 JSON은 Git에서 제외되는 `data/`에 보관합니다. 토큰이 취소되거나 만료되어 자동 갱신이 실패하면 `drive-login`을 다시 실행합니다.

### 예약 작업 반영

운영체제 예약 시각은 컴퓨터의 로컬 시간대를 따르므로 macOS·Windows의 시간대를 **서울(Asia/Seoul)**로 설정합니다. `python -m daily_us run`의 정규수업 판별은 OS 시간대와 무관하게 한국 시간을 사용합니다.

macOS는 기존 설치 스크립트를 다시 실행하면 `com.daily-us.regular-class` 작업까지 등록됩니다.

```bash
bash scripts/macos/install-launch-agents.sh
```

Windows는 기존 작업들이 등록된 환경에서 관리자 PowerShell로 갱신 스크립트를 실행합니다. 새 `daily-us regular class` 작업은 기존 `daily-us company analysis guide`의 실행 계정을 사용합니다.

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\update-scheduled-tasks.ps1
```

두 운영체제의 정규수업 작업은 `poll --watcher regular_class --respect-schedule`을 실행합니다. 22:00 예약 실행의 시작 지연을 허용하기 위해 조회 가능 시간은 22:04분까지이며, 22:05 이후에는 놓친 조회를 시작하지 않습니다. 시간대 안에 시작한 다운로드·업로드는 종료 시각 이후에도 완료까지 진행하며, 같은 예약 작업의 중복 실행은 겹치지 않습니다. 영상 처리 중에는 새 폴링을 쌓지 않습니다.

수동으로 당일 영상을 확인하려면 다음 명령을 사용합니다. 이 명령은 실제로 전 주 파일을 삭제하고 업로드 및 봇 전송을 수행하므로 Google 인증 후 실행합니다.

```bash
python -m daily_us poll --watcher regular_class
```

`test-latest`는 이력을 무시하는 명령이라 동영상 업로드를 제외합니다. `seed-seen`도 정규수업은 제외하여 오늘 영상을 실수로 완료 처리하지 않습니다.

### 실패·재시도 동작

- 영상이 아직 변환 중이면 완료 처리하지 않고 다음 5분 폴링에서 다시 확인합니다.
- 다운로드 실패 시 전 주 영상은 삭제하지 않습니다. 삭제 또는 업로드 실패 시 완료된 로컬 MP4를 남겨 재사용합니다.
- Google 공식 라이브러리의 분할 업로드로 전송 중 네트워크 오류를 재시도합니다. 업로드 ID는 전송 전에 SQLite에 저장하므로 완료 응답 유실이나 프로세스 재시작 후에도 같은 파일을 확인합니다.
- 업로드 성공 후 로컬 MP4는 삭제합니다. 봇 전송만 실패하면 다음 폴링에서 같은 Drive 링크를 다시 보냅니다. 폴더에 동일 날짜의 MP4가 이미 하나 있으면 이를 재사용하며, 여러 개면 임의로 고르지 않고 오류를 알립니다.
- 한계: 프로세스가 종료되면 진행 중이던 다운로드와 미완료 업로드는 처음부터 시작합니다. 필요하면 HLS 세그먼트 캐시와 Drive 재개 URI 저장으로 확장할 수 있습니다. 봇의 여러 수신자 중 일부만 전송에 실패한 경우 다음 폴링에서 이미 받은 수신자에게 링크가 다시 갈 수 있습니다.
- 한계: 전 주 파일을 삭제한 뒤 업로드가 실패하면 Drive에서 전 주 영상을 되돌릴 수 없습니다. 새 영상의 로컬 파일은 보존합니다. 22:05 이후에는 자동 재시도를 시작하지 않으며, 그날이 지나면 `only_today`에 따라 과거 게시글을 건너뜁니다.

로컬 검증:

```bash
python -m unittest discover -s tests -p 'test_video_delivery.py'
```

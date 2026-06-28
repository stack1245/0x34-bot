# Team 0x34 Discord Bot

Team 0x34 운영을 위한 `discord.py` 기반 Discord 봇입니다. Slash Commands, Modals, Buttons, Embeds, SQLite를 사용하며 Railway 배포를 바로 시작할 수 있는 구조입니다.

## 디렉토리 구조

```text
0x34-bot/
├── bot.py
├── config.py
├── requirements.txt
├── Procfile
├── .env.example
├── .gitignore
├── cogs/
│   ├── __init__.py
│   ├── schedule.py
│   ├── tournament.py
│   ├── recruitment.py
│   └── maintenance.py
└── utils/
    ├── __init__.py
    ├── database.py
    ├── datetime.py
    └── embeds.py
```

## 빠른 시작

1. Discord Developer Portal에서 봇을 만들고 `applications.commands`와 `bot` 스코프로 서버에 초대합니다.
2. `.env.example`을 참고해 Railway Variables 또는 로컬 `.env`를 설정합니다.
3. 로컬 실행 시 아래 명령을 사용합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

## Railway 배포

Railway는 `Procfile`의 `worker: python bot.py`를 사용해 봇을 실행합니다. SQLite 파일을 장기간 보관해야 한다면 Railway Volume을 만들고 `DB_PATH`를 Volume 경로로 설정하세요. 예를 들어 `/app/data/0x34.sqlite3`처럼 지정하면 됩니다. 기존 `DATABASE_PATH`도 하위 호환으로 지원하지만, 둘 다 있으면 `DB_PATH`가 우선합니다. 운영 규모가 커지면 PostgreSQL로 교체하는 편이 안전합니다.

## Gemini 설정

`/모집생성`은 Google AI Studio에서 발급한 Gemini API 키가 필요합니다. 로컬 `.env` 또는 Railway Variables에 아래 값을 추가하세요.

```env
GEMINI_API_KEY=your-google-ai-studio-api-key
GEMINI_MODEL=gemini-2.5-flash
```

API 키는 코드나 Git에 커밋하지 말고 환경 변수로만 관리하세요.

## 주요 명령어

- `/일정`: 등록된 전체 일정을 Embed로 보여줍니다.
- `/일정추가`: Modal로 제목, 날짜/시간, 내용을 받아 일정을 저장하고 서버 이벤트 생성을 시도합니다.
- `/일정생성`: Gemini가 안내 텍스트에서 참가 신청, 예선, 본선 등 여러 일정을 JSON 배열로 추출해 일괄 등록합니다.
- `/대회등록`: Modal로 대회 정보를 받아 알림 채널에 Embed를 전송합니다. 민감 정보 메모는 작성자에게만 Ephemeral 응답으로 보여줍니다.
- `/모집`: Modal로 모집 글을 만들고 `[참가]`, `[불참]`, `[대기]`, `[모집 마감]` 버튼으로 실시간 참가자 목록을 관리합니다.
- `/모집생성`: Gemini가 링크나 상세 텍스트를 분석해 모집 Embed를 만들고, JSON 응답의 `max_members`로 정원을 자동 설정한 뒤 동일한 참가 버튼을 붙입니다.
- `/db정리`: 관리자 전용 명령어입니다. SQLite DB 파일을 먼저 Ephemeral 백업 파일로 전송한 뒤 삭제된 메시지/스레드/서버 이벤트를 가리키는 고아 데이터를 정리합니다.

유지보수 Cog는 `discord.ext.tasks` 백그라운드 루프로 하루에 한 번 과거 일정도 자동 삭제합니다. 날짜 파싱에 실패한 일정은 안전하게 건너뜁니다.

`/모집생성`에 URL을 입력하면 봇이 `aiohttp`와 BeautifulSoup으로 웹페이지 텍스트를 먼저 추출한 뒤 Gemini에 전달합니다. 텍스트 안에 여러 URL이 섞여 있으면 URL들을 동시에 크롤링하고, URL이 아닌 일반 텍스트도 함께 프롬프트에 포함합니다. 모든 URL 크롤링이 실패하고 대체 텍스트도 없으면 링크 대신 상세 텍스트를 직접 입력하라는 안내를 보냅니다.

모집 글이 생성되면 같은 채널에 비공개 워크스페이스 스레드를 만들고 작성자를 즉시 초대합니다. `[참가]` 버튼을 누른 팀원은 자동으로 해당 비공개 스레드에 추가됩니다. 봇에는 채널 기준 `Create Private Threads`, `Send Messages in Threads`, 필요 시 `Manage Threads` 권한이 있어야 합니다.

## Slash Command 초기화

고스트 커맨드가 쌓이면 `.env` 또는 Railway Variables에서 아래 값을 켠 뒤 봇을 재시작하세요.

```env
SYNC_COMMANDS=true
CLEAR_COMMANDS_ON_START=true
```

`GUILD_ID`가 설정되어 있으면 전역 명령과 해당 테스트 서버 명령을 먼저 비운 뒤 서버 명령을 빠르게 재등록합니다. `GUILD_ID`가 비어 있으면 전역 명령을 비운 뒤 전역 명령으로 재등록합니다. 전역 명령은 Discord 정책상 반영에 최대 1시간이 걸릴 수 있으므로 개발 중에는 `GUILD_ID`를 넣고 서버 단위로 테스트하는 편이 좋습니다.

슬래시 커맨드가 아예 보이지 않는 상황을 대비해 봇 소유자 전용 텍스트 명령도 선택적으로 켤 수 있습니다.

```env
ENABLE_ADMIN_TEXT_COMMANDS=true
```

이 경우 Discord Developer Portal에서 Message Content Intent를 활성화해야 하며, 서버 채널에서 `!인증 sync guild`, `!인증 sync global`, `!인증 clear all`처럼 사용할 수 있습니다.

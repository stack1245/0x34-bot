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
│   └── recruitment.py
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

Railway는 `Procfile`의 `worker: python bot.py`를 사용해 봇을 실행합니다. SQLite 파일을 장기간 보관해야 한다면 Railway Volume을 만들고 `DATABASE_PATH`를 Volume 경로로 설정하세요. 운영 규모가 커지면 PostgreSQL로 교체하는 편이 안전합니다.

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
- `/대회등록`: Modal로 대회 정보를 받아 알림 채널에 Embed를 전송합니다. 민감 정보 메모는 작성자에게만 Ephemeral 응답으로 보여줍니다.
- `/모집`: Modal로 모집 글을 만들고 `[참가]`, `[불참]`, `[대기]`, `[모집 마감]` 버튼으로 실시간 참가자 목록을 관리합니다.
- `/모집생성`: Gemini가 링크나 상세 텍스트를 분석해 모집 Embed를 만들고 동일한 참가 버튼을 붙입니다.

# Telegram Market Briefing Bot for Railway

비개발자도 Railway에 바로 배포할 수 있는 Python 텔레그램 자동 시장 브리핑 봇입니다.

이 봇은 Railway에서 24시간 켜진 worker로 실행되며, 한국장/유럽장/미국장 세션 기준으로 시장 브리핑을 텔레그램으로 보냅니다.

## 기능

- Telegram Bot API `sendMessage`로 텔레그램 전송
- OpenAI API로 뉴스와 시장 데이터를 요약
- `.env` 또는 Railway Variables로 환경변수 관리
- APScheduler로 한국장, 유럽장, 미국장 세션 기준 예약 실행
- 중요도 ★★★★★ 뉴스와 이벤트 긴급 알림
- 예정된 ★★★★★ 이벤트 6시간 전 사전 알림
- `sent_alerts.json` 기반 중복 알림 방지
- Railway 자동 배포 지원
- 오류 발생 시 텔레그램으로 오류 메시지 전송

## 폴더 구조

```text
.
├─ main.py
├─ requirements.txt
├─ Procfile
├─ railway.json
├─ .env.example
├─ .gitignore
└─ README.md
```

## 필요한 계정

1. Telegram 계정
2. OpenAI API 계정
3. GitHub 계정
4. Railway 계정

## 1. Telegram Bot 만들기

1. 텔레그램에서 `@BotFather`를 검색합니다.
2. `/newbot`을 입력합니다.
3. 봇 이름을 정합니다.
4. BotFather가 알려주는 토큰을 복사합니다.

이 값이 `TELEGRAM_BOT_TOKEN`입니다.

## 2. Telegram Chat ID 확인

1. 방금 만든 봇에게 텔레그램에서 아무 메시지나 보냅니다.
2. 브라우저 주소창에 아래 주소를 입력합니다.

```text
https://api.telegram.org/bot봇토큰/getUpdates
```

예시:

```text
https://api.telegram.org/bot123456789:ABCDEF/getUpdates
```

3. 화면에서 `"chat":{"id":123456789}`처럼 보이는 숫자를 찾습니다.

이 숫자가 `TELEGRAM_CHAT_ID`입니다.

## 3. OpenAI API Key 준비

OpenAI API 키를 준비합니다.

이 값이 `OPENAI_API_KEY`입니다.

## 4. 로컬에서 테스트하기

Windows PowerShell 기준입니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` 파일을 열고 값을 입력합니다.

```text
TELEGRAM_BOT_TOKEN=본인_텔레그램_봇_토큰
TELEGRAM_CHAT_ID=본인_텔레그램_채팅_ID
OPENAI_API_KEY=본인_OpenAI_API_Key
OPENAI_MODEL=gpt-4o-mini
RUN_ON_START=true
```

테스트 실행:

```powershell
python main.py
```

`RUN_ON_START=true`이면 실행 직후 텔레그램으로 테스트 브리핑이 1번 전송됩니다.
테스트가 끝나면 Railway 배포 전에는 `RUN_ON_START=false`로 바꾸는 것을 권장합니다.

## 5. GitHub에 업로드하기

### 방법 A: GitHub 웹사이트로 업로드

1. [GitHub](https://github.com)에 로그인합니다.
2. 오른쪽 위 `+` 버튼을 누릅니다.
3. `New repository`를 클릭합니다.
4. Repository name에 원하는 이름을 입력합니다.
   예: `telegram-market-briefing-bot`
5. `Public` 또는 `Private`를 선택합니다.
6. `Create repository`를 클릭합니다.
7. `uploading an existing file`을 클릭합니다.
8. 이 폴더의 파일을 업로드합니다.

업로드할 파일:

- `main.py`
- `requirements.txt`
- `Procfile`
- `railway.json`
- `.env.example`
- `.gitignore`
- `README.md`

업로드하지 말아야 할 파일:

- `.env`
- `.venv`
- `__pycache__`

`.env`에는 비밀키가 들어 있으므로 GitHub에 올리면 안 됩니다.

### 방법 B: 명령어로 업로드

Git이 설치되어 있다면 아래 순서로 업로드할 수 있습니다.

```powershell
git init
git add .
git commit -m "Initial Railway telegram briefing bot"
git branch -M main
git remote add origin https://github.com/본인아이디/저장소이름.git
git push -u origin main
```

## 6. Railway에 배포하기

1. [Railway](https://railway.app)에 로그인합니다.
2. `New Project`를 클릭합니다.
3. `Deploy from GitHub repo`를 선택합니다.
4. 방금 업로드한 GitHub 저장소를 선택합니다.
5. Railway가 자동으로 Python 프로젝트를 감지하고 배포를 시작합니다.

이 프로젝트에는 Railway용 파일이 이미 들어 있습니다.

- `requirements.txt`: Python 패키지 설치 목록
- `Procfile`: worker 실행 명령
- `railway.json`: Railway 배포 설정

## 7. Railway 환경변수 설정하기

Railway 프로젝트 화면에서:

1. 배포된 서비스를 클릭합니다.
2. `Variables` 탭을 엽니다.
3. 아래 변수를 추가합니다.

```text
TELEGRAM_BOT_TOKEN=본인_텔레그램_봇_토큰
TELEGRAM_CHAT_ID=본인_텔레그램_채팅_ID
OPENAI_API_KEY=본인_OpenAI_API_Key
OPENAI_MODEL=gpt-4o-mini
RUN_ON_START=false
```

저장하면 Railway가 자동으로 다시 배포합니다.

## 8. 24시간 동작 방식

Railway는 `python main.py`를 계속 실행합니다.
`main.py` 안의 APScheduler가 아래 시간마다 브리핑을 보냅니다.

- 06:50 KST - Morning Market Report
- 08:00 KST - Korea Pre-Market
- 16:00 KST - Korea Close Recap
- 08:00 Europe/Paris - Europe Pre-Market
- 18:00 Europe/Paris - Europe Close Recap
- 08:30 America/New_York - US Pre-Market
- 16:30 America/New_York - US Close Recap

유럽장과 미국장 스케줄은 코드에서 `Europe/Paris`, `America/New_York` 시간대를 사용하므로 서머타임을 자동 반영합니다.

프로그램이 오류로 종료되면 `railway.json` 설정에 따라 Railway가 다시 시작을 시도합니다.

## 9. Railway에서 바로 테스트 전송하기

처음 배포 후 테스트 메시지를 바로 받고 싶으면 Railway Variables에서:

```text
RUN_ON_START=true
```

로 바꾸고 저장합니다.

텔레그램 메시지가 정상적으로 오면 다시:

```text
RUN_ON_START=false
```

로 바꾸는 것을 권장합니다.

그렇지 않으면 Railway가 재시작될 때마다 브리핑이 한 번씩 추가 전송될 수 있습니다.

## 10. 브리핑 형식

06:50 Morning Market Report는 아래 고정 형식으로 전송됩니다.

```text
# Morning Market Report
## 0. [Current Asset Snapshot]
## 1. [Signal vs Noise]
## 2. [Economic Calendar]
## 3. [Market Pulse]
## 4. [Indicator Sensitivity]
## 5. [Today’s Priority]
```

다른 정규 리포트는 Korea Pre-Market, Korea Close Recap, Europe Pre-Market, Europe Close Recap, US Pre-Market, US Close Recap 성격에 맞춰 작성됩니다.

중요도 ★★★★★ 뉴스는 정규 시간 외에도 `[긴급 시장 알림 | ★★★★★]` 형식으로 전송됩니다.

## 11. 자주 생기는 문제

### 텔레그램 메시지가 오지 않아요

- 봇에게 먼저 아무 메시지나 보냈는지 확인하세요.
- `TELEGRAM_BOT_TOKEN`이 정확한지 확인하세요.
- `TELEGRAM_CHAT_ID`가 정확한지 확인하세요.

### Railway 배포는 됐는데 실행이 안 돼요

- Railway의 `Variables`에 3개 필수 값이 모두 있는지 확인하세요.
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OPENAI_API_KEY`

### OpenAI 오류가 나요

- OpenAI API 키가 정확한지 확인하세요.
- OpenAI API 결제 또는 사용 한도를 확인하세요.

### GitHub에 .env를 올렸어요

즉시 OpenAI API 키와 Telegram Bot Token을 새로 발급하세요.
`.env` 파일은 절대 GitHub에 올리면 안 됩니다.

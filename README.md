# JIN Market Pulse v2.2

한국인 개인 사용자가 Telegram 하나에서 아침 시장 파악, 실시간 가격 조회,
경제일정, 움직임의 배경, 개인 가격 알림을 처리하는 서버리스 봇입니다.

기준 폴더는 `D:\Codex\JIN-Market-Pulse`입니다. C드라이브에 프로젝트 복제본을
만들지 않습니다.

## 자동으로 오는 메시지

- 매일 `06:50 KST`: BTC 24시간 차트 1장과 2,000자 이하 모닝 리포트
- 중요도 5성 지표: 60~90분 전 사전 알림과 실제 발표 후 결과·시장 반응
- 긴급 뉴스: 공식 출처 1곳 또는 독립 매체 2곳과 유의미한 자산 움직임이 모두 확인될 때
- 개인 가격 알림: 사용자가 직접 만든 조건을 30분마다 점검

한국·유럽·미국 세션별 정기 리포트는 보내지 않습니다. 필요할 때 질문으로 조회합니다.

## 질문 예시

```text
이더 얼마야
비트 원화로
새벽 3시 이후 비트
미 10년물 변동
비트랑 금 중 뭐가 더 올랐어?
코스피 왜 떨어져?
DXY가 뭐야?
금리가 왜 Nasdaq에 중요해?
오늘 일정
이번 주 일정
다음 주 일정
비트 65000 아래면 알려줘
내 알림 목록
긴급 알림 꺼줘
8시간 조용히
앞으로 언제 메시지 와?
```

고정 버튼은 `현재 시장`, `오늘 일정`, `이번 주`, `왜 움직여?`, `최근 리포트`,
`상태`입니다.

명령 메뉴:

```text
/start /brief /price /compare /markets /calendar /week /last
/alerts /mute /settings /status /reset /help
```

## 상담 원칙

- 가격·변화율·발표 시각·예상·실제값은 코드가 공급원에서 직접 넣습니다.
- DXY, CPI, PCE, FOMC, PMI, 국채금리, 실질금리, QE·QT, VIX, 펀딩비,
  미결제약정은 내장 한국어 설명으로 즉시 답합니다.
- 현재 원인은 `관찰 사실`, `확인된 원인`, `가능한 배경`, `반대 증거`로 나눕니다.
- 직접 원인이 입증되지 않으면 가격 동행만 표시하고 원인이라고 단정하지 않습니다.
- 매수·매도, 목표가, 수익 보장과 가격 예측은 제공하지 않습니다.
- 이미지 질문은 하루 2회, 60초 이하 음성 질문은 하루 3회입니다.
- AI 설명·현재 원인·이미지·음성·링크 검증을 합쳐 하루 5회로 제한합니다.

## 데이터 원칙

- BTC·ETH: CoinGecko, Yahoo Finance 교차 경로
- 지수·외환·원자재: Yahoo Finance, FMP 키가 있을 때 선택적 보조
- 미국채 2년·10년: U.S. Treasury, FRED 보조
- 현재 주 경제일정: Forex Factory 공개 주간 JSON
- 다음 주 중요 미국 일정: BLS·BEA 공식 일정
- 뉴스 발견: Google News RSS
- 뉴스 검증: 공식 출처 또는 서로 다른 신뢰 매체

모든 자산은 현재값, 비교값, 변화, 기준 시각, 시장 상태, 출처, 신선도,
실제 자산·대용 자산 여부를 구분합니다. 급변값이 보조 자산 방향과 맞지 않으면
원인 분석과 긴급 알림에서 제외합니다.

## 상태와 복구

상태는 `/data/jin_market_pulse.sqlite3`에 SQLite WAL 방식으로 저장합니다.
기존 `/data/sent_alerts.json`은 최초 실행 때 안전 이전하고
`sent_alerts.json.pre-sqlite.bak`으로 보존합니다.

저장 대상:

- 모닝 리포트 차트·텍스트 단계
- 경제지표 사전값·결과·정정
- 뉴스 사건과 중복 방지
- 개인 가격 알림
- Telegram update 작업함
- 24시간 대화 문맥
- AI 사용량과 공급원 캐시·장애 상태

웹훅은 update를 먼저 저장한 뒤 200을 반환합니다. 처리 중 중단되면 다음 tick이
미완료 작업을 복구합니다.

## 공개 인터페이스

```text
GET  /health
GET  /ready
POST /jobs/tick
POST /jobs/morning
POST /telegram/webhook
```

`/jobs/tick`과 `/jobs/morning`은 `Authorization: Bearer <CRON_SECRET>`을
검증합니다. Telegram 웹훅은 `X-Telegram-Bot-Api-Secret-Token`과 허용된
`TELEGRAM_CHAT_ID`를 모두 검증합니다.

## Railway Variables

비밀값:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
OPENAI_API_KEY
CRON_SECRET
TELEGRAM_WEBHOOK_SECRET
```

운영값:

```text
RUN_MODE=serverless
RUN_ON_START=false
STATE_DIR=/data
ENABLED_REPORTS=morning
ENABLE_EVENT_ALERTS=true
ENABLE_EMERGENCY_ALERTS=true
ENABLE_AI_ADVISOR=true
AI_ADVISOR_DAILY_LIMIT=5
AI_CURRENT_CAUSE_DAILY_LIMIT=3
IMAGE_DAILY_LIMIT=2
VOICE_DAILY_LIMIT=3
MAX_PRICE_ALERTS=5
OPENAI_MODEL=gpt-5.6-luna
OPENAI_REASONING_EFFORT=low
OPENAI_WEB_SEARCH=true
PUBLIC_BASE_URL=https://서비스주소
CRON_TARGET_URL=https://서비스주소
DATA_CONTACT_EMAIL=personal-use@example.com
REQUEST_TIMEOUT_SECONDS=25
```

배포 후 Telegram 명령 메뉴와 웹훅은 비밀값을 출력하지 않는 다음 명령으로
등록합니다.

```bash
python -m jin_market_pulse.bootstrap
```

`FMP_API_KEY`는 선택입니다. 실제 비밀값은 Railway Variables와 GitHub Actions
Secrets에만 저장합니다.

## 저비용 배포

- 웹 서비스: Serverless, 1 Replica, `/data` Volume, 256MB, 0.25 vCPU
- Cron 서비스: `20,50 * * * *` UTC, 시작 명령 `python -m jin_market_pulse.cron`
- GitHub 백업: 매일 `06:58 KST`에 `/jobs/morning` 호출
- 빌드: Railpack, Python 3.12
- Healthcheck: `/health`

목표는 Railway 월 `$0.75 이하`이며 실제 사용량에 따라 달라질 수 있습니다.
비용 이메일 알림과 Workspace hard limit은 Railway 대시보드에서 별도로 확인합니다.

## 보안

GitHub에 올리면 안 되는 파일:

- `.env`
- 실제 API Key와 Telegram Token
- `sent_alerts.json`
- `jin_market_pulse.sqlite3`
- 로그와 압축 백업본

`.env.example`은 예시값만 있어 GitHub에 올려도 됩니다. 로그는 OpenAI Key,
Telegram Token과 URL query 비밀값을 자동 마스킹합니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pytest -q
$env:RUN_LIVE_TESTS='true'
.\.venv\Scripts\python.exe -m pytest -q tests/test_live_providers.py
```

`tests/fixtures/user_scenarios.json`은 한국어 질문·오타·후속 질문 150개 이상을
고정하며 결정형 라우팅은 100% 통과해야 합니다.

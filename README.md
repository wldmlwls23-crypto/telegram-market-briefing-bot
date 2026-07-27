# JIN Market Pulse v2

한국어 Telegram 시장 브리핑 봇입니다. Railway의 백그라운드 worker로 실행됩니다.

v2의 우선 목표는 매일 06:50 KST에 보내는 Morning Market Report의 정확성과 가독성입니다.

## 현재 활성 기능

- BTC와 ETH 현재가 및 24시간 변화
- Nasdaq 100, S&P 500, Dow, KOSPI, KOSDAQ
- DXY, 원/달러, WTI 유가, 금
- 미국 재무부 공식 2년물과 10년물 금리
- 중요 경제일정과 예상치, 이전치, 실제 발표값
- 06:50 KST Morning Market Report
- 중요도 5성 이벤트 6시간 전 사전 알림
- 이벤트 발표 직전 시장 기준값 저장
- 실제 발표값과 발표 전후 시장 반응 업데이트
- 신뢰 가능한 출처 기반 긴급 알림
- Railway Volume을 이용한 중복 방지 상태 유지

한국, 유럽, 미국 세션별 리포트는 모닝 리포트 검증이 끝난 뒤 v2.1에서 순차 활성화합니다.

## 데이터 원칙

- BTC와 ETH: CoinGecko
- 지수, 외환, 원자재: Yahoo Finance 보조 데이터
- 미국채 2년물과 10년물: U.S. Department of the Treasury
- 경제일정: Forex Factory calendar feed
- 시장 뉴스: Google News RSS에서 신뢰 가능한 매체만 선별
- 선택적 우선 공급원: Financial Modeling Prep

모든 가격에는 비교 기준, 데이터 시각, 출처가 붙습니다. 핵심 데이터가 부족하면 GPT가 빈칸을 채우지 않고 `[시장 데이터 점검 알림]`을 보냅니다.

## GitHub에 올리면 안 되는 파일

- `.env`
- 실제 OpenAI API Key
- 실제 Telegram Bot Token
- 실제 FMP API Key
- `sent_alerts.json`
- ZIP 또는 RAR 압축본
- 로그 파일

`.env.example`에는 예시값만 있으므로 GitHub에 올려도 됩니다.

## Railway Variables

필수:

```text
TELEGRAM_BOT_TOKEN=실제 값
TELEGRAM_CHAT_ID=실제 값
OPENAI_API_KEY=실제 값
OPENAI_MODEL=gpt-5.6
OPENAI_REASONING_EFFORT=medium
OPENAI_WEB_SEARCH=true
RUN_ON_START=false
STATE_DIR=/data
ENABLED_REPORTS=morning
ENABLE_EMERGENCY_ALERTS=false
REQUEST_TIMEOUT_SECONDS=25
```

선택:

```text
FMP_API_KEY=
```

비밀값은 Railway Variables에서만 관리합니다.

## Railway Volume 연결

중복 방지 기록을 재배포 후에도 유지하려면 Volume이 필요합니다.

1. Railway에서 `telegram-market-briefing-bot` 서비스를 엽니다.
2. 서비스에 Volume을 추가합니다.
3. Mount Path를 `/data`로 설정합니다.
4. Variables에서 `STATE_DIR=/data`인지 확인합니다.
5. 저장하고 배포합니다.

Volume을 연결하지 않아도 봇은 실행되지만 재배포하면 중복 기록이 초기화될 수 있습니다.

## 첫 v2 테스트

처음에는 긴급 알림을 끈 상태로 모닝 리포트 한 번만 확인합니다.

1. Railway Variables에서 `ENABLE_EMERGENCY_ALERTS=false`를 확인합니다.
2. `RUN_ON_START=true`로 바꿉니다.
3. Apply 또는 Deploy를 누릅니다.
4. Telegram에 v2 Morning Market Report가 한 번 오는지 확인합니다.
5. Railway Logs에 `Morning Market Report sent successfully`가 있는지 확인합니다.
6. 테스트가 끝나면 반드시 `RUN_ON_START=false`로 되돌립니다.
7. 다시 Apply 또는 Deploy를 누릅니다.

`RUN_ON_START=true`를 그대로 두면 재시작할 때마다 테스트 리포트가 반복됩니다.

## 긴급 알림 활성화

모닝 리포트를 먼저 확인한 뒤:

```text
ENABLE_EMERGENCY_ALERTS=true
```

로 변경하고 배포합니다.

긴급 알림은 다음 조건을 모두 통과해야 합니다.

- 공식 출처 한 곳 또는 서로 다른 신뢰 매체 두 곳 이상
- 갑작스러운 시장 충격 후보
- 같은 주제 6시간 내 재전송 아님
- 최근 30분 동안 긴급 알림이 3건 미만
- OpenAI의 최종 검증 통과

## 로컬 검증

공개 데이터 공급원만 확인:

```powershell
python -m jin_market_pulse.smoke
```

단위 테스트:

```powershell
pytest -q
```

실제 공개 공급원까지 포함:

```powershell
$env:RUN_LIVE_TESTS='true'
pytest -q
```

로컬 테스트에는 실제 Telegram Token이나 OpenAI API Key가 필요하지 않습니다. 실제 OpenAI 생성과 Telegram 전송은 Railway에서 확인합니다.

## Morning Market Report 형식

```text
# Morning Market Report

## 0. [Current Asset Snapshot]
## 1. [Signal vs Noise]
## 2. [Economic Calendar]
## 3. [Market Pulse]
## 4. [Indicator Sensitivity]
## 5. [Today's Priority]
```

표, 파이프 문자, `N/A`, `확인 필요`, 매수와 매도 지시는 사용하지 않습니다. 메시지가 길면 문단 경계에서 `1/2`, `2/2`로 나누어 전송합니다.

## 자동 실행

Railway 시작 명령:

```text
python main.py
```

정규 Morning Market Report:

```text
매일 06:50 KST
```

이 서비스는 웹사이트가 아닌 worker이므로 Railway에서 `Unexposed service`라고 표시되어도 정상입니다.

## 오류 확인

Telegram 메시지가 오지 않으면 Railway의 `Deployments`에서 최신 배포가 성공했는지 확인하고 `View Logs`를 엽니다.

주요 로그:

```text
JIN Market Pulse v2 started
Morning Market Report sent successfully
Morning report withheld
OpenAI morning analysis failed
Scheduled job failed
```

`Morning report withheld`는 핵심 공급원 데이터가 부족해 잘못된 리포트를 보내지 않았다는 뜻입니다.

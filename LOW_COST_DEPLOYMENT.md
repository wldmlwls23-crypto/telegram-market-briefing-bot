# JIN Market Pulse 저비용 운영

## 목표 구조

1. 웹 서비스는 Telegram 요청과 예약 호출이 있을 때만 깨어납니다.
2. `market-pulse-cron`이 매시 UTC 5·20·35·50분에 `/jobs/tick`을 호출하고 종료합니다.
3. tick은 장 마감·모닝·5성 지표·속보·개인 가격 알림과 미처리 웹훅을 점검합니다.
4. GitHub Actions는 매일 06:58 KST 모닝 백업만 담당합니다.

웹 서비스는 Serverless, 1 Replica, 256MB, 0.25 vCPU, `/data` Volume을
유지합니다. `RUN_ON_START=false`이므로 재배포 자체가 Telegram 메시지를 만들지 않습니다.

## 비용 제어

- 운영 목표: 월 `$0.75 이하`
- 이메일 알림: `$0.75`
- Compute hard limit: 대시보드에서 설정 가능한 `$1 이하 최저값`
- OpenAI: `gpt-5.6-luna`, reasoning `low`, user AI 요청 합계 하루 5회
- 이미지 2회, 음성 3회, 현재 원인 웹 검증 3회 이내

실제 비용은 Railway 정책과 사용량에 따라 달라지므로 고정 보장은 아닙니다.

## Cron 서비스

별도 `market-pulse-cron` 서비스의 Railway Config File은
`/railway.cron.json`으로 지정합니다. 시작 명령과 UTC 스케줄, 자원 상한은
이 파일에 고정되어 있습니다.

```text
Start Command: python -m jin_market_pulse.cron
Cron Schedule: 5,20,35,50 * * * *
CRON_TARGET_URL=https://telegram-market-briefing-bot-production.up.railway.app
CRON_SECRET=웹 서비스와 같은 값
```

Railway Cron은 UTC 기준입니다. 한 실행은 90초 안에 끝나며 이전 실행이 겹치면
다음 실행을 중복 수행하지 않도록 idempotency key를 사용합니다.

## 수동 테스트

`RUN_ON_START`를 바꾸지 않습니다. 별도의 idempotency key로 한 번 호출합니다.

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "$env:PUBLIC_BASE_URL/jobs/morning" `
  -Headers @{
    Authorization = "Bearer $env:CRON_SECRET"
    "X-Idempotency-Key" = "manual-v23-test"
  }
```

비밀값은 명령 기록이나 문서에 직접 적지 않습니다.

# JIN Market Pulse 저비용 운영

목표는 Railway 무료 서버리스와 하루 한 번의 OpenAI 호출을 사용해 월 비용을
약 1~1.5달러 수준으로 줄이는 것입니다. 실제 청구액은 데이터 양과 공급자 정책에
따라 달라질 수 있으므로 고정 상한은 아닙니다.

## 작동 방식

1. 서비스는 평소에 잠든 상태로 유지됩니다.
2. GitHub Actions가 매일 06:50 KST에 Railway 공개 주소를 호출합니다.
3. Railway가 깨어나 모닝 리포트 한 건을 생성하고 Telegram으로 보냅니다.
4. 작업이 끝나면 다시 유휴 상태가 됩니다.

실시간 긴급 알림과 5~30분 간격의 이벤트 감시는 이 모드에서 실행하지 않습니다.

## Railway Variables

```text
RUN_MODE=serverless
RUN_ON_START=false
STATE_DIR=/data
ENABLED_REPORTS=morning
ENABLE_EMERGENCY_ALERTS=false
OPENAI_MODEL=gpt-5.6-luna
OPENAI_REASONING_EFFORT=low
OPENAI_MAX_OUTPUT_TOKENS=2500
OPENAI_WEB_SEARCH=true
CRON_SECRET=길고 무작위인 비밀값
```

기존 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENAI_API_KEY`는 그대로
Railway Variables에만 보관합니다. Volume의 Mount Path는 `/data`를 유지합니다.

## GitHub Actions secrets

```text
RAILWAY_JOB_URL=https://Railway에서-생성한-도메인
CRON_SECRET=Railway와-같은-비밀값
```

워크플로 파일은 `.github/workflows/morning-report.yml`입니다. GitHub 예약 실행은
혼잡할 때 몇 분 늦어질 수 있습니다.

## 안전한 테스트

GitHub의 `Actions`에서 `Morning Market Report`를 선택한 뒤 `Run workflow`를
한 번 누릅니다. Telegram 수신과 Railway 로그를 확인합니다. 테스트 중복 전송을
막기 위해 Railway의 `RUN_ON_START`는 계속 `false`로 둡니다.

# JIN Market Pulse 프로젝트 상태

최종 업데이트: 2026-07-28

## 기준 폴더

`D:\Codex\JIN-Market-Pulse`

## 현재 버전

JIN Market Pulse v2 구현 중입니다.

- `main.py`는 실행 진입점만 담당합니다.
- 실제 기능은 `jin_market_pulse` 패키지로 분리했습니다.
- Stooq 의존성을 제거했습니다.
- Morning Market Report를 v2 우선 대상으로 설정했습니다.
- 한국, 유럽, 미국 세션 리포트는 v2.1 대상입니다.

## 운영 기본값

```text
OPENAI_MODEL=gpt-5.6
OPENAI_REASONING_EFFORT=medium
OPENAI_WEB_SEARCH=true
RUN_ON_START=false
STATE_DIR=/data
ENABLED_REPORTS=morning
ENABLE_EMERGENCY_ALERTS=false
```

## 배포 전 남은 외부 확인

- Railway Volume을 `/data`에 연결
- Railway Variables 갱신
- `RUN_ON_START=true`로 1회 테스트
- 실제 Telegram 출력 승인
- `RUN_ON_START=false` 원복
- 이후 `ENABLE_EMERGENCY_ALERTS=true` 활성화

비밀값은 Railway Variables에서만 관리합니다.

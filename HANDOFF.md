# JIN Market Pulse v2.1 인수인계

최종 업데이트: 2026-07-28

## 현재 로컬 위치

`D:\Codex\JIN-Market-Pulse`

## 먼저 읽을 파일

1. `PROJECT_STATUS.md`
2. `README.md`
3. `jin_market_pulse/config.py`
4. `jin_market_pulse/app.py`

## 구조

- `main.py`: Railway 시작점
- `jin_market_pulse/providers.py`: 시장 가격과 미국채 수집
- `jin_market_pulse/calendar.py`: 경제일정 필터
- `jin_market_pulse/news.py`: 뉴스 출처와 중복 필터
- `jin_market_pulse/reports.py`: OpenAI 구조화 분석과 텔레그램 리포트 렌더링
- `jin_market_pulse/chart.py`: BTC 24시간 PNG 차트
- `jin_market_pulse/bot_queries.py`: Telegram 숫자 조회
- `jin_market_pulse/advisor.py`: 내장 시장 용어와 제한형 AI 설명
- `jin_market_pulse/alerts.py`: 사전 알림, 결과 업데이트, 긴급 알림
- `jin_market_pulse/state.py`: Railway Volume 상태 저장
- `tests`: 자동 테스트

## 보안

API Key와 Telegram Token을 코드, 문서, 로그, GitHub에 기록하지 않습니다.

## 이어서 작업할 때

```text
JIN Market Pulse v2 이어받기 모드로 시작해줘.
D:\Codex\JIN-Market-Pulse의 PROJECT_STATUS.md와 README.md를 먼저 읽어줘.
비밀값은 출력하지 말고 Railway Variables에서만 관리해줘.
먼저 pytest와 공개 API smoke test 결과를 확인한 뒤 작업해줘.
```

## 다음 단계

v2.1 모닝 리포트를 최소 이틀 검증한 뒤 한국, 유럽, 미국 장 전후 리포트를 순차 활성화합니다.

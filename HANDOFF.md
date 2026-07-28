# JIN Market Pulse v2.2 인수인계

최종 업데이트: 2026-07-29

## 기준 폴더

`D:\Codex\JIN-Market-Pulse`

## 먼저 확인할 것

1. `PROJECT_STATUS.md`
2. `README.md`
3. `git status --short --branch`
4. `.\.venv\Scripts\python.exe -m pytest -q`
5. Railway 최신 배포와 `/health`, `/ready`

## 코드 구조

- `main.py`: Railway 웹 진입점
- `providers.py`: 가격·금리, 캐시, 급변 교차검증
- `calendar.py`: 현재 주·공식 다음 주 경제일정
- `news.py`: 출처 등급, 사건 키, 중복·오탐 필터
- `reports.py`: 구조화 AI 선택과 모닝 HTML 렌더링
- `chart.py`: BTC 24시간 PNG
- `bot_queries.py`: 결정형 한국어 라우터와 답변
- `advisor.py`: 내장 용어, 구조화 현재 원인, 이미지·음성
- `alerts.py`: 5성 지표·긴급 뉴스
- `jobs.py`: 30분 tick, Telegram 작업함, 개인 알림
- `state.py`: SQLite WAL과 JSON 안전 이전
- `server.py`: `/health`, `/ready`, jobs, Telegram 웹훅
- `telegram.py`: 전송·수정·버튼·명령·파일 API
- `tests/fixtures/user_scenarios.json`: 150개 이상 사용자 계약

## 안전 원칙

- API Key와 Token을 채팅, 코드, 로그, README, GitHub에 기록하지 않습니다.
- 실제 값은 Railway Variables에서만 관리합니다.
- `sent_alerts.json` 원본과 이전 백업은 삭제하지 않습니다.
- 자동 주문, 매수·매도 신호, 목표가, 수익 보장은 추가하지 않습니다.
- C드라이브에 프로젝트 복제본을 만들지 않습니다.

## 이어서 작업할 때

```text
JIN Market Pulse v2.2 작업을 D:\Codex\JIN-Market-Pulse에서 이어가줘.
PROJECT_STATUS.md와 README.md를 먼저 읽고, 비밀값은 출력하지 마.
로컬 전체 테스트와 실공급원 smoke, GitHub CI, Railway 배포 상태를 각각 검증해줘.
```

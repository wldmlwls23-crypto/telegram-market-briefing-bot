# JIN Market Pulse 프로젝트 상태

최종 업데이트: 2026-07-17

## 기준 폴더

`D:\Codex\JIN-Market-Pulse`

## 목적

Telegram Market Briefing Bot 운영/유지보수 프로젝트다.

## 주요 파일

- `main.py`: 봇 메인 코드
- `requirements.txt`: Python 의존성
- `.env.example`: 환경변수 예시
- `README.md`: 배포/운영 안내
- `telegram-market-briefing-bot-updated-v*.zip`: 과거 전달용 압축본. GitHub에는 올리지 않는다.

## GitHub 분리 원칙

이 프로젝트는 자재24 운영허브와 섞지 않는다.

- 자재24: `j24-workspace`
- JIN Market Pulse: `telegram-market-briefing-bot`

## 주의

- `.env`, API Key, Telegram Bot Token은 GitHub에 올리지 않는다.
- Railway Variables에 있는 비밀값은 로컬 문서에 원문으로 기록하지 않는다.
- ZIP 전달본은 보관용이며 Git 추적 대상에서 제외한다.

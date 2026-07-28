# JIN Market Pulse 프로젝트 상태

최종 업데이트: 2026-07-29

## 기준

- 폴더: `D:\Codex\JIN-Market-Pulse`
- 버전: v2.2 사용자 완성판
- 브랜치: `main`
- 운영 형태: Railway Serverless 웹 서비스와 30분 Cron

## 로컬 완료

- BTC 차트와 2,000자 이하 한국어 모닝 리포트
- 검증 시장 데이터와 공식 미국 일정 보조 공급원
- SQLite 이전, 작업함, 캐시, 중복·부분 전송 복구
- Telegram 고정 버튼·명령·자연어 라우터
- 가격·비교·기간·개념·관계·현재 원인·후속 질문
- 오늘·내일·24시간·이번 주·다음 주 일정
- 개인 가격 알림, mute, 설정, status
- 이미지·음성·기사 링크 제한형 상담
- 5성 지표 단계 알림과 검증 긴급 뉴스
- Railpack/Python 3.12, CI, 06:58 GitHub 백업
- 150개 이상 한국어 사용자 시나리오 자동 검증

## 배포 확인 항목

- GitHub `main` push와 CI 성공
- Railway `/data` Volume과 Serverless 유지
- 웹 서비스 Variables 갱신
- `market-pulse-cron` 서비스 생성·UTC Cron 연결
- Telegram 명령 메뉴와 웹훅 재등록
- 실제 모닝 1회와 가격·일정·원인·알림 종단 테스트
- 비용 알림과 hard limit 확인
- 성공 커밋에 Git tag 생성

비밀값은 출력하거나 코드·문서·GitHub에 기록하지 않습니다.

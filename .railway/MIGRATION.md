# Market Pulse 운영 이전 메모 (2026-11-24)

웹 배포 aaf2ff23과 크론 배포 7d00abac의 소스 커밋은 df7dcff이다.
Deployment Details의 propertyFileMapping은 각각 /railway.json과
/railway.cron.json을 가리킨다. Settings에 웹 경로는 비어 있고 크론 경로는
/railway.cron.json이다. CLI migrate status는 둘 다 없다고 표시했다.
두 서비스는 main 푸시에 자동 배포되며 CI 대기는 꺼져 있다.

## 검증

SDK를 .railway에서 설치하고 저장소 루트에서
`node --experimental-strip-types .railway/verify-migration.mjs`를 실행한다.
기존 JSON 삭제 전 검사용이다. 5.75.0 CLI의 읽기 전용 config plan 결과는
0 add, 2 change, 0 destroy였다. 웹 명령, 헬스체크와 제한, 크론 제한을
서비스 설정에 명시한다. 상태 경로는 웹 STATE_DIR=/data이다.

## 별도 승인 후

1. 정규 발송 사이에 작업한다. 최신 배포, 두 서비스의 자동 배포 설정과 크론의
   사용자 지정 파일 경로를 다시 기록하고 기존 /data 백업의 복구 가능 여부를 확인한다.
2. 자동 배포를 일시 중지하고 모든 전환 설정을 검토한다. cron 파일 경로만
   먼저 해제한 상태로 루트 railway.json이 있는 커밋을 재배포하면 크론이 웹
   설정을 상속할 수 있다. 서비스 재시작을 유발하는 변경은 중간 상태로 배포하지 않는다.
3. 두 서비스의 IaC 설정과 동일한 값을 확보하고, 크론 경로 해제와 JSON이 없는
   최종 소스 전환을 조율한다. CLI의 cutover 대상 감지가 실제 저장 경로와
   다르면 Settings에서 명시적 크론 경로를 확인한다. plan을 다시 확인하고
   추가/삭제 또는 다른 자원 변경이 있으면 중단한다. 전환 중 실행될 커밋이나
   설정 출처를 보장할 수 없으면 진행하지 않는다.
4. 적용 후 웹과 크론의 Deployment Details, /health, /ready, 볼륨 ID와
   마운트, cron 단일 시간표를 확인하고 main 자동 배포를 복원한다.
   다음 정규 06:50 KST 발송에서 차트의 기준일과 본문의 KST 보고일을 각각
   대조하고 차트 1장과 본문 1건의 도착을 확인한다.

## 복구

기존 서비스와 볼륨을 유지한다. 종료일 전에는 기존 JSON이 있는 df7dcff와
기록한 경로(웹 기본 탐색, 크론 명시 경로)를 함께 복원한다. 종료일 이후에는
이전 애플리케이션 코드에 검증된 서비스별 IaC 값을 적용하고 실제 명령과
동일한 /data 볼륨을 확인한다. 수동 리포트 재발송으로 검증하지 않는다.

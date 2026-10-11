# 티처덱 모듈 원천

이 폴더의 `module/`은 쿨 예약실 웹 모듈의 원천입니다. 원작 CoolReserve의 Python 코어와 같은 저장소에서 검토하고 배포합니다. Python 코어는 기존 MIT 라이선스를 유지하며, 새 티처덱 모듈은 [GPL-3.0-only](LICENSE)와 [추가 약관](LICENSE-ADDITIONAL-TERMS)을 따릅니다. 원작 MIT 저작자 고지는 모듈 README에도 보존합니다.

TeacherDeck 개발자는 검토를 마친 커밋을 체크아웃하고 `tools/scripts/sync-coolreserve.ps1 -SourceCheckout <원작 체크아웃>`을 실행합니다. 동기화 도구는 작업 트리가 깨끗한지 확인하고 Git에 커밋된 바이트에서 모듈 소스와 제한된 실행 코어를 가져옵니다. 이후 `pnpm gen`, `pnpm verify`, 설치본 빌드를 통과해야 티처덱에 포함할 수 있습니다. 앱이 실행 중인 원격 코드를 자동으로 받지는 않습니다.

`module/package.json`의 workspace 의존성은 TeacherDeck 작업 공간에서 해결합니다. 단독 CoolReserve Python 앱과 모듈의 화면은 다르지만 예약 등록 코어는 같습니다. 현재 연결의 첨부 지원 여부와 실제 쿨메신저 검증 한계는 [모듈 안내](module/README.md)를 참고하세요.

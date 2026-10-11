# TeacherDeck 연동 개발 계약

독립 CoolReserve의 Store·Runner·WindowsDriver와 업무 처리기를 TeacherDeck에서도 같은 원천으로 사용합니다. TeacherDeck의 관리 Windows x64 Python·고정 의존성은 별도 호스트 배포물이며 이 공개 소스 ZIP에는 포함하지 않습니다. 독립판 INSTALL/START 경로는 그대로 유지합니다.

## 고정 작업자

진입점은 `teacherdeck_worker.py`입니다. isolated/no-site/bytecode 금지 옵션으로 실행하며 진입점은 자기 배포 폴더만 import 경로에 넣습니다. 호스트만 앱 소유 자료 폴더를 `--data-dir`로 전달합니다. 일반 사용자가 직접 실행할 프로그램이 아닙니다.

비공개 stdin/stdout은 UTF-8 JSON line이며 LF 포함 512KiB 한도를 둡니다. 요청은 `{id,method,args}`, 응답은 `{id,ok:true,result}` 또는 `{id,ok:false,error:{code}}`입니다. method는 connect/recipients/plan/start/status/cancel만 허용합니다. 경로·계정·화면 선택자는 공개 모듈에서 받지 않으며 호스트가 검증한 첨부 경로와 원작 수신자 identity만 내부 채널에 둡니다. helper stdout/stderr는 일반 로그로 복사하지 않습니다.

- connect는 기존 조직도와 본인 증거를 읽어 connectionId·accountBinding을 반환합니다. 유효한 저장 self_binding은 재사용합니다. 최초 또는 증거 변경 때만 원작의 이미 열린 본인 작성창 대조가 필요합니다. helper가 작성창을 열거나 클릭하지 않습니다.
- recipients는 연결된 계정의 표시 이름·소속·계정 표시와 private recipientId를 반환합니다. 호스트는 이를 세션·모듈 소유의 공개 recipientHandle로 감쌉니다.
- plan은 고정 메시지·연결·수신자·첨부 identity에 묶인 검토를 만들고 5분에 만료합니다. 한 계획은 500건·512KiB·제목/본문 합계 256KiB 이하입니다. 초과 데이터를 자르지 않습니다. inspect/prepare는 원작처럼 한 건만 허용합니다.
- start의 confirmed와 accountBinding은 호스트만 넘깁니다. register는 trusted shell의 한 번 확인을 요구합니다. 공개 메시지의 본문·제목이 원작 정규화 결과와 달라지면 조용히 바꾸지 않고 거절합니다.
- status는 고정 단계·건별 상태·retryLocked만 반환합니다. 원작 로그·진단·경로는 복사하지 않습니다. 기본 정상 결과는 등록 요청 완료이며 수신함 도착이나 서버 접수의 증명이 아닙니다.
- cancel과 F8는 같은 원작 stop flag를 사용합니다. 이미 등록된 예약을 취소하는 기능이 아닙니다.

Store의 SQLite FULL 동기화와 begin_submit, 재시작 uncertain 복구, 삭제 후에도 남는 제출 지문을 유지합니다. 준비 전 일시 실패만 원작의 최대 한 번 복구가 가능하며 보내기 이후 자동 재시도하지 않습니다. helper는 Google 직접 연결·HTTP 서버를 import하거나 실행하지 않습니다.

## 현재 지원 범위

원작 자동 프로필은 무첨부 확인만 지원합니다. 실제 첨부 목록·파일 선택 연결이 없으면 attachments feature를 제공하지 않고 초안 첨부는 보존합니다. 수동 화면 역할 연결을 TeacherDeck에 임의 선택자로 확대하지 않습니다. 선택 예약목록 대조도 실제 프로필 capability가 있을 때만 제공하며 기본은 manual입니다.

실행 import 파일은 app의 `__init__`, core, engine, windows, gentoo, gentoo_tree, recipients, verification, draft_recovery, waiting, privacy, teacherdeck_worker와 고정 root 진입점입니다. 원작 업무·반복 처리기는 모듈 입력 처리에서 재사용하며 helper의 임의 추가 RPC로 노출하지 않습니다. comtypes wrapper는 메모리에 생성하고 bytecode·런타임 폴더 쓰기는 금지합니다.

## 검증과 배포

`tests/test_teacherdeck_worker.py`는 가짜 provider/driver와 합성 SQLite만 사용합니다. 관리 실행 환경이 준비되면 `tools/test_teacherdeck_managed.py --runtime <관리 런타임 폴더>`로 실제 isolated Python의 잘못된 method/field 거절과 런타임 파일 해시 불변을 검사합니다. 이 검사는 Gentoo 조회·창 열기·메시지 발송을 요청하지 않습니다.

TeacherDeck은 원천 SHA와 파일별 SHA256으로 같은 소스를 소비합니다. 공개 파일은 public-release.json과 .gitignore의 정확한 허용목록에 추가한 뒤 기존 build_public_release.py로 별도 배포본과 RELEASE-MANIFEST.json을 생성합니다. 실제 메시지 시험은 별도 승인 전까지 하지 않으며 합성 성공을 기관 앱의 실제 호환 완료로 표시하지 않습니다. 원작 MIT LICENSE와 제삼자 고지는 유지합니다.

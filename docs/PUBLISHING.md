# GitHub 공개 준비

공개 저장소는 [rochelobeJYJ/CoolReserve](https://github.com/rochelobeJYJ/CoolReserve)입니다. 사용자는 [최신 Release](https://github.com/rochelobeJYJ/CoolReserve/releases/latest)의 배포 ZIP으로 시작하고, 저장소의 **Code → Download ZIP**으로 개발 소스를 받을 수도 있습니다. README의 첫 사용 순서와 Release 설명은 같은 내용을 안내해야 합니다.

공개는 **검사한 별도 소스 폴더**에서 시작합니다. 개발 중 사용한 폴더 전체를 GitHub에 올리거나 그 폴더에서 `git add .`를 실행하지 마세요. `.gitignore`만으로 기존 파일이나 Git 이력의 개인정보까지 제거되지는 않습니다.

## 공개본 만들기

프로젝트의 `public-release.json`은 포함할 파일의 명시적 목록입니다. 소스·공개 문서·빈 양식·라이선스·검토한 합성 검사만 복사합니다. 작업 자료·실행 기록·캡처·개인 설정·원본 복사본은 이 목록에 포함하지 않습니다.

개발 환경에서 다음을 실행합니다.

```powershell
.venv\Scripts\python.exe -B -X utf8 tools\check_public.py
.venv\Scripts\python.exe -B -X utf8 tools\build_public_release.py --output dist\CoolReserve-public
.venv\Scripts\python.exe -B -X utf8 tools\check_public.py --root dist\CoolReserve-public --staging
```

기존 출력이 있으면 덮어쓰지 않습니다. 새 버전 이름을 출력 경로에 지정하세요. 성공하면 별도 폴더와 ZIP, 각 포함 파일의 SHA-256이 기록된 `RELEASE-MANIFEST.json`이 생깁니다. ZIP 내용도 생성 시 폴더와 대조합니다.

실제 업무를 사용했던 개발 PC에서는 공개 파일에 섞일 수 있는 이름·기관명·개인 문서 ID를 JSON 문자열 배열로 **비공개 파일**에 저장해 추가 대조하세요. 위 두 검사와 빌드 명령에 모두 `--deny-file "비공개 JSON 파일 경로"`를 추가합니다. 검사기는 그 값이나 해시를 출력·배포하지 않습니다. 이 파일 자체를 Git에 추가하지 마세요.

검사는 파일 형식, 인증값·개인 주소·계정 형태, XLSX 내부 XML·숨은 값·외부 참조, 링크된 파일, 허용목록 밖 파일을 점검합니다. 실패하면 보고된 위치를 수정하거나 공개 대상에서 제외한 뒤 다시 생성하세요. 민감값을 인코딩해 검사를 피하면 안 됩니다.

## 마지막 확인

1. 별도 공개 ZIP을 열어 포함 목록과 문서를 확인합니다. 빈 양식에 개인 명단·본문·시트 주소가 없는지 봅니다.
2. 검사는 공개본을 실행하기 **전**에 합니다. 실행하면 `.venv`나 테스트 출력이 생길 수 있으므로 검사한 원본 ZIP을 보존하고 실행 시험은 별도 복사본에서 합니다.
3. 실행 시험은 INSTALL·TEST·모의 모드로 진행합니다. 합성 자료로 **예약표 가져오기**와 **자료로 메시지 만들기**, **등록 가능 N건 · 수정 필요 M건** 안내를 확인하세요. 실제 메시지를 보내는 시험은 수신자와 내용을 정한 후 별도로 수행하세요.
4. 개발 폴더와 운영 데이터는 그대로 비공개로 유지합니다. 자동 검사는 임의의 모든 개인정보를 식별하지 못하므로 새 파일을 허용목록에 넣을 때 내용을 검토하세요.

지원 조건과 실제 검사 범위를 구분해 배포 설명에 남기세요. 현재 새 설치는 Python 3.14 64비트에서 확인했으며 3.13의 새 설치는 별도로 시험하지 않았습니다. 제목 없는 입력·대조의 자동 검사 통과를 실제 쿨메신저의 무제목 발송 완료로 설명하지 마세요. 정상 **등록 요청 완료**와 선택적인 확인 기록, 전송 전 중단과 등록 여부 불명의 재등록 잠금도 구분합니다.

개발 환경에서 확인한 자동 검사 수치는 Python 608개·화면 로직 70개입니다. 공개본은 합성 자료로 된 검사만 포함하므로 공개 ZIP에서 실행한 검사 수와 다를 수 있습니다. Release에 검사 결과를 적을 때는 개발 환경 검사와 해당 공개본 검사를 구분하고 실제 실행 결과를 사용하세요.

## GitHub에 올릴 때

처음 공개할 때는 새 빈 폴더에 **공개 ZIP만** 풉니다. 그 폴더에서 새 Git 저장소를 만들어 공개할 파일을 검토합니다. 기존 개발 저장소의 `.git`, commit 이력, stash, remote 설정을 복사하지 마세요.

```powershell
git init
git add .
git diff --cached --stat
git diff --cached
git commit -m "Initial public release"
```

명령을 실행하기 전에 현재 위치가 공개 ZIP을 푼 폴더인지 확인하세요. 공개 대상은 `https://github.com/rochelobeJYJ/CoolReserve.git`입니다. 원격 저장소의 기존 파일과 이력을 먼저 확인하고, 검토한 공개 커밋을 올립니다. 기존 원격 이력을 강제로 덮어쓰지 마세요. 이후 업데이트도 검사를 거친 공개 파일만 반영합니다. 위 준비 도구 자체는 저장소 생성·푸시·공개 전환을 수행하지 않습니다.

## Release와 다운로드 안내

1. GitHub에 올라간 파일이 검토한 공개본과 일치하는지 확인하고 그 커밋에 버전 태그를 만듭니다.
2. 해당 태그로 Release를 작성합니다. **빌드 도구가 생성하고 검사한 배포 ZIP**을 첨부하세요. 실행 시험으로 `.venv`나 임시 자료가 생긴 폴더를 다시 압축하지 마세요.
3. Release 설명에 아래 첫 사용 순서를 넣고 [처음 사용 안내](FIRST_RUN.md)를 연결합니다.
4. 게시 후 [최신 Release 링크](https://github.com/rochelobeJYJ/CoolReserve/releases/latest)에서 ZIP을 받을 수 있는지, 첨부 ZIP의 내용과 `RELEASE-MANIFEST.json`이 검토한 공개본과 일치하는지 확인합니다. 이 확인 전에는 다운로드가 검증됐다고 안내하지 마세요.

첫 사용 순서는 **ZIP 다운로드 → 압축 모두 풀기 → Python 3.14 64비트 설치 → INSTALL.cmd → 쿨메신저 로그인·조직도 전체 표시 → START.cmd → 조직도 계정 불러오기 → 본인에게 한 건 시험**입니다. 첫 설치에는 인터넷이 필요하고, 예약 등록 중에는 완료될 때까지 PC 입력·잠금·절전을 멈춘다고 함께 안내합니다.

기능은 일반 업무용 **새 메시지·예약표 가져오기·자료로 메시지 만들기**를 먼저 설명합니다. **중식 지도**는 확정된 배정표를 읽는 선택 프리셋입니다. 실제 배정표·개인 명단·Google 시트 주소는 예시로 넣지 마세요.

소스는 MIT 라이선스입니다. `LICENSE`, `THIRD_PARTY_NOTICES.md`, `third-party/`의 원문 고지를 함께 유지하세요. 이 ZIP에는 Python·의존성 바이너리·쿨메신저 프로그램이 포함되지 않습니다. 나중에 EXE를 묶어 배포한다면 실제 포함하는 구성요소의 고지를 다시 확인해야 합니다.

[공개 이슈](https://github.com/rochelobeJYJ/CoolReserve/issues)에는 실제 이름·본문·인증 파일·실제 화면을 올리지 말고 합성 자료와 필요한 버전 정보로 재현 방법을 작성하세요. README와 첫 사용 안내의 도움말 링크가 이 주소를 가리키는지도 확인합니다.

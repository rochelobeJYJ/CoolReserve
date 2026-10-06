# 제3자 고지

`LICENSE`의 MIT는 프로젝트 자체 코드와 문서에 적용합니다. 아래 의존성 및 `third-party/`의 고지는 각각의 원래 라이선스를 유지합니다. 이 소스 배포에는 의존성의 실행파일·라이브러리 바이너리를 포함하지 않습니다.

| 의존성 | 고정 버전 | 고지 |
| --- | --- | --- |
| pywinauto | 0.6.9 | BSD-3-Clause |
| comtypes | 1.4.17 | MIT |
| six | 1.17.0 | MIT |
| pywin32 | 312 | 패키지 메타데이터는 PSF. 실제 배포에는 파일별로 다른 고지가 포함됨 |

설치된 해당 버전의 라이선스 파일을 `third-party/`에 원문 그대로 복사했고 `license-inventory.json`에 해시를 기록했습니다. pywin32의 adodbapi에는 LGPL 고지, IDLE에는 역사적 라이선스 고지가 포함됩니다. 이 파일들을 프로젝트의 MIT로 다시 표시하지 않습니다. 추후 EXE·의존성 바이너리를 묶어 배포한다면 실제 포함한 구성요소에 맞춰 원문 고지와 배포 조건을 다시 확인해야 합니다.

공식 출처: [pywinauto](https://github.com/pywinauto/pywinauto/blob/0.6.9/LICENSE), [comtypes](https://github.com/enthought/comtypes/blob/v1.4.17/LICENSE.txt), [six](https://github.com/benjaminp/six/blob/1.17.0/LICENSE), [pywin32 배포 안내](https://pypi.org/project/pywin32/312/).

Python을 별도로 설치해 사용합니다. Python 인터프리터를 배포에 포함할 경우에는 [Python 라이선스](https://docs.python.org/3/license.html)도 함께 검토해야 합니다.

이 배포에는 쿨메신저 제품 코드·실행파일·로고·화면 캡처와 출처 미확인 원본 복사본·전송 조각을 포함하지 않습니다.

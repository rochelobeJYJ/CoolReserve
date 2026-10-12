# 쿨 예약실

쿨메신저 Gentoo 예약 메시지를 작성하고 여러 건을 검토·등록합니다. 원작 CoolReserve의 새 메시지, 공동 수신, 반복, 예약표, 일반 업무자료, 선택형 중식 지도, 양식 저장, 보관 흐름을 덱 화면으로 옮겼습니다.

## 사용

1. 쿨메신저에 로그인하고 조직도를 연 뒤 **조직도 계정 불러오기**를 누릅니다. 처음 본인 확인 안내가 나오면 본인 항목에서 ‘나에게 보내기’를 켠 작성창을 준비하고 다시 연결합니다.
2. **새 메시지**에서 한국시간 예약시각·내용을 입력합니다. 제목은 선택입니다. 여러 이름은 쉼표나 줄바꿈으로 넣고 동명이인은 소속과 전체 계정 표시를 보고 선택합니다. 매주 반복은 날짜마다 개별 초안을 만들며 공휴일을 자동 제외하지 않습니다.
3. **예약표 가져오기**는 XLSX·UTF-8/CP949 CSV를 읽습니다. 붙여넣기는 열 이름을 포함한 탭 구분 표와 CSV를 읽습니다. 필수 열은 받는사람·예약날짜·예약시간·내용이며 제목·첨부파일은 선택입니다. 기존 예약일시와 Excel 날짜·시간 숫자값도 읽습니다. 모든 행이 확인되어야 함께 저장합니다.
4. **자료로 메시지 만들기**는 일반 업무 양식으로 시작합니다. 날짜·대상·내용 열과 {열 이름} 문구 변수를 연결합니다. 중식 지도는 별도 프리셋이며 확정된 1·2차 담당자·제외일·고사 1차만 표시를 읽습니다. 배정을 계산하지 않습니다. 실제 계정을 명시적으로 연결한 뒤 검토합니다.
5. 목록에서 미등록 항목을 선택하고 **내용 검토**를 누릅니다. 수신자·제목·본문·예약시각·첨부를 확인하고 실행합니다. 실제 등록은 마지막 덱 확인창에서 승인합니다. 모의 실행·작성창 대조·자동 입력은 보내기를 누르지 않습니다.

업무자료를 다시 읽으면 새 초안·변경·동일·사라진 업무를 비교합니다. 자료 식별명을 같은 자료·시트에서 유지하세요. 원본이 그대로이면 개별 편집을 보존하고 원본 변경과 개별 편집이 겹치면 반영을 막습니다. 등록을 시도한 날짜·차시는 자동 재생성·삭제하지 않습니다.

## 연결·실행 범위

현재 조직도 계정은 전체 명부를 보장하지 않습니다. 계정이 빠지면 조직도 표시 필터를 전체로 바꾸세요. 저장한 수신자는 이름·소속·계정 표시가 유일하고 정확히 일치할 때만 새 핸들에 연결합니다.

현재 자동 연결에서는 첨부 등록을 검증하지 못할 수 있습니다. 첨부 지원이 없는 연결은 계획 전에 안내하고 초안을 보존합니다. 선택한 파일 핸들만 전달하며 예약표의 전체 경로를 자동으로 열지 않습니다. 재시작·백업 복구 뒤 첨부는 다시 선택해야 합니다.

입력·등록 중에는 마우스·키보드 사용, 화면 잠금·절전을 멈추세요. 중지와 F8는 현재 Windows 호출 이후 반영될 수 있으며 이미 접수된 예약을 취소하지 않습니다. **등록 요청 완료**는 보내기와 작성창 종료를 마친 상태이며 도착 조회 결과가 아닙니다. 제출 후 불확실한 결과는 **등록 여부 불명**과 재등록 잠금을 유지합니다. 원작 호스트의 영속 시도 이력이 새 ID·복구 뒤에도 중복 제출을 막습니다.

Google 직접 연결은 승인 범위에 따라 비활성화되어 있습니다. Excel·CSV·붙여넣기를 사용하세요. 수동 화면 요소 지정은 이 모듈에서 제공하지 않으며 현재 승인된 Gentoo 연결을 사용합니다.

## 저장과 한도

초안·보관·수신자 표시·양식·항목별 상태는 storage 캡에만 저장합니다. 240KiB 미만 UTF-8 청크와 마지막 포인터 전환을 사용하며 실패 시 이전 상태를 유지합니다. 기록을 자동으로 잘라 저장하지 않습니다. JSON 백업은 이 모듈의 자료이며 원작 전체 SQLite DB나 연결 프로필 백업이 아닙니다. 업무 CSV에는 수신자와 내용이 포함됩니다. 실행 기록은 이 모듈의 상태이며 전체 쿨메신저 기록을 조회하지 않습니다.

| 범위           | 한도                                                   |
| -------------- | ------------------------------------------------------ |
| 목록·계획      | 미보관 500건, 전체 저장 2,500건·2MiB                   |
| 저장 양식      | 30개                                                   |
| 표             | 10MiB, 제목행 포함 510행·128열, XLSX 100,000셀·100시트 |
| XLSX 압축 해제 | 전체 64MiB, XML 하나 16MiB, 4,096개 항목               |
| 메시지         | 제목 200자·본문 30,000자·수신자 100명·첨부 10개        |
| 호스트 계획    | 직렬화 512KiB·제목/본문 합계 256KiB·5분 만료           |
| 시각           | 지금부터 10분 뒤~달력상 3개월, 한국시간                |
| 대량 화면      | 목록 50건·입력/미리보기 25건씩                         |

필수 캡은 messaging ^1.0 / storage ^1.0 / fs ^1.4.0입니다. connect·recipients·plan·start·status·cancel 여섯 typed SDK 메서드만 사용합니다. 압축 해제와 CSV·붙여넣기 파싱은 Worker에서 수행합니다. 모듈에 네트워크 호출·직접 Python 실행·사용자 경로 입력은 없습니다.

합성 회귀는 반복·표·동명이인·재연결·자료 변경 충돌·저장 실패·XLSX 왕복·SDK mock 검토/확인취소/잠금을 검사합니다. mock 테스트는 실제 Gentoo 등록 성공의 증명이 아닙니다. 실제 Windows 연결·조작은 해당 PC에서 별도로 확인해야 합니다.

## 원작과 저작권

[CoolReserve](https://github.com/rochelobeJYJ/CoolReserve)의 web/app.js·batch.js와 app/core.py·recipients.py·batch.py·workflows.py를 원천으로 읽고 덱 화면·핸들 계약에 맞춰 옮겼습니다. 원작은 **Copyright (c) 2026 CoolReserve contributors, MIT License**입니다. 관리 Python 원작 코어와 LICENSE/THIRD_PARTY_NOTICES는 호스트가 고정한 별도 원천 패키지에 동봉합니다. fflate 0.8.3은 MIT입니다. 덱 모듈 코드에는 TeacherDeck GPL-3.0-only와 추가 약관이 적용되며 기존 원작 저작권과 고지를 유지합니다.

원작 MIT 고지원문:

> MIT License
>
> Copyright (c) 2026 CoolReserve contributors
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## 파일 입력과 저장 위치

메시지 작성 화면에 파일을 놓으면 첨부 목록에 추가하고 파일명과 추가 결과를 표시해요. 첨부는 10개까지 받아요. 업무자료 화면의 XLSX·CSV 드롭은 현재 업무자료로 읽고, 다른 화면에서는 예약표로 읽어요. JSON 백업은 한 번에 한 파일씩 복구해요. 처리 중·지원하지 않는 형식·한도 초과는 화면에서 이유를 알려요. 파일을 놓은 뒤 선택창을 다시 열지 않아요.

이 화면에서 가져오기를 완료한 자료와 연결된 결과는 원본 파일의 폴더에 새 이름으로 저장해요. 다른 폴더를 명시적으로 고르면 그 폴더에 바로 저장해요. 원본 파일이 없는 새 자료·양식·복구 사본은 저장할 폴더를 선택해요. 자동 하위 폴더를 만들거나 원본 파일을 덮어쓰지 않아요.

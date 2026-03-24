# Input and Output Contracts

## 입력 계약
### 필수 입력
- PDF 파일 경로

### 허용 문서 유형
- `DIGITAL`: 텍스트 레이어가 실질적으로 사용 가능
- `SCANNED`: 텍스트 레이어가 없거나 쓸 수 없음
- `HYBRID`: 일부만 텍스트 레이어 사용 가능

### 페이지 방향 계약
- 페이지 주 방향은 아래 둘 중 하나로만 처리한다.
  - `LEFT_TO_RIGHT`
  - `TOP_TO_BOTTOM`
- 한 페이지에서 두 방향이 동시에 섞인 경우는 기본 처리 대상이 아니다.

### 선택 입력
- 페이지 범위
- OCR 엔진 선택
- 방향 강제 지정
- 검수 시트 레이아웃 선택
- 신뢰도 임계값
- 디버그 산출물 저장 여부
- 스캔본 전처리 활성화 여부
- OCR 우선 처리 여부
- 난인식 텍스트 스킵 임계값

## 중간 산출물 계약
- `document_classification.json`
- `step1_text_layer.json`
- `step1_5_preprocessed_images.json`
- `step2_vision_layout.json`
- `step3_reconciled.json`
- `step4_skip_resolution.json`
- `summary.json`
- `review_required_pages.json`
- 로그 파일

## 최종 출력 계약
### CSV
- 파일명: `final_output.csv`
- 텍스트 순서는 최종 복원 순서와 동일해야 한다.
- 좌→우 페이지는 1행 기준 순차 배치
- 상→하 페이지는 1열 기준 순차 배치
- 스킵 항목은 빈 슬롯 또는 명시적 상태값으로 표시 가능

### XLSX
- 파일명: `review_output.xlsx`
- 각 페이지의 원본 이미지와 텍스트를 같은 시트에서 동시에 검수할 수 있어야 한다.
- 상→하 페이지: 왼쪽 이미지 / 오른쪽 텍스트
- 좌→우 페이지: 위쪽 이미지 / 아래쪽 텍스트
- 필요 시 순번, confidence, skip flag, document type을 추가한다.
- 스킵 항목은 식별 가능해야 한다.

## 절대 바꾸지 말아야 할 계약
- 최종 산출물은 CSV와 XLSX 둘 다 생성해야 한다.
- CSV와 XLSX의 텍스트 순서는 일치해야 한다.
- 원본 이미지와 추출 텍스트의 동시 검수 가능성을 제거하지 말아야 한다.
- 스킵 여부를 숨기지 말아야 한다.

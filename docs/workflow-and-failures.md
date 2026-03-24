# Workflow and Failure Policy

## 고정 워크플로우
1. 입력 수집 및 작업 초기화
2. 문서 유형 판정 및 처리 경로 결정
3. 텍스트 레이어 추출
4. 스캔본 전처리
5. OCR/비전 기반 텍스트 및 위치 분석
6. 정합 및 읽기 순서 복원
7. 난인식 텍스트 스킵 처리 및 실행 지속
8. 품질 판정 및 최종 분기
9. CSV 생성
10. 검수용 XLSX 생성
11. 로그/중간 산출물 저장
12. QA 회귀 테스트 실행

## 단계별 핵심 규칙
### Step 0
- PDF 여부를 검증한다.
- 작업 디렉터리와 실행 컨텍스트를 초기화한다.

### Step 0.5
- 문서/페이지를 `DIGITAL`, `SCANNED`, `HYBRID`로 분류한다.
- 분류가 애매하면 기본값은 OCR 우선 경로다.

### Step 1
- DIGITAL/HYBRID에서 텍스트 레이어 결과를 최대한 보존한다.
- 텍스트 레이어 부재도 명시적으로 기록한다.

### Step 1.5
- 스캔본 또는 OCR 우선 페이지에 대해 grayscale, threshold, denoise, deskew, contrast enhancement를 적용할 수 있다.
- 전처리 실패 시 원본 렌더 이미지로 fallback한다.

### Step 2
- OCR/비전으로 텍스트, bbox, orientation 후보, rotated flag를 확보한다.
- 스캔본에서는 이 결과를 주 추출 소스로 사용한다.

### Step 3
- DIGITAL: 문자 자체는 Step 1 우선, 위치/배치는 Step 2 우선
- SCANNED: 문자와 위치 모두 Step 2 우선
- HYBRID: 페이지/영역별 가변 적용
- 최종 결과에는 `order_index`, `reading_direction`, `confidence`, `review_required`를 포함한다.

### Step 4
- confidence가 임계값보다 낮은 텍스트만 `SKIPPED` 또는 `UNKNOWN`으로 표시한다.
- 나머지 텍스트는 계속 처리한다.
- 전체 파이프라인을 멈추지 않는다.

### Step 5
- 자동 확정 가능 결과와 검토 필요 결과를 구분한다.
- 전체 품질이 낮아도 결과 생성은 계속하되 자동 승인하지 않는다.

### Step 6~7
- CSV와 XLSX를 모두 생성한다.
- XLSX는 검수 편의성을 기준으로 레이아웃을 선택한다.

## 실패 처리 원칙
### 실행 단계
- 일부 텍스트 실패는 전체 중단 사유가 아니다.
- 텍스트 단위 부분 스킵 후 계속 진행한다.

### 자동 재시도 기본값
- OCR 오류: 최대 2회
- 파일 쓰기 실패: 최대 1회
- 파싱 예외: 최대 1회
- 전처리 실패: 최대 1회

### 에스컬레이션 조건
- 정답 데이터 정의가 불명확한 경우
- QA 기준이 충돌하는 경우
- 반복 수정에도 동일 케이스가 장기 미해결인 경우

## 상태 전이
- `RECEIVED -> CLASSIFIED -> TEXT_LAYER_EXTRACTED -> PREPROCESSED_FOR_OCR -> VISION_ANALYZED -> RECONCILED -> SKIP_RESOLVED -> VALIDATED -> EXPORT_COMPLETED`
- 경고 포함 완료: `VALIDATED -> APPROVED_WITH_WARNINGS -> EXPORT_COMPLETED`
- QA: `EXPORT_COMPLETED -> QA_PENDING -> QA_RUNNING -> QA_PASSED|QA_FAILED`
- 치명 오류: `FAILED`

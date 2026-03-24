# CLAUDE.md

## 프로젝트 정의
- 이 프로젝트는 디지털 PDF, 스캔본 PDF, 하이브리드 PDF에서 일렬 배치된 텍스트를 추출하고, 사람이 읽는 순서로 복원한 뒤, 검수 가능한 CSV/XLSX를 생성하는 로컬 우선 시스템이다.
- 최종 목표는 **텍스트 정확도 + 순서 정확도**를 높게 유지하면서, 사용자가 XLSX에서 원본 이미지와 결과를 빠르게 대조 검수할 수 있게 만드는 것이다.

## 범위
### 포함
- 디지털 PDF 텍스트 레이어 추출
- 스캔본 PDF OCR/비전 처리
- 하이브리드 PDF 처리
- 방향 판정: 좌→우 / 상→하
- 90도 회전 텍스트 일부 처리
- 텍스트/위치 정합
- 읽기 순서 복원
- 검수용 CSV/XLSX 생성
- 샘플 PDF 기반 QA 회귀 테스트

### 제외
- 일반 다단 문서의 완전한 읽기 순서 복원
- 복잡한 표 구조 전체 복원
- 자유 배치 멀티컬럼 일반화
- 손글씨 문서 지원
- 저품질 스캔본의 완전 자동 복원 보장
- 서버 상시 운영형 SaaS 설계

## 우선순위
1. 텍스트 정확도와 순서 정확도를 동시에 지킬 것
2. 기존 입출력 계약을 유지할 것
3. 사용자가 XLSX에서 빠르게 검수할 수 있을 것
4. 실행 단계에서는 전체 파이프라인을 끝까지 진행할 것
5. QA 단계에서는 100% 정답 일치만 통과로 인정할 것
6. 성능/미관 개선은 위 조건을 해치지 않는 범위에서만 수행할 것

## 입력 계약
- 입력은 PDF만 허용한다.
- 입력 PDF는 DIGITAL / SCANNED / HYBRID 중 하나로 분류한다.
- 주 읽기 방향은 페이지 기준으로 `LEFT_TO_RIGHT` 또는 `TOP_TO_BOTTOM` 중 하나로 본다.
- 한 페이지 안에 두 방향이 동시에 강하게 섞인 문서는 기본 범위 밖이다.
- 일부 페이지 또는 일부 텍스트는 끝까지 인식 실패할 수 있다.
- 선택 옵션은 페이지 범위, OCR 엔진, 방향 강제 지정, 신뢰도 임계값, 디버그 저장 여부, 스캔본 전처리 여부를 포함할 수 있다.

## 출력 계약
- 반드시 `final_output.csv`와 `review_output.xlsx`를 생성한다.
- CSV와 XLSX의 텍스트 순서는 서로 일치해야 한다.
- 좌→우 페이지는 텍스트를 1행 기준으로 순차 배치한다.
- 상→하 페이지는 텍스트를 1열 기준으로 순차 배치한다.
- XLSX에는 반드시 원본 PDF 페이지 이미지와 최종 텍스트를 같은 시트에 함께 배치한다.
- 상→하 페이지는 `왼쪽 이미지 / 오른쪽 텍스트` 레이아웃을 사용한다.
- 좌→우 페이지는 `위쪽 이미지 / 아래쪽 텍스트` 레이아웃을 사용한다.
- 스킵된 텍스트가 있으면 결과물과 로그에 명시적으로 표시한다.

## 처리 원칙
- Step 0에서 입력/옵션을 검증하고 작업 디렉터리를 초기화한다.
- Step 0.5에서 문서 유형을 판정하고 페이지별 처리 전략을 결정한다.
- DIGITAL은 문자 정확성 충돌 시 Step 1 텍스트 레이어를 우선한다.
- SCANNED는 문자 자체를 Step 2 OCR 결과 우선으로 처리한다.
- HYBRID는 페이지 또는 영역별로 Step 1/Step 2 우선순위를 가변 적용한다.
- 위치와 배치 순서는 Step 2 이미지 분석 결과를 우선 반영한다.
- 방향 판정은 `LEFT_TO_RIGHT` 또는 `TOP_TO_BOTTOM`만 허용한다.
- 불확실한 일부 텍스트 때문에 전체 실행을 멈추지 말고 해당 텍스트만 `SKIPPED` 또는 `UNKNOWN`으로 처리한 뒤 다음 텍스트를 계속 처리한다.
- 실행 단계 스킵은 허용하지만, QA 단계에서는 스킵을 성공으로 인정하지 않는다.

## 판단과 코드의 역할 분리

### Claude Code(이 대화) 담당
Claude Code 자체가 LLM이므로, API를 별도 호출하지 않는다.
파이프라인을 단계별로 실행하면서 판단이 필요한 시점에 직접 개입한다.

| 단계 | 파일 | Claude Code 역할 | 입력 파일 | 출력 파일 |
|------|------|-----------------|-----------|-----------|
| Step 0.5 | `step0_5_classify.py` | 페이지 이미지를 직접 보고 DIGITAL/SCANNED/HYBRID + 읽기방향 판정 | `classify_input.json` | `classify_decision.json` |
| Step 2   | `step2_vision.py`     | 페이지 이미지에서 텍스트와 위치(bbox) 직접 추출 (OCR) | `vision_input.json` | `vision_output.json` |
| Step 3   | `step3_reconcile.py`  | 텍스트 레이어 샘플 vs OCR 샘플 비교 후 소스 결정 | `reconcile_input.json` | `reconcile_decision.json` |
| Step 5   | `step5_validate.py`   | 추출 통계+샘플 보고 품질 평가 후 VALIDATED/APPROVED_WITH_WARNINGS 결정 | `validate_input.json` | `validate_decision.json` |
| QA       | `qa/run_qa.py`        | 불일치 원인 분류 및 수정 피드백 생성 | `qa_failure_input.json` | `qa_failure_analysis.json` |

모든 판단 단계는 decision 파일이 없으면 규칙 기반 fallback으로 자동 진행된다.

### 스크립트 담당 (결정론적, 반복 가능)
- PDF 파싱 및 텍스트 레이어 추출 (pdfplumber)
- PDF → 이미지 렌더링 (PyMuPDF)
- 스캔 이미지 전처리 (OpenCV)
- 읽기 순서 정렬 (bbox 기반)
- CSV / XLSX 생성
- QA 비교 및 리포트 저장

### 추가 API 키 불필요
- `anthropic` SDK 미사용 – 이 대화(Claude Code) 자체가 LLM
- `pytesseract`, `easyocr` 미사용 – OCR은 Claude Code가 이미지 직독

## 수정 원칙
- 기존 구조를 유지하면서 필요한 파일만 최소 범위로 수정한다.
- 동작 변경과 무관한 대규모 리네이밍, 구조 개편, 미관 목적 리팩토링은 하지 않는다.
- 입출력 계약, 상태값, 산출물 파일명, QA 구조는 명시적 요구 없이 바꾸지 않는다.
- QA 샘플/정답 데이터는 구현 코드와 분리된 디렉터리에서 관리한다.

## 금지사항
- 사용자가 요구하지 않은 UI/리포트 전면 재설계 금지
- 입출력 포맷 임의 변경 금지
- 검증 없이 핵심 추출 로직 교체 금지
- 스킵 항목을 조용히 누락 처리 금지
- QA 실패 상태를 완료로 간주하는 행위 금지
- 문서 수정 후 QA를 생략하는 행위 금지

## QA 게이트
- 모든 코드 수정 또는 문서 수정 후 반드시 QA 회귀 테스트를 실행한다.
- QA는 샘플 PDF 입력 → 실제 추출 → 정답 비교 → 실패 리포트 생성까지 포함해야 한다.
- 등록된 모든 샘플이 **100% 정답 일치**해야만 `QA_PASSED`로 본다.
- QA 단계에서는 skipped item이 하나라도 남아 있으면 실패다.
- QA 실패 시 자동 승인하지 말고, 실패 원인과 수정 피드백을 남긴 뒤 다시 수정하고 재실행한다.

## 상태 규칙
- 정상 흐름: `RECEIVED -> CLASSIFIED -> TEXT_LAYER_EXTRACTED -> PREPROCESSED_FOR_OCR -> VISION_ANALYZED -> RECONCILED -> SKIP_RESOLVED -> VALIDATED -> EXPORT_COMPLETED`
- 경고 포함 실행 완료: `VALIDATED -> APPROVED_WITH_WARNINGS -> EXPORT_COMPLETED`
- QA 흐름: `EXPORT_COMPLETED -> QA_PENDING -> QA_RUNNING -> QA_PASSED|QA_FAILED`
- 치명 오류는 어느 단계에서든 `FAILED`로 전이한다.

## 참조 문서
- `docs/project-scope-and-rules.md`
- `docs/io-contracts.md`
- `docs/workflow-and-failures.md`
- `docs/testing-and-qa.md`
- `.claude/agents/main-orchestrator.md`
- `.claude/agents/layout-reconstructor.md`
- `.claude/agents/quality-validator.md`
- `.claude/agents/report-generator.md`
- `.claude/agents/qa-agent.md`

## 폴더 구조

```
PDFTEXT_READ/
│
├── CLAUDE.md                          # 프로젝트 규칙 및 전체 폴더 구조 (이 파일)
├── main.py                            # CLI 진입점 (python main.py <pdf> [options])
├── requirements.txt                   # Python 패키지 의존성
│
├── src/                               # 핵심 구현 소스
│   ├── __init__.py
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   └── state.py                   # 상태 enum, TextBlock, PageInfo, PipelineContext 정의
│   │
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── logger.py                  # 로깅 설정 (콘솔 + 파일)
│   │   ├── image_utils.py             # PDF 페이지 렌더링, 스캔 전처리, XLSX용 리사이즈
│   │   └── llm_client.py              # Claude API 래퍼 (ask_json / ask_with_image / ask_text)
│   │
│   └── pipeline/                      # 파이프라인 단계별 스크립트
│       ├── __init__.py
│       ├── step0_init.py              # Step 0 : 입력 검증 / 작업 디렉터리 초기화
│       ├── step0_5_classify.py        # Step 0.5: 문서 유형 판정 (DIGITAL/SCANNED/HYBRID)
│       ├── step1_text_layer.py        # Step 1 : 텍스트 레이어 추출 (pdfplumber)
│       ├── step1_5_preprocess.py      # Step 1.5: 스캔 페이지 이미지 전처리 (OpenCV)
│       ├── step2_vision.py            # Step 2 : OCR 실행 (tesseract / easyocr)
│       ├── step3_reconcile.py         # Step 3 : 텍스트 레이어 + OCR 정합 및 읽기 순서 복원
│       ├── step4_skip.py              # Step 4 : 저신뢰도 텍스트 SKIPPED/UNKNOWN 처리
│       ├── step5_validate.py          # Step 5 : 품질 판정 (VALIDATED / APPROVED_WITH_WARNINGS)
│       ├── step6_csv.py               # Step 6 : final_output.csv 생성
│       └── step7_xlsx.py              # Step 7 : review_output.xlsx 생성 (이미지 + 텍스트)
│
├── docs/                              # 프로젝트 규칙 문서
│   ├── project-scope-and-rules.md
│   ├── io-contracts.md
│   ├── workflow-and-failures.md
│   └── testing-and-qa.md
│
├── .claude/                           # Claude 에이전트 정의
│   └── agents/
│       ├── main-orchestrator.md
│       ├── layout-reconstructor.md
│       ├── quality-validator.md
│       ├── report-generator.md
│       └── qa-agent.md
│
├── input/                             # PDF 입력 폴더 (--batch 모드 기본 경로)
│   └── *.pdf                          # 처리할 PDF 파일들을 여기에 넣기
│
├── qa/                                # QA 회귀 테스트 자산
│   ├── __init__.py
│   ├── run_qa.py                      # QA 실행 스크립트 (python qa/run_qa.py)
│   ├── samples/                       # 샘플 PDF 파일들 (.pdf)
│   ├── answers/                       # 정답 JSON 파일들 (<stem>.json)
│   ├── fixtures/                      # 고정 테스트 픽스처
│   └── reports/                       # QA 결과 리포트 (자동 생성)
│
└── output/                            # 실행 결과 출력 디렉터리 (자동 생성)
    └── <pdf_stem>_<timestamp>/        # 실행별 작업 디렉터리
        ├── final_output.csv           # 최종 텍스트 추출 결과 (CSV)
        ├── review_output.xlsx         # 검수용 XLSX (이미지 + 텍스트)
        ├── pipeline.log               # 실행 로그
        ├── intermediate/              # 단계별 중간 산출물 JSON
        │   ├── step0_manifest.json
        │   ├── document_classification.json
        │   ├── step1_text_layer.json
        │   ├── step1_5_preprocessed_images.json
        │   ├── step2_vision_layout.json
        │   ├── step3_reconciled.json
        │   ├── step4_skip_resolution.json
        │   ├── summary.json
        │   └── review_required_pages.json
        ├── images/                    # 렌더링된 PDF 페이지 이미지 (page_001.png …)
        └── preprocessed/              # 전처리된 페이지 이미지 (page_001_pre.png …)
```

## 실행 방법

### 설치
```bash
pip install -r requirements.txt
# Tesseract OCR 별도 설치 필요:
# Windows: https://github.com/UB-Mannheim/tesseract/wiki
# macOS:   brew install tesseract tesseract-lang
# Linux:   sudo apt install tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng
```

### 환경 변수 설정
```bash
export ANTHROPIC_API_KEY=sk-ant-...   # LLM 판정에 필수
```

### 기본 실행 (단일 파일)
```bash
python main.py document.pdf
```

### 일괄 처리 (input/ 폴더)
```bash
# PDF를 input/ 폴더에 넣고 실행
python main.py --batch

# 다른 폴더 지정
python main.py --input-dir /path/to/pdfs --output-dir /path/to/results
```

### 옵션 예시
```bash
# 페이지 범위 지정
python main.py document.pdf --pages 1-5

# OCR 강제 적용 (스캔본으로 처리)
python main.py document.pdf --ocr-priority

# 읽기 방향 강제 지정
python main.py document.pdf --force-direction TOP_TO_BOTTOM

# 신뢰도 임계값 조정 (기본 0.5)
python main.py document.pdf --confidence-threshold 0.6

# 전처리 비활성화
python main.py document.pdf --no-preprocess

# EasyOCR 사용 (설치 필요: pip install easyocr)
python main.py document.pdf --ocr-engine easyocr
```

### QA 실행
```bash
# 전체 샘플 QA
python qa/run_qa.py

# 특정 샘플만
python qa/run_qa.py --sample my_doc.pdf --verbose
```

## QA 정답 파일 형식 (qa/answers/<stem>.json)
```json
{
  "doc_type": "DIGITAL",
  "blocks": [
    {"page_num": 1, "order_index": 0, "text": "예상 텍스트"},
    {"page_num": 1, "order_index": 1, "text": "두 번째 텍스트"}
  ]
}
```

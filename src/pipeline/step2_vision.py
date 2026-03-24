"""
Step 2: OCR – 이미지에서 텍스트와 위치 추출.
[에이전트(Claude Code) 판단 영역]

스크립트 역할:
  1. 처리 대상 페이지 이미지 경로 목록을 vision_input.json 에 저장
  2. Claude가 작성한 vision_output.json 을 읽어 TextBlock 구성

Claude Code 담당:
  - vision_input.json 의 이미지를 직접 읽고 (Read 도구)
  - 각 페이지에서 텍스트와 위치(bbox: [x0,y0,x1,y1], 0~1 정규화)를 추출
  - vision_output.json 에 결과 작성

vision_output.json 형식:
{
  "pages": {
    "1": [
      {"text": "추출된 텍스트", "bbox": [0.1, 0.05, 0.9, 0.12], "confidence": 0.95, "rotated": false},
      ...
    ],
    "2": [...]
  }
}

bbox 기준: 페이지 좌상단 (0,0) ~ 우하단 (1,1), 정규화된 비율값

State transition: PREPROCESSED_FOR_OCR → VISION_ANALYZED
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from src.models.state import (
    DocumentType,
    PipelineContext,
    ProcessingStatus,
    TextBlock,
    TextStatus,
)
from src.pipeline.step1_text_layer import _sort_ltr, _sort_ttb
from src.models.state import ReadingDirection
from src.utils.logger import get_logger


def run(ctx: PipelineContext) -> PipelineContext:
    """
    OCR 대상 이미지 목록을 저장하고, Claude의 텍스트 추출 결과를 읽어 TextBlock을 구성한다.
    """
    logger = get_logger()
    logger.info("Step 2: 이미지 OCR 데이터 수집 중…")

    try:
        ocr_priority = ctx.options.get("ocr_priority", False)

        # 대상 페이지 정보 수집
        vision_input: Dict[str, Any] = {"pages": {}}
        needs_ocr_pages = []

        for pi in ctx.page_infos:
            needs_ocr = (
                pi.doc_type in (DocumentType.SCANNED.value, DocumentType.HYBRID.value)
                or ocr_priority
            )
            img_path = pi.preprocessed_image_path or pi.image_path

            vision_input["pages"][str(pi.page_num)] = {
                "needs_ocr": needs_ocr,
                "doc_type": pi.doc_type,
                "direction": pi.direction,
                "image_path": img_path,
                "preprocessed_image_path": pi.preprocessed_image_path,
                "original_image_path": pi.image_path,
            }
            if needs_ocr:
                needs_ocr_pages.append(pi.page_num)

        # vision_input.json 저장
        input_path = os.path.join(ctx.work_dir, "intermediate", "vision_input.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(vision_input, f, ensure_ascii=False, indent=2)

        logger.info(f"OCR 대상 페이지: {needs_ocr_pages}")
        logger.info(f"vision_input.json 저장 완료: {input_path}")

        if needs_ocr_pages:
            logger.info(">>> Claude Code가 아래 이미지들을 읽고 vision_output.json 을 작성해야 합니다:")
            for page_num in needs_ocr_pages:
                img = vision_input["pages"][str(page_num)].get("image_path", "")
                logger.info(f"    Page {page_num}: {img}")

        # vision_output.json 읽기 (Claude 작성)
        output_path = os.path.join(ctx.work_dir, "intermediate", "vision_output.json")
        ocr_results = _load_output(output_path, vision_input)

        # OCR 결과를 options에 임시 저장 (Step 3에서 사용)
        ctx.options["_ocr_raw"] = ocr_results

        ctx.status = ProcessingStatus.VISION_ANALYZED
        _save(ctx, ocr_results)
        logger.info(f"Step 2 완료. Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 2 실패: {exc}")
        logger.error(f"Step 2 실패: {exc}", exc_info=True)

    return ctx


# ── output JSON 로드 ──────────────────────────────────────────────────────

def _load_output(output_path: str, vision_input: dict) -> Dict[str, List[Dict]]:
    """
    Claude가 작성한 vision_output.json 을 읽는다.
    파일이 없으면 빈 결과를 반환한다.
    """
    logger = get_logger()
    if os.path.exists(output_path):
        with open(output_path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("vision_output.json 로드 완료.")
        return {k: v for k, v in data.get("pages", {}).items()}

    logger.warning("vision_output.json 없음 → OCR 결과 없이 진행 (텍스트 레이어만 사용).")
    # 빈 결과 파일 생성 (다음 실행 시 Claude가 채울 수 있도록)
    empty = {"pages": {str(pn): [] for pn in vision_input["pages"]}}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(empty, f, ensure_ascii=False, indent=2)
    return {str(pn): [] for pn in vision_input["pages"]}


def _save(ctx: PipelineContext, ocr_results: dict) -> None:
    path = os.path.join(ctx.work_dir, "intermediate", "step2_vision_layout.json")
    data = {
        "status": ctx.status,
        "ocr_source": "claude_code_vision",
        "pages": ocr_results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

"""
Step 3: Reconciliation of text layer and OCR results.
[에이전트(Claude Code) 판단 영역]

스크립트 역할:
  1. 텍스트 레이어 샘플과 OCR 샘플을 reconcile_input.json 에 저장
  2. Claude가 작성한 reconcile_decision.json 을 읽어 소스 우선순위 결정
  3. 최종 TextBlock 목록과 읽기 순서 확정

Claude Code 담당:
  - reconcile_input.json 의 샘플을 비교하고
  - 각 페이지별로 use_ocr_text: true/false 결정
  - reconcile_decision.json 에 결과 작성

우선순위 규칙:
  DIGITAL : 문자 = 텍스트 레이어, 위치 = OCR bbox
  SCANNED : 문자 + 위치 = OCR
  HYBRID  : Claude가 페이지별로 판단

State transition: VISION_ANALYZED → RECONCILED
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from src.models.state import (
    DocumentType,
    PipelineContext,
    ProcessingStatus,
    ReadingDirection,
    TextBlock,
    TextStatus,
)
from src.pipeline.step1_text_layer import _sort_ltr, _sort_ttb
from src.utils.logger import get_logger

_HYBRID_OCR_THRESHOLD = 0.15
_SAMPLE_SIZE = 5


def run(ctx: PipelineContext) -> PipelineContext:
    """
    텍스트 레이어 + OCR 샘플을 저장하고, Claude의 정합 결정을 읽어 최종 블록을 구성한다.
    """
    logger = get_logger()
    logger.info("Step 3: 텍스트 소스 정합 중…")

    try:
        ocr_raw: Dict[str, List[Dict]] = ctx.options.pop("_ocr_raw", {})

        tl_by_page: Dict[int, List[TextBlock]] = {}
        for tb in ctx.text_blocks:
            if tb.source == "text_layer":
                tl_by_page.setdefault(tb.page_num, []).append(tb)

        # reconcile_input.json 저장 (Claude 판단 재료)
        input_data = {}
        for pi in ctx.page_infos:
            tl_blocks = tl_by_page.get(pi.page_num, [])
            ocr_words = ocr_raw.get(str(pi.page_num), [])
            input_data[str(pi.page_num)] = {
                "doc_type": pi.doc_type,
                "text_coverage": pi.text_coverage,
                "tl_sample": [b.text for b in tl_blocks[:_SAMPLE_SIZE]],
                "ocr_sample": [w.get("text", "") for w in ocr_words[:_SAMPLE_SIZE]],
                "tl_count": len(tl_blocks),
                "ocr_count": len(ocr_words),
            }

        input_path = os.path.join(ctx.work_dir, "intermediate", "reconcile_input.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump({"pages": input_data}, f, ensure_ascii=False, indent=2)
        logger.info(f"정합 판단 재료 저장: {input_path}")
        logger.info(">>> Claude Code가 reconcile_input.json 을 보고 reconcile_decision.json 을 작성해야 합니다.")

        # reconcile_decision.json 읽기 (Claude 작성)
        decision_path = os.path.join(ctx.work_dir, "intermediate", "reconcile_decision.json")
        decision = _load_decision(decision_path, input_data)

        # 결정에 따라 TextBlock 구성
        reconciled: List[TextBlock] = []
        order_idx = 0

        for pi in ctx.page_infos:
            tl_blocks = tl_by_page.get(pi.page_num, [])
            ocr_words = ocr_raw.get(str(pi.page_num), [])
            page_decision = decision.get(str(pi.page_num), {})
            use_ocr = bool(page_decision.get("use_ocr_text", _fallback_use_ocr(pi)))

            if use_ocr and ocr_words:
                page_blocks = _blocks_from_ocr(ocr_words, pi.page_num, pi.direction)
            elif tl_blocks:
                page_blocks = _blocks_from_text_layer(tl_blocks, ocr_words, pi.direction)
            elif ocr_words:
                page_blocks = _blocks_from_ocr(ocr_words, pi.page_num, pi.direction)
                ctx.add_warning(f"Page {pi.page_num}: 텍스트 레이어 없음 → OCR fallback.")
            else:
                ctx.add_warning(f"Page {pi.page_num}: 두 소스 모두 비어 있음.")
                page_blocks = []

            sorted_blocks = _sort_blocks(page_blocks, pi.direction)
            for tb in sorted_blocks:
                tb.order_index = order_idx
                order_idx += 1
            reconciled.extend(sorted_blocks)

            logger.info(f"  Page {pi.page_num}: {len(sorted_blocks)}개 블록 "
                        f"({'OCR' if use_ocr else '텍스트 레이어'} 우선) "
                        f"[{page_decision.get('reason', '')}]")

        ctx.text_blocks = reconciled
        ctx.status = ProcessingStatus.RECONCILED
        _save(ctx)
        logger.info(f"Step 3 완료. 총 블록: {len(reconciled)}, Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 3 실패: {exc}")
        logger.error(f"Step 3 실패: {exc}", exc_info=True)

    return ctx


# ── decision JSON 로드 ────────────────────────────────────────────────────

def _load_decision(decision_path: str, input_data: dict) -> dict:
    logger = get_logger()
    if os.path.exists(decision_path):
        with open(decision_path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("reconcile_decision.json 로드 완료.")
        return data.get("pages", data)

    logger.warning("reconcile_decision.json 없음 → coverage 기반 fallback 적용.")
    fallback = {}
    for page_num_str, d in input_data.items():
        use_ocr = d["doc_type"] == "SCANNED" or d["text_coverage"] < _HYBRID_OCR_THRESHOLD
        fallback[page_num_str] = {
            "use_ocr_text": use_ocr,
            "reason": "fallback: reconcile_decision.json 없음",
        }
    with open(decision_path, "w", encoding="utf-8") as f:
        json.dump({"source": "fallback", "pages": fallback}, f, ensure_ascii=False, indent=2)
    return fallback


def _fallback_use_ocr(pi) -> bool:
    return pi.doc_type == DocumentType.SCANNED.value or pi.text_coverage < _HYBRID_OCR_THRESHOLD


# ── 블록 생성 헬퍼 ────────────────────────────────────────────────────────

def _blocks_from_ocr(ocr_words, page_num, direction):
    blocks = []
    for w in ocr_words:
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        blocks.append(TextBlock(
            order_index=0, page_num=page_num, text=text,
            bbox=w.get("bbox", [0, 0, 1, 1]),
            confidence=float(w.get("confidence", 0.0)),
            reading_direction=direction, status=TextStatus.OK.value,
            source="ocr", review_required=False, rotated=w.get("rotated", False),
        ))
    return blocks


def _blocks_from_text_layer(tl_blocks, ocr_words, direction):
    if not ocr_words:
        return list(tl_blocks)
    ocr_bboxes = [w["bbox"] for w in ocr_words if w.get("bbox")]
    result = []
    for tb in tl_blocks:
        best = _closest_bbox(tb.bbox, ocr_bboxes)
        result.append(TextBlock(
            order_index=0, page_num=tb.page_num, text=tb.text,
            bbox=best if best else tb.bbox,
            confidence=tb.confidence, reading_direction=direction,
            status=tb.status, source=tb.source,
            review_required=tb.review_required, rotated=tb.rotated,
        ))
    return result


def _closest_bbox(ref, candidates, max_dist=0.05):
    rcx, rcy = (ref[0] + ref[2]) / 2, (ref[1] + ref[3]) / 2
    best, bd = None, float("inf")
    for bb in candidates:
        d = (((bb[0] + bb[2]) / 2 - rcx) ** 2 + ((bb[1] + bb[3]) / 2 - rcy) ** 2) ** 0.5
        if d < bd:
            bd, best = d, bb
    return best if best and bd <= max_dist else None


def _sort_blocks(blocks, direction):
    if not blocks:
        return blocks
    wd = [{"text": b.text, "bbox": b.bbox, "_block": b} for b in blocks]
    sd = _sort_ltr(wd) if direction == ReadingDirection.LEFT_TO_RIGHT.value else _sort_ttb(wd)
    return [d["_block"] for d in sd]


def _save(ctx):
    path = os.path.join(ctx.work_dir, "intermediate", "step3_reconciled.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "status": ctx.status,
            "total_blocks": len(ctx.text_blocks),
            "blocks": [tb.to_dict() for tb in ctx.text_blocks],
        }, f, ensure_ascii=False, indent=2)

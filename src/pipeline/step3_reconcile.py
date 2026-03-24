"""
Step 3: Reconciliation of text layer and OCR results.
에이전트(LLM) 담당 영역.

Claude가 텍스트 레이어 샘플과 OCR 샘플을 비교하고
어느 소스를 신뢰할지 페이지별로 판단한다.

우선순위 규칙:
  DIGITAL : 문자 = Step 1 (텍스트 레이어), 위치 = Step 2 (OCR bbox)
  SCANNED : 문자 + 위치 = Step 2 (OCR)
  HYBRID  : LLM이 페이지별로 판단

LLM 실패 시 coverage 기반 fallback.

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
from src.utils.llm_client import ask_json
from src.utils.logger import get_logger

_HYBRID_OCR_THRESHOLD = 0.15   # 이 값 미만이면 OCR 우선 (fallback용)
_SAMPLE_SIZE = 5                # LLM에 전달할 샘플 블록 수

_RECONCILE_PROMPT = """\
당신은 PDF 텍스트 추출 전문가입니다.
한 PDF 페이지에서 두 가지 소스로 텍스트를 추출했습니다.

문서 유형: {doc_type}
텍스트 레이어 커버리지: {coverage:.1%}

[텍스트 레이어 샘플 (최대 {n}개)]
{tl_sample}

[OCR 결과 샘플 (최대 {n}개)]
{ocr_sample}

판단 기준:
- DIGITAL 문서에서 텍스트 레이어가 있으면 텍스트 레이어의 문자가 더 정확합니다.
- SCANNED 문서에서는 OCR 결과가 유일한 소스입니다.
- HYBRID에서는 텍스트 품질(깨짐, 공백 오류, 인코딩 문제 여부)을 비교해서 판단하세요.

아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{
  "use_ocr_text": true | false,
  "reason": "판단 근거 한 문장"
}}

use_ocr_text=true  → OCR 결과를 주 텍스트 소스로 사용
use_ocr_text=false → 텍스트 레이어를 주 텍스트 소스로 사용
"""


def run(ctx: PipelineContext) -> PipelineContext:
    """LLM을 사용해 텍스트 레이어와 OCR 결과를 정합한다."""
    logger = get_logger()
    logger.info("Step 3: LLM으로 텍스트 소스 정합 중…")

    try:
        ocr_raw: Dict[str, List[Dict]] = ctx.options.pop("_ocr_raw", {})

        # 텍스트 레이어 블록을 페이지별로 정리
        tl_by_page: Dict[int, List[TextBlock]] = {}
        for tb in ctx.text_blocks:
            if tb.source == "text_layer":
                tl_by_page.setdefault(tb.page_num, []).append(tb)

        reconciled: List[TextBlock] = []
        order_idx = 0

        for pi in ctx.page_infos:
            tl_blocks = tl_by_page.get(pi.page_num, [])
            ocr_words = ocr_raw.get(str(pi.page_num), [])

            # LLM에게 소스 우선순위 판단 위임
            use_ocr = _decide_source_with_llm(pi, tl_blocks, ocr_words, logger)

            if use_ocr and ocr_words:
                page_blocks = _blocks_from_ocr(ocr_words, pi.page_num, pi.direction)
            elif tl_blocks:
                page_blocks = _blocks_from_text_layer(tl_blocks, ocr_words, pi.direction)
            elif ocr_words:
                page_blocks = _blocks_from_ocr(ocr_words, pi.page_num, pi.direction)
                ctx.add_warning(f"Page {pi.page_num}: 텍스트 레이어 없음 → OCR로 fallback.")
            else:
                ctx.add_warning(f"Page {pi.page_num}: 두 소스 모두 비어 있음.")
                page_blocks = []

            sorted_blocks = _sort_blocks(page_blocks, pi.direction)
            for tb in sorted_blocks:
                tb.order_index = order_idx
                order_idx += 1
            reconciled.extend(sorted_blocks)

            logger.info(
                f"  Page {pi.page_num}: {len(sorted_blocks)}개 블록 "
                f"({'OCR' if use_ocr else '텍스트 레이어'} 우선)"
            )

        ctx.text_blocks = reconciled
        ctx.status = ProcessingStatus.RECONCILED
        _save(ctx)
        logger.info(f"Step 3 완료. 총 블록: {len(reconciled)}, Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 3 실패: {exc}")
        logger.error(f"Step 3 실패: {exc}", exc_info=True)

    return ctx


# ── LLM 소스 판단 ─────────────────────────────────────────────────────────

def _decide_source_with_llm(pi, tl_blocks, ocr_words, logger) -> bool:
    """
    LLM에게 이 페이지의 텍스트 소스를 OCR로 할지 텍스트 레이어로 할지 물어본다.
    SCANNED는 무조건 OCR, DIGITAL은 텍스트 레이어 우선 – LLM은 HYBRID와 애매한 경우 판단.
    """
    # 명확한 케이스: LLM 불필요
    if pi.doc_type == DocumentType.SCANNED.value:
        return True
    if pi.doc_type == DocumentType.DIGITAL.value and tl_blocks:
        return False

    # HYBRID 또는 판단 애매한 경우 → LLM 판단
    tl_sample = _format_sample(
        [{"text": b.text} for b in tl_blocks[:_SAMPLE_SIZE]]
    )
    ocr_sample = _format_sample(ocr_words[:_SAMPLE_SIZE])

    if not tl_sample and not ocr_sample:
        return False

    prompt = _RECONCILE_PROMPT.format(
        doc_type=pi.doc_type,
        coverage=pi.text_coverage,
        n=_SAMPLE_SIZE,
        tl_sample=tl_sample or "(없음)",
        ocr_sample=ocr_sample or "(없음)",
    )

    fallback_use_ocr = pi.text_coverage < _HYBRID_OCR_THRESHOLD
    fallback = {"use_ocr_text": fallback_use_ocr, "reason": "LLM 실패 – coverage 기반 fallback"}

    try:
        result = ask_json(prompt, fallback=None)
        use_ocr = bool(result.get("use_ocr_text", fallback_use_ocr))
        logger.debug(f"  Page {pi.page_num} LLM 정합 근거: {result.get('reason', '')}")
        return use_ocr
    except Exception as exc:
        logger.warning(f"  Page {pi.page_num}: LLM 정합 실패 ({exc}), fallback 적용.")
        return fallback_use_ocr


def _format_sample(words: List[Dict]) -> str:
    """샘플 단어 목록을 LLM 전달용 텍스트로 변환."""
    if not words:
        return ""
    return "\n".join(f"  - {w.get('text', '')}" for w in words[:_SAMPLE_SIZE])


# ── 블록 생성 헬퍼 ────────────────────────────────────────────────────────

def _blocks_from_ocr(
    ocr_words: List[Dict[str, Any]], page_num: int, direction: str
) -> List[TextBlock]:
    blocks = []
    for w in ocr_words:
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        blocks.append(TextBlock(
            order_index=0,
            page_num=page_num,
            text=text,
            bbox=w.get("bbox", [0, 0, 1, 1]),
            confidence=float(w.get("confidence", 0.0)),
            reading_direction=direction,
            status=TextStatus.OK.value,
            source="ocr",
            review_required=False,
            rotated=w.get("rotated", False),
        ))
    return blocks


def _blocks_from_text_layer(
    tl_blocks: List[TextBlock],
    ocr_words: List[Dict[str, Any]],
    direction: str,
) -> List[TextBlock]:
    """텍스트 레이어 문자 + OCR bbox 위치 보정."""
    if not ocr_words:
        return list(tl_blocks)

    ocr_bboxes = [w["bbox"] for w in ocr_words if w.get("bbox")]
    result = []
    for tb in tl_blocks:
        best_bbox = _closest_bbox(tb.bbox, ocr_bboxes)
        result.append(TextBlock(
            order_index=0,
            page_num=tb.page_num,
            text=tb.text,
            bbox=best_bbox if best_bbox else tb.bbox,
            confidence=tb.confidence,
            reading_direction=direction,
            status=tb.status,
            source=tb.source,
            review_required=tb.review_required,
            rotated=tb.rotated,
        ))
    return result


def _closest_bbox(ref: List[float], candidates: List[List[float]], max_dist: float = 0.05):
    ref_cx = (ref[0] + ref[2]) / 2
    ref_cy = (ref[1] + ref[3]) / 2
    best, best_dist = None, float("inf")
    for bb in candidates:
        d = ((((bb[0] + bb[2]) / 2) - ref_cx) ** 2 + (((bb[1] + bb[3]) / 2) - ref_cy) ** 2) ** 0.5
        if d < best_dist:
            best_dist, best = d, bb
    return best if best and best_dist <= max_dist else None


def _sort_blocks(blocks: List[TextBlock], direction: str) -> List[TextBlock]:
    if not blocks:
        return blocks
    word_dicts = [{"text": b.text, "bbox": b.bbox, "_block": b} for b in blocks]
    sorted_dicts = (
        _sort_ltr(word_dicts)
        if direction == ReadingDirection.LEFT_TO_RIGHT.value
        else _sort_ttb(word_dicts)
    )
    return [d["_block"] for d in sorted_dicts]


def _save(ctx: PipelineContext) -> None:
    path = os.path.join(ctx.work_dir, "intermediate", "step3_reconciled.json")
    data = {
        "status": ctx.status,
        "reconciler": "llm",
        "total_blocks": len(ctx.text_blocks),
        "blocks": [tb.to_dict() for tb in ctx.text_blocks],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

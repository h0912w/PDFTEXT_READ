"""
Step 3: Reconciliation of text layer and OCR results.

Priority rules:
  DIGITAL : text = Step 1 (text layer), position = Step 2 (OCR bbox)
  SCANNED : text + position = Step 2 (OCR)
  HYBRID  : per-page decision based on text_coverage

After reconciliation, all TextBlocks are assigned final order_index values
according to the page reading direction.

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

# Text-coverage threshold below which we prefer OCR even on HYBRID pages
_HYBRID_OCR_THRESHOLD = 0.15


def run(ctx: PipelineContext) -> PipelineContext:
    """Merge text-layer and OCR sources into final TextBlocks."""
    logger = get_logger()
    logger.info("Step 3: Reconciling text layer and OCR results…")

    try:
        ocr_raw: Dict[str, List[Dict]] = ctx.options.pop("_ocr_raw", {})

        # Rebuild text-layer lookup: page_num → [word_dict]
        tl_by_page: Dict[int, List[TextBlock]] = {}
        for tb in ctx.text_blocks:
            if tb.source == "text_layer":
                tl_by_page.setdefault(tb.page_num, []).append(tb)

        reconciled: List[TextBlock] = []
        order_idx = 0

        for pi in ctx.page_infos:
            tl_blocks = tl_by_page.get(pi.page_num, [])
            ocr_words = ocr_raw.get(str(pi.page_num), [])

            # Decide per-page strategy
            use_ocr_text = _should_use_ocr_text(pi)

            if use_ocr_text and ocr_words:
                # SCANNED or low-coverage HYBRID: use OCR entirely
                page_blocks = _blocks_from_ocr(ocr_words, pi.page_num, pi.direction)
            elif tl_blocks:
                # DIGITAL or high-coverage HYBRID: use text layer text
                # Enhance position with OCR bbox if available
                page_blocks = _blocks_from_text_layer(tl_blocks, ocr_words, pi.direction)
            elif ocr_words:
                # Fallback: text layer is empty, use OCR
                page_blocks = _blocks_from_ocr(ocr_words, pi.page_num, pi.direction)
                ctx.add_warning(f"Page {pi.page_num}: text layer empty, fell back to OCR.")
            else:
                # Both empty
                ctx.add_warning(f"Page {pi.page_num}: no text from either source.")
                page_blocks = []

            # Apply reading-order sort and assign order_index
            sorted_blocks = _sort_blocks(page_blocks, pi.direction)
            for tb in sorted_blocks:
                tb.order_index = order_idx
                order_idx += 1
            reconciled.extend(sorted_blocks)

            logger.info(
                f"  Page {pi.page_num}: {len(sorted_blocks)} block(s) "
                f"({'OCR' if use_ocr_text else 'text_layer'} priority)."
            )

        ctx.text_blocks = reconciled
        ctx.status = ProcessingStatus.RECONCILED
        _save(ctx)
        logger.info(f"Step 3 complete. Total blocks: {len(reconciled)}, Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 3 failed: {exc}")
        logger.error(f"Step 3 failed: {exc}", exc_info=True)

    return ctx


# ── Strategy helpers ──────────────────────────────────────────────────────

def _should_use_ocr_text(pi) -> bool:
    """Decide whether to use OCR as primary text source for this page."""
    if pi.doc_type == DocumentType.SCANNED.value:
        return True
    if pi.doc_type == DocumentType.HYBRID.value and pi.text_coverage < _HYBRID_OCR_THRESHOLD:
        return True
    return False


def _blocks_from_ocr(
    ocr_words: List[Dict[str, Any]], page_num: int, direction: str
) -> List[TextBlock]:
    """Convert raw OCR word dicts to TextBlocks."""
    blocks = []
    for w in ocr_words:
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        blocks.append(TextBlock(
            order_index=0,  # Will be assigned later
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
    """
    Use text-layer text. If OCR bboxes are available, snap each text-layer
    block to the spatially closest OCR bbox for more accurate positioning.
    """
    if not ocr_words:
        return list(tl_blocks)

    # Build OCR bbox lookup (list of [x0,y0,x1,y1])
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


def _closest_bbox(
    ref_bbox: List[float], candidates: List[List[float]], max_dist: float = 0.05
) -> List[float]:
    """Return the candidate bbox whose center is closest to ref_bbox center."""
    ref_cx = (ref_bbox[0] + ref_bbox[2]) / 2
    ref_cy = (ref_bbox[1] + ref_bbox[3]) / 2

    best = None
    best_dist = float("inf")
    for bb in candidates:
        cx = (bb[0] + bb[2]) / 2
        cy = (bb[1] + bb[3]) / 2
        dist = ((cx - ref_cx) ** 2 + (cy - ref_cy) ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best = bb

    if best and best_dist <= max_dist:
        return best
    return ref_bbox


def _sort_blocks(blocks: List[TextBlock], direction: str) -> List[TextBlock]:
    """Sort TextBlocks into reading order."""
    if not blocks:
        return blocks

    # Convert to word-dicts for reuse of step1 sort helpers
    word_dicts = [{"text": b.text, "bbox": b.bbox, "_block": b} for b in blocks]

    if direction == ReadingDirection.LEFT_TO_RIGHT.value:
        sorted_dicts = _sort_ltr(word_dicts)
    else:
        sorted_dicts = _sort_ttb(word_dicts)

    return [d["_block"] for d in sorted_dicts]


def _save(ctx: PipelineContext) -> None:
    path = os.path.join(ctx.work_dir, "intermediate", "step3_reconciled.json")
    data = {
        "status": ctx.status,
        "total_blocks": len(ctx.text_blocks),
        "blocks": [tb.to_dict() for tb in ctx.text_blocks],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

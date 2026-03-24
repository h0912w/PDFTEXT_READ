"""
Step 1: Digital text layer extraction using pdfplumber.

Extracts word-level text with bounding boxes from DIGITAL and HYBRID pages.
SCANNED pages receive an empty text-layer result.

State transition: CLASSIFIED → TEXT_LAYER_EXTRACTED
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pdfplumber

from src.models.state import (
    DocumentType,
    PipelineContext,
    ProcessingStatus,
    ReadingDirection,
    TextBlock,
    TextStatus,
)
from src.utils.logger import get_logger


def run(ctx: PipelineContext) -> PipelineContext:
    """Extract text layer for all target pages."""
    logger = get_logger()
    logger.info("Step 1: Extracting text layer…")

    raw_results: Dict[int, List[Dict[str, Any]]] = {}  # page_num → raw word list

    try:
        with pdfplumber.open(ctx.pdf_path) as pdf:
            for pi in ctx.page_infos:
                if pi.doc_type == DocumentType.SCANNED.value and not ctx.options.get("ocr_priority"):
                    logger.info(f"  Page {pi.page_num}: SCANNED – skipping text layer.")
                    raw_results[pi.page_num] = []
                    continue

                page = pdf.pages[pi.page_num - 1]
                words = page.extract_words() or []
                raw = _words_to_raw(words, float(page.width), float(page.height), pi.page_num)
                raw_results[pi.page_num] = raw
                logger.info(f"  Page {pi.page_num}: {len(raw)} word(s) extracted from text layer.")

        # Build initial TextBlocks (will be reordered in Step 3)
        blocks: List[TextBlock] = []
        order_idx = 0
        for pi in ctx.page_infos:
            page_words = raw_results.get(pi.page_num, [])
            ordered = _sort_words(page_words, pi.direction)
            for w in ordered:
                blocks.append(TextBlock(
                    order_index=order_idx,
                    page_num=pi.page_num,
                    text=w["text"],
                    bbox=w["bbox"],
                    confidence=w["confidence"],
                    reading_direction=pi.direction,
                    status=TextStatus.OK.value,
                    source="text_layer",
                    review_required=False,
                    rotated=w.get("rotated", False),
                ))
                order_idx += 1

        ctx.text_blocks = blocks
        ctx.status = ProcessingStatus.TEXT_LAYER_EXTRACTED
        _save(ctx, raw_results)
        logger.info(f"Step 1 complete. Total blocks: {len(blocks)}, Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 1 failed: {exc}")
        logger.error(f"Step 1 failed: {exc}", exc_info=True)

    return ctx


# ── Helpers ───────────────────────────────────────────────────────────────

def _words_to_raw(
    words: List[Dict], page_width: float, page_height: float, page_num: int
) -> List[Dict[str, Any]]:
    """Normalize pdfplumber word dicts to our internal format."""
    result = []
    for w in words:
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        x0 = float(w.get("x0", 0))
        top = float(w.get("top", 0))
        x1 = float(w.get("x1", x0))
        bottom = float(w.get("bottom", top))
        upright = w.get("upright", True)

        # Normalize to [0, 1]
        nx0 = x0 / max(1.0, page_width)
        ny0 = top / max(1.0, page_height)
        nx1 = x1 / max(1.0, page_width)
        ny1 = bottom / max(1.0, page_height)

        result.append({
            "text": text,
            "bbox": [nx0, ny0, nx1, ny1],
            "confidence": 1.0,  # Text layer is considered fully reliable
            "rotated": not bool(upright),
            "page_num": page_num,
        })
    return result


def _sort_words(words: List[Dict], direction: str) -> List[Dict]:
    """Sort words into reading order."""
    if not words:
        return words

    if direction == ReadingDirection.LEFT_TO_RIGHT.value:
        # Group into rows by y-center proximity, then sort each row by x0
        return _sort_ltr(words)
    else:
        # Group into columns by x-center proximity, then sort each column by y0
        return _sort_ttb(words)


def _sort_ltr(words: List[Dict]) -> List[Dict]:
    """Left-to-right reading order: row-major (y first, then x)."""
    if not words:
        return words

    heights = [w["bbox"][3] - w["bbox"][1] for w in words]
    median_h = sorted(heights)[len(heights) // 2]
    tolerance = max(median_h * 0.5, 0.005)

    # Sort by y0 first for band grouping
    sorted_by_y = sorted(words, key=lambda w: w["bbox"][1])
    rows = _group_by_band(sorted_by_y, axis=1, tolerance=tolerance)

    result = []
    for row in rows:
        result.extend(sorted(row, key=lambda w: w["bbox"][0]))  # sort by x0
    return result


def _sort_ttb(words: List[Dict]) -> List[Dict]:
    """Top-to-bottom reading order: column-major (x first, then y)."""
    if not words:
        return words

    widths = [w["bbox"][2] - w["bbox"][0] for w in words]
    median_w = sorted(widths)[len(widths) // 2]
    tolerance = max(median_w * 0.5, 0.005)

    sorted_by_x = sorted(words, key=lambda w: w["bbox"][0])
    columns = _group_by_band(sorted_by_x, axis=0, tolerance=tolerance)

    result = []
    for col in columns:
        result.extend(sorted(col, key=lambda w: w["bbox"][1]))  # sort by y0
    return result


def _group_by_band(words: List[Dict], axis: int, tolerance: float) -> List[List[Dict]]:
    """
    Group words into bands along a given axis (0=x-center, 1=y-center).
    Words whose center coordinates are within `tolerance` of the current band
    center are grouped together.
    """
    if not words:
        return []

    bands: List[List[Dict]] = []
    current_band: List[Dict] = [words[0]]
    # Use center of bbox along axis
    center_idx = axis  # 0→x0, 1→y0 (start of bbox component)

    def _center(w: Dict) -> float:
        return (w["bbox"][center_idx] + w["bbox"][center_idx + 2]) / 2

    band_center = _center(words[0])

    for w in words[1:]:
        c = _center(w)
        if abs(c - band_center) <= tolerance:
            current_band.append(w)
        else:
            bands.append(current_band)
            current_band = [w]
            band_center = c

    bands.append(current_band)
    return bands


def _save(ctx: PipelineContext, raw: Dict) -> None:
    path = os.path.join(ctx.work_dir, "intermediate", "step1_text_layer.json")
    data = {
        "status": ctx.status,
        "total_blocks": len(ctx.text_blocks),
        "pages": {str(k): v for k, v in raw.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

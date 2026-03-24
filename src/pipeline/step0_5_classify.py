"""
Step 0.5: Document type classification and per-page strategy decision.

Classifies each page as DIGITAL / SCANNED / HYBRID.
Determines overall document type and reading direction per page.

State transition: RECEIVED → CLASSIFIED
"""
from __future__ import annotations

import json
import os
from typing import List

import pdfplumber

from src.models.state import (
    DocumentType,
    PageInfo,
    PipelineContext,
    ProcessingStatus,
    ReadingDirection,
)
from src.pipeline.step0_init import parse_page_range
from src.utils.logger import get_logger

# Thresholds
_DIGITAL_CHAR_MIN = 80     # min chars per page to be considered DIGITAL
_SCANNED_CHAR_MAX = 10     # max chars per page before considering it SCANNED


def run(ctx: PipelineContext) -> PipelineContext:
    """
    Classify each target page and assign reading direction.

    Populates ctx.page_infos and ctx.doc_type.
    """
    logger = get_logger()
    logger.info("Step 0.5: Classifying document…")

    try:
        with pdfplumber.open(ctx.pdf_path) as pdf:
            total = len(pdf.pages)
            target_pages = parse_page_range(ctx.options.get("page_range"), total)
            logger.info(f"Total pages: {total}, target: {target_pages}")

            page_infos: List[PageInfo] = []
            for page_num in target_pages:
                page = pdf.pages[page_num - 1]  # pdfplumber is 0-indexed
                info = _classify_page(page, page_num, ctx)
                page_infos.append(info)
                logger.info(
                    f"  Page {page_num}: type={info.doc_type}, "
                    f"dir={info.direction}, coverage={info.text_coverage:.2f}"
                )

        ctx.page_infos = page_infos
        ctx.doc_type = _infer_overall_type(page_infos)
        ctx.status = ProcessingStatus.CLASSIFIED

        _save(ctx)
        logger.info(f"Step 0.5 complete. Overall type: {ctx.doc_type}, Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 0.5 failed: {exc}")
        logger.error(f"Step 0.5 failed: {exc}", exc_info=True)

    return ctx


# ── Helpers ───────────────────────────────────────────────────────────────

def _classify_page(page, page_num: int, ctx: PipelineContext) -> PageInfo:
    """Classify a single pdfplumber page."""
    width = float(page.width)
    height = float(page.height)

    # Extract words from text layer
    words = page.extract_words() or []
    char_count = sum(len(w["text"]) for w in words)

    # Compute text coverage ratio
    coverage = _compute_coverage(char_count, width, height)

    # Classify
    forced = ctx.options.get("force_direction")
    if ctx.options.get("ocr_priority"):
        doc_type = DocumentType.SCANNED
    elif char_count >= _DIGITAL_CHAR_MIN:
        doc_type = DocumentType.DIGITAL
    elif char_count <= _SCANNED_CHAR_MAX:
        doc_type = DocumentType.SCANNED
    else:
        doc_type = DocumentType.HYBRID

    # Direction detection
    if forced:
        direction = forced
    else:
        direction = _detect_direction(words, width, height)

    return PageInfo(
        page_num=page_num,
        doc_type=doc_type.value,
        direction=direction,
        width=width,
        height=height,
        text_coverage=coverage,
    )


def _compute_coverage(char_count: int, width: float, height: float) -> float:
    """
    Estimate text coverage as a fraction of how much text we expect
    on a page of this size (rough heuristic).
    """
    area = width * height
    if area <= 0:
        return 0.0
    # Rough: ~0.5 char per 10 pt² for a dense page
    expected_max_chars = area / 200.0
    return min(1.0, char_count / max(1.0, expected_max_chars))


def _detect_direction(words: list, width: float, height: float) -> str:
    """
    Determine reading direction from text block positions.

    Heuristic:
    - Compute the span of text in X vs Y.
    - If text is spread much more vertically than horizontally AND is
      arranged in vertical strips → TOP_TO_BOTTOM.
    - Otherwise → LEFT_TO_RIGHT (default for most documents).
    """
    if not words:
        return ReadingDirection.LEFT_TO_RIGHT.value

    xs = [(w["x0"] + w["x1"]) / 2 for w in words]
    ys = [(w["top"] + w["bottom"]) / 2 for w in words]

    if len(xs) < 2:
        return ReadingDirection.LEFT_TO_RIGHT.value

    x_range = max(xs) - min(xs)
    y_range = max(ys) - min(ys)

    # Aspect ratio of the page
    page_is_tall = height > width

    # TOP_TO_BOTTOM heuristic: high x-variance and moderate y-variance
    # relative to page dimensions, AND page is taller than wide.
    # This targets vertical-column layouts (e.g., traditional CJK documents).
    x_span_ratio = x_range / max(1.0, width)
    y_span_ratio = y_range / max(1.0, height)

    if page_is_tall and x_span_ratio > 0.5 and y_span_ratio < 0.3:
        # Wide horizontal spread, shallow vertical spread → column strips
        return ReadingDirection.TOP_TO_BOTTOM.value

    return ReadingDirection.LEFT_TO_RIGHT.value


def _infer_overall_type(page_infos: List[PageInfo]) -> str:
    """Choose the dominant document type across all classified pages."""
    counts = {t.value: 0 for t in DocumentType}
    for pi in page_infos:
        counts[pi.doc_type] = counts.get(pi.doc_type, 0) + 1

    if counts[DocumentType.SCANNED.value] == len(page_infos):
        return DocumentType.SCANNED.value
    if counts[DocumentType.DIGITAL.value] == len(page_infos):
        return DocumentType.DIGITAL.value
    return DocumentType.HYBRID.value


def _save(ctx: PipelineContext) -> None:
    path = os.path.join(ctx.work_dir, "intermediate", "document_classification.json")
    data = {
        "overall_type": ctx.doc_type,
        "status": ctx.status,
        "pages": [pi.to_dict() for pi in ctx.page_infos],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

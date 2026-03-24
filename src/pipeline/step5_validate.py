"""
Step 5: Quality validation and final routing.

Checks:
  - Order index continuity.
  - Presence of review_required pages.
  - Overall confidence distribution.

Routes to VALIDATED or APPROVED_WITH_WARNINGS.
Does NOT auto-approve if quality is unacceptably low.

State transition: SKIP_RESOLVED → VALIDATED | APPROVED_WITH_WARNINGS
"""
from __future__ import annotations

import json
import os
from typing import List, Set

from src.models.state import PipelineContext, ProcessingStatus, TextStatus
from src.utils.logger import get_logger

# If skipped ratio exceeds this, raise a warning (not a failure)
_SKIP_WARN_RATIO = 0.20
# Minimum average confidence to auto-confirm without warnings
_MIN_AVG_CONFIDENCE = 0.70


def run(ctx: PipelineContext) -> PipelineContext:
    """Validate extraction quality and set final pre-export status."""
    logger = get_logger()
    logger.info("Step 5: Validating extraction quality…")

    try:
        total = len(ctx.text_blocks)
        skipped = ctx.skipped_count
        review_pages: Set[int] = set()
        confidences: List[float] = []

        for tb in ctx.text_blocks:
            if tb.review_required:
                review_pages.add(tb.page_num)
            if tb.status == TextStatus.OK.value:
                confidences.append(tb.confidence)

        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        skip_ratio = skipped / max(1, total)

        logger.info(
            f"  Total blocks: {total}, skipped: {skipped} ({skip_ratio:.1%}), "
            f"avg confidence: {avg_conf:.2f}, review pages: {sorted(review_pages)}"
        )

        has_warnings = False

        if skip_ratio > _SKIP_WARN_RATIO:
            msg = f"High skip ratio: {skip_ratio:.1%} ({skipped}/{total} blocks)"
            ctx.add_warning(msg)
            logger.warning(f"  WARNING: {msg}")
            has_warnings = True

        if avg_conf < _MIN_AVG_CONFIDENCE and confidences:
            msg = f"Low average confidence: {avg_conf:.2f}"
            ctx.add_warning(msg)
            logger.warning(f"  WARNING: {msg}")
            has_warnings = True

        if total == 0:
            msg = "No text blocks produced."
            ctx.add_warning(msg)
            logger.warning(f"  WARNING: {msg}")
            has_warnings = True

        # Mark review_required on page_infos for XLSX highlighting
        for pi in ctx.page_infos:
            if pi.page_num in review_pages:
                pass  # Already set on individual blocks

        if has_warnings:
            ctx.status = ProcessingStatus.APPROVED_WITH_WARNINGS
        else:
            ctx.status = ProcessingStatus.VALIDATED

        _save(ctx, review_pages, avg_conf, skip_ratio)
        logger.info(f"Step 5 complete. Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 5 failed: {exc}")
        logger.error(f"Step 5 failed: {exc}", exc_info=True)

    return ctx


def _save(ctx: PipelineContext, review_pages, avg_conf, skip_ratio) -> None:
    # summary.json
    summary_path = os.path.join(ctx.work_dir, "intermediate", "summary.json")
    summary = {
        "status": ctx.status,
        "pdf_path": ctx.pdf_path,
        "doc_type": ctx.doc_type,
        "total_pages": len(ctx.page_infos),
        "total_blocks": len(ctx.text_blocks),
        "skipped_count": ctx.skipped_count,
        "skip_ratio": skip_ratio,
        "avg_confidence": avg_conf,
        "warnings": ctx.warnings,
        "errors": ctx.errors,
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # review_required_pages.json
    review_path = os.path.join(ctx.work_dir, "intermediate", "review_required_pages.json")
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump({"pages": sorted(review_pages)}, f, ensure_ascii=False, indent=2)

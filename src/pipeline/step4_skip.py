"""
Step 4: Skip resolution for low-confidence text blocks.

Marks text blocks below the confidence threshold as SKIPPED or UNKNOWN.
The pipeline continues; skipped items are counted and logged explicitly.
Full execution never stops because of individual low-confidence blocks.

State transition: RECONCILED → SKIP_RESOLVED
"""
from __future__ import annotations

import json
import os

from src.models.state import PipelineContext, ProcessingStatus, TextStatus
from src.utils.logger import get_logger

# Below this confidence → SKIPPED (text has content but unreliable)
# Below half threshold → UNKNOWN (text is essentially unreadable)
_UNKNOWN_RATIO = 0.5


def run(ctx: PipelineContext) -> PipelineContext:
    """Apply skip policy to all text blocks."""
    logger = get_logger()
    logger.info("Step 4: Resolving skips and low-confidence blocks…")

    threshold = ctx.options.get("confidence_threshold", 0.5)
    unknown_threshold = threshold * _UNKNOWN_RATIO

    skipped = 0
    unknown = 0

    try:
        for tb in ctx.text_blocks:
            if tb.confidence < unknown_threshold:
                tb.status = TextStatus.UNKNOWN.value
                tb.review_required = True
                unknown += 1
            elif tb.confidence < threshold:
                tb.status = TextStatus.SKIPPED.value
                tb.review_required = True
                skipped += 1
            # else: status remains OK

        ctx.skipped_count = skipped + unknown
        ctx.status = ProcessingStatus.SKIP_RESOLVED

        if ctx.skipped_count > 0:
            ctx.add_warning(
                f"Skip resolution: {skipped} SKIPPED, {unknown} UNKNOWN "
                f"(threshold={threshold:.2f})"
            )
            logger.warning(
                f"  {skipped} SKIPPED, {unknown} UNKNOWN out of {len(ctx.text_blocks)} blocks."
            )
        else:
            logger.info("  No blocks skipped.")

        _save(ctx, skipped, unknown)
        logger.info(f"Step 4 complete. Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 4 failed: {exc}")
        logger.error(f"Step 4 failed: {exc}", exc_info=True)

    return ctx


def _save(ctx: PipelineContext, skipped: int, unknown: int) -> None:
    path = os.path.join(ctx.work_dir, "intermediate", "step4_skip_resolution.json")
    data = {
        "status": ctx.status,
        "confidence_threshold": ctx.options.get("confidence_threshold", 0.5),
        "skipped_count": skipped,
        "unknown_count": unknown,
        "total_skipped": ctx.skipped_count,
        "blocks": [
            {"order_index": tb.order_index, "page_num": tb.page_num,
             "text": tb.text, "status": tb.status, "confidence": tb.confidence}
            for tb in ctx.text_blocks
            if tb.status != TextStatus.OK.value
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

"""
Step 6: Export final_output.csv.

CSV columns:
  order_index | page_num | reading_direction | text | confidence | status |
  source | review_required | rotated

For LEFT_TO_RIGHT pages: one row per text block (row-major reading order).
For TOP_TO_BOTTOM pages: one row per text block (column-major reading order).
The ordering is determined by order_index assigned in Step 3.

Skipped items appear with their SKIPPED/UNKNOWN status – they are NEVER silently dropped.

State transition: VALIDATED|APPROVED_WITH_WARNINGS → (partial) EXPORT_COMPLETED
"""
from __future__ import annotations

import csv
import os

from src.models.state import PipelineContext, ProcessingStatus, TextStatus
from src.utils.logger import get_logger

_CSV_FILENAME = "final_output.csv"
_FIELDNAMES = [
    "order_index",
    "page_num",
    "reading_direction",
    "text",
    "confidence",
    "status",
    "source",
    "review_required",
    "rotated",
]


def run(ctx: PipelineContext) -> PipelineContext:
    """Write final_output.csv to the work directory."""
    logger = get_logger()
    logger.info("Step 6: Generating final_output.csv…")

    csv_path = os.path.join(ctx.work_dir, _CSV_FILENAME)

    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
            writer.writeheader()

            for tb in sorted(ctx.text_blocks, key=lambda b: b.order_index):
                writer.writerow({
                    "order_index": tb.order_index,
                    "page_num": tb.page_num,
                    "reading_direction": tb.reading_direction,
                    "text": tb.text,
                    "confidence": f"{tb.confidence:.4f}",
                    "status": tb.status,
                    "source": tb.source,
                    "review_required": tb.review_required,
                    "rotated": tb.rotated,
                })

        logger.info(f"  CSV written: {csv_path} ({len(ctx.text_blocks)} rows)")
        ctx.options["_csv_path"] = csv_path

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 6 failed: {exc}")
        logger.error(f"Step 6 failed: {exc}", exc_info=True)

    return ctx

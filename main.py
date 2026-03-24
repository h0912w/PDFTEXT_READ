"""
main.py – CLI entry point for the PDF text extraction pipeline.

Usage:
    python main.py <pdf_path> [options]

Examples:
    python main.py document.pdf
    python main.py document.pdf --pages 1-5 --preprocess --debug
    python main.py document.pdf --ocr-engine tesseract --confidence-threshold 0.6
    python main.py document.pdf --force-direction TOP_TO_BOTTOM
"""
from __future__ import annotations

import argparse
import os
import sys

from src.models.state import ProcessingStatus
from src.pipeline import (
    step0_init,
    step0_5_classify,
    step1_text_layer,
    step1_5_preprocess,
    step2_vision,
    step3_reconcile,
    step4_skip,
    step5_validate,
    step6_csv,
    step7_xlsx,
)
from src.utils.logger import get_logger


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdftext",
        description="Extract and review text from PDF files (digital, scanned, hybrid).",
    )
    p.add_argument("pdf_path", help="Path to the input PDF file.")
    p.add_argument(
        "--output-dir", default="output",
        help="Root output directory (default: ./output).",
    )
    p.add_argument(
        "--pages",
        help='Page range to process. Examples: "1-5", "1,3,5", "2-4,7". Default: all pages.',
    )
    p.add_argument(
        "--ocr-engine", choices=["tesseract", "easyocr"], default="tesseract",
        help="OCR engine to use (default: tesseract).",
    )
    p.add_argument(
        "--force-direction", choices=["LEFT_TO_RIGHT", "TOP_TO_BOTTOM"],
        help="Force reading direction for all pages.",
    )
    p.add_argument(
        "--confidence-threshold", type=float, default=0.5, metavar="0.0-1.0",
        help="Confidence threshold below which text is marked SKIPPED (default: 0.5).",
    )
    p.add_argument(
        "--render-dpi", type=int, default=150,
        help="DPI for rendering PDF pages to images (default: 150).",
    )
    p.add_argument(
        "--preprocess", action="store_true", default=True,
        help="Apply scan preprocessing (grayscale, denoise, threshold, deskew). Default: on.",
    )
    p.add_argument(
        "--no-preprocess", dest="preprocess", action="store_false",
        help="Disable scan preprocessing.",
    )
    p.add_argument(
        "--ocr-priority", action="store_true", default=False,
        help="Force OCR on all pages (treat everything as SCANNED).",
    )
    p.add_argument(
        "--debug", action="store_true", default=False,
        help="Save extra debug information to the work directory.",
    )
    return p


def run_pipeline(pdf_path: str, output_dir: str, options: dict) -> int:
    """
    Execute the full extraction pipeline.

    Returns:
        0 on success (EXPORT_COMPLETED), 1 on failure.
    """
    # Step 0: Init
    try:
        ctx = step0_init.run(pdf_path, options=options, output_base_dir=output_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    logger = get_logger()

    def _check(label: str) -> bool:
        if ctx.status == ProcessingStatus.FAILED:
            logger.error(f"Pipeline failed at {label}. Errors: {ctx.errors}")
            return False
        return True

    # Step 0.5: Classify
    step0_5_classify.run(ctx)
    if not _check("Step 0.5"):
        return 1

    # Step 1: Text layer
    step1_text_layer.run(ctx)
    if not _check("Step 1"):
        return 1

    # Step 1.5: Preprocess
    step1_5_preprocess.run(ctx)
    if not _check("Step 1.5"):
        return 1

    # Step 2: OCR
    step2_vision.run(ctx)
    if not _check("Step 2"):
        return 1

    # Step 3: Reconcile
    step3_reconcile.run(ctx)
    if not _check("Step 3"):
        return 1

    # Step 4: Skip resolution
    step4_skip.run(ctx)
    if not _check("Step 4"):
        return 1

    # Step 5: Validate
    step5_validate.run(ctx)
    if not _check("Step 5"):
        return 1

    # Step 6: CSV
    step6_csv.run(ctx)
    if not _check("Step 6"):
        return 1

    # Step 7: XLSX
    step7_xlsx.run(ctx)
    if not _check("Step 7"):
        return 1

    # Final status report
    logger.info("=" * 60)
    logger.info(f"Pipeline complete. Status: {ctx.status}")
    logger.info(f"Work directory: {ctx.work_dir}")
    logger.info(f"CSV:  {ctx.options.get('_csv_path')}")
    logger.info(f"XLSX: {ctx.options.get('_xlsx_path')}")
    if ctx.warnings:
        logger.warning("Warnings:")
        for w in ctx.warnings:
            logger.warning(f"  • {w}")
    if ctx.skipped_count:
        logger.warning(f"Skipped blocks: {ctx.skipped_count}")
    logger.info("=" * 60)

    print(f"\nDone. Outputs saved to: {ctx.work_dir}")
    print(f"  CSV : {ctx.options.get('_csv_path')}")
    print(f"  XLSX: {ctx.options.get('_xlsx_path')}")
    if ctx.warnings:
        print(f"  Warnings ({len(ctx.warnings)}):")
        for w in ctx.warnings:
            print(f"    • {w}")

    return 0 if ctx.status == ProcessingStatus.EXPORT_COMPLETED else 1


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    options = {
        "page_range": args.pages,
        "ocr_engine": args.ocr_engine,
        "force_direction": args.force_direction,
        "confidence_threshold": args.confidence_threshold,
        "render_dpi": args.render_dpi,
        "preprocess": args.preprocess,
        "ocr_priority": args.ocr_priority,
        "debug": args.debug,
    }

    sys.exit(run_pipeline(args.pdf_path, args.output_dir, options))


if __name__ == "__main__":
    main()

"""
Step 0: Input validation and work directory initialization.

State transition: RECEIVED → (stays RECEIVED on error → FAILED)
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.models.state import PipelineContext, ProcessingStatus
from src.utils.logger import setup_logger


def run(
    pdf_path: str,
    options: Optional[Dict[str, Any]] = None,
    output_base_dir: str = "output",
) -> PipelineContext:
    """
    Validate input and initialize the pipeline context.

    Args:
        pdf_path:        Absolute or relative path to the PDF.
        options:         Optional processing options dict.
        output_base_dir: Root directory for all outputs.

    Returns:
        Initialized PipelineContext (status=RECEIVED).

    Raises:
        ValueError: If the input file is not a valid PDF.
        FileNotFoundError: If pdf_path does not exist.
    """
    if options is None:
        options = {}

    # ── Resolve absolute path ──────────────────────────────────────────────
    pdf_path = os.path.abspath(pdf_path)

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if not pdf_path.lower().endswith(".pdf"):
        raise ValueError(f"Input must be a PDF file, got: {pdf_path}")

    # Quick magic-byte check
    with open(pdf_path, "rb") as f:
        magic = f.read(5)
    if magic != b"%PDF-":
        raise ValueError(f"File does not appear to be a valid PDF: {pdf_path}")

    # ── Build work directory ───────────────────────────────────────────────
    pdf_stem = _safe_stem(pdf_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = os.path.abspath(os.path.join(output_base_dir, f"{pdf_stem}_{timestamp}"))

    _make_dirs(work_dir)

    # ── Set up logger ──────────────────────────────────────────────────────
    log_file = os.path.join(work_dir, "pipeline.log")
    logger = setup_logger("pdftext", log_file=log_file)
    logger.info("=== Pipeline Start ===")
    logger.info(f"PDF: {pdf_path}")
    logger.info(f"Work dir: {work_dir}")
    logger.info(f"Options: {options}")

    # ── Normalize options ──────────────────────────────────────────────────
    normalized = _normalize_options(options, logger)

    # ── Build context ──────────────────────────────────────────────────────
    ctx = PipelineContext(
        pdf_path=pdf_path,
        work_dir=work_dir,
        status=ProcessingStatus.RECEIVED,
        options=normalized,
    )

    # Save init manifest
    _save_manifest(ctx)
    logger.info(f"Step 0 complete. Status: {ctx.status}")
    return ctx


# ── Helpers ────────────────────────────────────────────────────────────────

def _safe_stem(pdf_path: str) -> str:
    """Return a filesystem-safe version of the PDF filename (without extension)."""
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    stem = re.sub(r"[^\w\-]", "_", stem)
    return stem[:64]  # Limit length


def _make_dirs(work_dir: str) -> None:
    """Create work directory sub-structure."""
    for subdir in ("intermediate", "images", "preprocessed"):
        os.makedirs(os.path.join(work_dir, subdir), exist_ok=True)


def _normalize_options(raw: Dict[str, Any], logger) -> Dict[str, Any]:
    """Validate and apply defaults for all pipeline options."""
    opts: Dict[str, Any] = {}

    # Page range: "1-5" | "1,3,5" | None (all pages)
    opts["page_range"] = raw.get("page_range", None)

    # OCR engine
    engine = raw.get("ocr_engine", "tesseract").lower()
    if engine not in ("tesseract", "easyocr"):
        logger.warning(f"Unknown OCR engine '{engine}', defaulting to tesseract.")
        engine = "tesseract"
    opts["ocr_engine"] = engine

    # Force reading direction
    forced_dir = raw.get("force_direction", None)
    if forced_dir is not None and forced_dir not in ("LEFT_TO_RIGHT", "TOP_TO_BOTTOM"):
        logger.warning(f"Invalid force_direction '{forced_dir}', ignoring.")
        forced_dir = None
    opts["force_direction"] = forced_dir

    # Confidence threshold (0.0–1.0)
    threshold = float(raw.get("confidence_threshold", 0.5))
    opts["confidence_threshold"] = max(0.0, min(1.0, threshold))

    # Boolean flags
    opts["debug"] = bool(raw.get("debug", False))
    opts["preprocess"] = bool(raw.get("preprocess", True))
    opts["ocr_priority"] = bool(raw.get("ocr_priority", False))

    # Render DPI for page images
    opts["render_dpi"] = int(raw.get("render_dpi", 150))

    return opts


def _save_manifest(ctx: PipelineContext) -> None:
    """Save step 0 manifest to intermediate directory."""
    manifest = {
        "pdf_path": ctx.pdf_path,
        "work_dir": ctx.work_dir,
        "status": ctx.status,
        "options": ctx.options,
    }
    path = os.path.join(ctx.work_dir, "intermediate", "step0_manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def parse_page_range(page_range_str: Optional[str], total_pages: int) -> List[int]:
    """
    Convert a page range string to a sorted list of 1-based page numbers.

    Accepts:
        None        → all pages
        "1-5"       → [1, 2, 3, 4, 5]
        "1,3,5"     → [1, 3, 5]
        "2-4,7"     → [2, 3, 4, 7]
    """
    if not page_range_str:
        return list(range(1, total_pages + 1))

    pages = set()
    for part in page_range_str.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))

    valid = sorted(p for p in pages if 1 <= p <= total_pages)
    return valid

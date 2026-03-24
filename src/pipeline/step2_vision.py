"""
Step 2: OCR / vision-based text and layout analysis.

Runs OCR on each page image (SCANNED / HYBRID / ocr_priority).
Produces word-level TextBlocks with confidence scores and bounding boxes.

Supported engines: tesseract (default), easyocr (optional).

State transition: PREPROCESSED_FOR_OCR → VISION_ANALYZED
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

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

# Maximum retries per page for OCR errors
_MAX_OCR_RETRIES = 2


def run(ctx: PipelineContext) -> PipelineContext:
    """Run OCR on all pages that require it."""
    logger = get_logger()
    logger.info("Step 2: Running OCR / vision analysis…")

    engine = ctx.options.get("ocr_engine", "tesseract")
    ocr_priority = ctx.options.get("ocr_priority", False)

    # Collect OCR results per page: {page_num: [raw_word_dict]}
    ocr_results: Dict[int, List[Dict[str, Any]]] = {}

    try:
        for pi in ctx.page_infos:
            needs_ocr = (
                pi.doc_type in (DocumentType.SCANNED.value, DocumentType.HYBRID.value)
                or ocr_priority
            )
            if not needs_ocr:
                ocr_results[pi.page_num] = []
                continue

            img_path = pi.preprocessed_image_path or pi.image_path
            if not img_path or not os.path.exists(img_path):
                logger.warning(f"  Page {pi.page_num}: no image found, skipping OCR.")
                ocr_results[pi.page_num] = []
                ctx.add_warning(f"Page {pi.page_num}: no image for OCR.")
                continue

            words = _run_ocr_with_retry(img_path, engine, pi.page_num, logger)
            ocr_results[pi.page_num] = words
            logger.info(f"  Page {pi.page_num}: {len(words)} word(s) from OCR.")

        # Build OCR TextBlocks (stored separately; Step 3 merges them)
        ctx.options["_ocr_raw"] = {str(k): v for k, v in ocr_results.items()}
        ctx.status = ProcessingStatus.VISION_ANALYZED
        _save(ctx, ocr_results)
        logger.info(f"Step 2 complete. Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 2 failed: {exc}")
        logger.error(f"Step 2 failed: {exc}", exc_info=True)

    return ctx


# ── OCR engines ───────────────────────────────────────────────────────────

def _run_ocr_with_retry(
    img_path: str, engine: str, page_num: int, logger
) -> List[Dict[str, Any]]:
    """Run OCR with automatic retry on failure."""
    for attempt in range(1, _MAX_OCR_RETRIES + 2):
        try:
            if engine == "easyocr":
                return _ocr_easyocr(img_path, page_num)
            else:
                return _ocr_tesseract(img_path, page_num)
        except Exception as exc:
            if attempt <= _MAX_OCR_RETRIES:
                logger.warning(f"  Page {page_num}: OCR attempt {attempt} failed ({exc}), retrying…")
            else:
                logger.error(f"  Page {page_num}: OCR failed after {_MAX_OCR_RETRIES + 1} attempts: {exc}")
                return []
    return []


def _ocr_tesseract(img_path: str, page_num: int) -> List[Dict[str, Any]]:
    """
    Run pytesseract and return normalized word dicts.
    Requires Tesseract to be installed on the system.
    """
    import pytesseract
    from PIL import Image

    img = Image.open(img_path)
    img_w, img_h = img.size

    # Use HOCR-style data output for bbox + confidence
    data = pytesseract.image_to_data(
        img,
        lang="kor+eng",
        config="--psm 3",
        output_type=pytesseract.Output.DICT,
    )

    words = []
    n = len(data["text"])
    for i in range(n):
        text = str(data["text"][i]).strip()
        conf = int(data["conf"][i])
        if not text or conf < 0:
            continue

        left = int(data["left"][i])
        top = int(data["top"][i])
        width = int(data["width"][i])
        height = int(data["height"][i])

        if width <= 0 or height <= 0:
            continue

        # Normalize bbox
        x0 = left / max(1, img_w)
        y0 = top / max(1, img_h)
        x1 = (left + width) / max(1, img_w)
        y1 = (top + height) / max(1, img_h)

        words.append({
            "text": text,
            "bbox": [x0, y0, x1, y1],
            "confidence": conf / 100.0,
            "rotated": False,
            "page_num": page_num,
        })

    return words


def _ocr_easyocr(img_path: str, page_num: int) -> List[Dict[str, Any]]:
    """
    Run EasyOCR and return normalized word dicts.
    Requires: pip install easyocr
    """
    import easyocr
    from PIL import Image

    img = Image.open(img_path)
    img_w, img_h = img.size

    reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
    results = reader.readtext(img_path, detail=1)

    words = []
    for (bbox_pts, text, prob) in results:
        text = str(text).strip()
        if not text:
            continue
        # bbox_pts: [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]
        xs = [p[0] for p in bbox_pts]
        ys = [p[1] for p in bbox_pts]
        x0 = min(xs) / max(1, img_w)
        y0 = min(ys) / max(1, img_h)
        x1 = max(xs) / max(1, img_w)
        y1 = max(ys) / max(1, img_h)

        words.append({
            "text": text,
            "bbox": [x0, y0, x1, y1],
            "confidence": float(prob),
            "rotated": False,
            "page_num": page_num,
        })

    return words


def _save(ctx: PipelineContext, ocr_results: Dict) -> None:
    path = os.path.join(ctx.work_dir, "intermediate", "step2_vision_layout.json")
    data = {
        "status": ctx.status,
        "ocr_engine": ctx.options.get("ocr_engine", "tesseract"),
        "pages": {str(k): v for k, v in ocr_results.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

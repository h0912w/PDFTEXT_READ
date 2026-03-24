"""
Step 1.5: Render PDF pages to images and apply scan preprocessing.

For SCANNED and HYBRID pages:
  - Render page to PNG via PyMuPDF.
  - Apply grayscale → denoise → threshold → deskew (if preprocess=True).
  - Falls back to plain rendered image on preprocessing failure.

State transition: TEXT_LAYER_EXTRACTED → PREPROCESSED_FOR_OCR
"""
from __future__ import annotations

import json
import os
from typing import List

from src.models.state import DocumentType, PipelineContext, ProcessingStatus
from src.utils.image_utils import preprocess_image, render_pdf_page
from src.utils.logger import get_logger


def run(ctx: PipelineContext) -> PipelineContext:
    """Render all target pages and preprocess where needed."""
    logger = get_logger()
    logger.info("Step 1.5: Rendering pages and preprocessing…")

    images_dir = os.path.join(ctx.work_dir, "images")
    pre_dir = os.path.join(ctx.work_dir, "preprocessed")
    dpi = ctx.options.get("render_dpi", 150)
    do_preprocess = ctx.options.get("preprocess", True)

    try:
        for pi in ctx.page_infos:
            # Always render the page (needed for XLSX review layout)
            img_path = os.path.join(images_dir, f"page_{pi.page_num:03d}.png")
            render_pdf_page(ctx.pdf_path, pi.page_num - 1, img_path, dpi=dpi)
            pi.image_path = img_path
            logger.info(f"  Page {pi.page_num}: rendered → {img_path}")

            # Preprocess only if page needs OCR
            needs_ocr = (
                pi.doc_type in (DocumentType.SCANNED.value, DocumentType.HYBRID.value)
                or ctx.options.get("ocr_priority", False)
            )
            if needs_ocr and do_preprocess:
                pre_path = os.path.join(pre_dir, f"page_{pi.page_num:03d}_pre.png")
                result = preprocess_image(img_path, pre_path)
                pi.preprocessed_image_path = result
                if result == img_path:
                    logger.warning(f"  Page {pi.page_num}: preprocessing fallback to original.")
                else:
                    logger.info(f"  Page {pi.page_num}: preprocessed → {pre_path}")
            else:
                pi.preprocessed_image_path = img_path  # Use original for OCR

        ctx.status = ProcessingStatus.PREPROCESSED_FOR_OCR
        _save(ctx)
        logger.info(f"Step 1.5 complete. Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 1.5 failed: {exc}")
        logger.error(f"Step 1.5 failed: {exc}", exc_info=True)

    return ctx


def _save(ctx: PipelineContext) -> None:
    path = os.path.join(ctx.work_dir, "intermediate", "step1_5_preprocessed_images.json")
    data = {
        "status": ctx.status,
        "pages": [pi.to_dict() for pi in ctx.page_infos],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

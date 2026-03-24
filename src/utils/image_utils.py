"""
Image helper utilities for PDF rendering and preprocessing.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

import numpy as np

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


def render_pdf_page(pdf_path: str, page_index: int, out_path: str, dpi: int = 150) -> str:
    """
    Render a PDF page to a PNG image using PyMuPDF.

    Args:
        pdf_path:   Path to the PDF file.
        page_index: 0-based page index.
        out_path:   Output PNG path.
        dpi:        Render resolution (default 150).

    Returns:
        out_path on success.
    """
    import fitz  # PyMuPDF

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.save(out_path)
    finally:
        doc.close()
    return out_path


def preprocess_image(src_path: str, dst_path: str) -> str:
    """
    Apply scan preprocessing: grayscale → denoise → threshold → deskew.
    Falls back to the original image if any step fails.

    Args:
        src_path: Source image path.
        dst_path: Destination image path.

    Returns:
        dst_path on success, src_path on fallback.
    """
    if not CV2_AVAILABLE:
        return src_path  # Fallback: no preprocessing

    try:
        img = cv2.imread(src_path)
        if img is None:
            return src_path

        # 1. Grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 2. Denoise
        denoised = cv2.fastNlMeansDenoising(gray, h=10)

        # 3. Contrast enhancement (CLAHE)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(denoised)

        # 4. Adaptive threshold (binarize)
        binary = cv2.adaptiveThreshold(
            enhanced, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, 10
        )

        # 5. Deskew
        deskewed = _deskew(binary)

        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        cv2.imwrite(dst_path, deskewed)
        return dst_path

    except Exception:
        return src_path  # Fallback to original


def _deskew(binary_img: "np.ndarray") -> "np.ndarray":
    """Rotate image to correct skew using Hough line analysis."""
    try:
        coords = np.column_stack(np.where(binary_img < 128))
        if len(coords) < 100:
            return binary_img
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = 90 + angle
        if abs(angle) < 0.5:
            return binary_img  # Negligible skew
        (h, w) = binary_img.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(binary_img, M, (w, h), flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_REPLICATE)
        return rotated
    except Exception:
        return binary_img


def resize_image_for_xlsx(src_path: str, max_width: int = 600, max_height: int = 800) -> Tuple[str, int, int]:
    """
    Resize an image to fit within (max_width, max_height) while keeping aspect ratio.
    Saves a resized copy next to the original with suffix '_thumb'.

    Returns:
        (thumb_path, actual_width, actual_height)
    """
    if not PIL_AVAILABLE:
        return src_path, max_width, max_height

    img = Image.open(src_path)
    img.thumbnail((max_width, max_height), Image.LANCZOS)
    w, h = img.size

    base, ext = os.path.splitext(src_path)
    thumb_path = base + "_thumb" + ext
    img.save(thumb_path)
    return thumb_path, w, h


def get_image_dimensions(image_path: str) -> Optional[Tuple[int, int]]:
    """Return (width, height) of an image, or None if unreadable."""
    if not PIL_AVAILABLE:
        return None
    try:
        with Image.open(image_path) as img:
            return img.size
    except Exception:
        return None

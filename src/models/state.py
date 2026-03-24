"""
Core data models and state definitions for the PDF text extraction pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DocumentType(str, Enum):
    DIGITAL = "DIGITAL"
    SCANNED = "SCANNED"
    HYBRID = "HYBRID"


class ReadingDirection(str, Enum):
    LEFT_TO_RIGHT = "LEFT_TO_RIGHT"
    TOP_TO_BOTTOM = "TOP_TO_BOTTOM"


class ProcessingStatus(str, Enum):
    RECEIVED = "RECEIVED"
    CLASSIFIED = "CLASSIFIED"
    TEXT_LAYER_EXTRACTED = "TEXT_LAYER_EXTRACTED"
    PREPROCESSED_FOR_OCR = "PREPROCESSED_FOR_OCR"
    VISION_ANALYZED = "VISION_ANALYZED"
    RECONCILED = "RECONCILED"
    SKIP_RESOLVED = "SKIP_RESOLVED"
    VALIDATED = "VALIDATED"
    APPROVED_WITH_WARNINGS = "APPROVED_WITH_WARNINGS"
    EXPORT_COMPLETED = "EXPORT_COMPLETED"
    QA_PENDING = "QA_PENDING"
    QA_RUNNING = "QA_RUNNING"
    QA_PASSED = "QA_PASSED"
    QA_FAILED = "QA_FAILED"
    FAILED = "FAILED"


class TextStatus(str, Enum):
    OK = "OK"
    SKIPPED = "SKIPPED"
    UNKNOWN = "UNKNOWN"


@dataclass
class TextBlock:
    """Single unit of extracted text with spatial and metadata information."""
    order_index: int          # Global sequential order across all pages
    page_num: int             # 1-based page number
    text: str
    bbox: List[float]         # [x0, y0, x1, y1] normalized to [0, 1]
    confidence: float         # 0.0 – 1.0
    reading_direction: str    # ReadingDirection value
    status: str               # TextStatus value
    source: str               # "text_layer" | "ocr"
    review_required: bool
    rotated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_index": self.order_index,
            "page_num": self.page_num,
            "text": self.text,
            "bbox": self.bbox,
            "confidence": self.confidence,
            "reading_direction": self.reading_direction,
            "status": self.status,
            "source": self.source,
            "review_required": self.review_required,
            "rotated": self.rotated,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TextBlock":
        return TextBlock(**d)


@dataclass
class PageInfo:
    """Per-page classification and processing metadata."""
    page_num: int             # 1-based
    doc_type: str             # DocumentType value
    direction: str            # ReadingDirection value
    width: float              # PDF page width in points
    height: float             # PDF page height in points
    text_coverage: float      # Ratio of chars found in text layer (0–1)
    image_path: Optional[str] = None
    preprocessed_image_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_num": self.page_num,
            "doc_type": self.doc_type,
            "direction": self.direction,
            "width": self.width,
            "height": self.height,
            "text_coverage": self.text_coverage,
            "image_path": self.image_path,
            "preprocessed_image_path": self.preprocessed_image_path,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PageInfo":
        return PageInfo(**d)


@dataclass
class PipelineContext:
    """Carries all state through the pipeline."""
    pdf_path: str
    work_dir: str
    status: str = ProcessingStatus.RECEIVED
    doc_type: Optional[str] = None        # Overall document type
    page_infos: List[PageInfo] = field(default_factory=list)
    text_blocks: List[TextBlock] = field(default_factory=list)
    options: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    skipped_count: int = 0

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def page_info(self, page_num: int) -> Optional[PageInfo]:
        for pi in self.page_infos:
            if pi.page_num == page_num:
                return pi
        return None

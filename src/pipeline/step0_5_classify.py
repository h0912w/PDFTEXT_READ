"""
Step 0.5: Document type classification and per-page strategy decision.
에이전트(LLM) 담당 영역.

Claude Vision이 각 페이지 이미지를 직접 보고 판정한다:
  - doc_type : DIGITAL | SCANNED | HYBRID
  - direction: LEFT_TO_RIGHT | TOP_TO_BOTTOM

LLM 호출 실패 시 규칙 기반 fallback으로 자동 전환한다.

State transition: RECEIVED → CLASSIFIED
"""
from __future__ import annotations

import json
import os
import tempfile
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
from src.utils.image_utils import render_pdf_page
from src.utils.llm_client import ask_json
from src.utils.logger import get_logger

# 분류용 저해상도 렌더링 DPI (LLM 비전 전용, 빠른 처리를 위해 낮게 설정)
_CLASSIFY_DPI = 72

# Fallback 규칙: 글자 수 임계값
_DIGITAL_CHAR_MIN = 80
_SCANNED_CHAR_MAX = 10

_CLASSIFY_PROMPT = """\
당신은 PDF 문서 분류 전문가입니다. 이 PDF 페이지 이미지를 분석하여 아래 JSON 형식으로만 응답하세요.

분류 기준:
- doc_type
  • DIGITAL  : 디지털 생성 PDF. 텍스트가 선명하고 균일한 폰트, 깔끔한 레이아웃.
  • SCANNED  : 종이 문서를 스캔/촬영한 것. 잡음, 기울기, 불균일한 밝기 등이 보임.
  • HYBRID   : 일부 페이지는 디지털, 일부는 스캔인 혼합 문서.

- direction (이 페이지의 주 읽기 방향)
  • LEFT_TO_RIGHT : 가로 행 단위로 읽는 일반적인 방식 (한국어, 영어 등 대부분)
  • TOP_TO_BOTTOM : 세로 열 단위로 읽는 방식 (일부 세로쓰기 문서)

응답 형식 (JSON만, 다른 텍스트 없이):
{
  "doc_type": "DIGITAL" | "SCANNED" | "HYBRID",
  "direction": "LEFT_TO_RIGHT" | "TOP_TO_BOTTOM",
  "reasoning": "판정 근거를 한 문장으로"
}
"""


def run(ctx: PipelineContext) -> PipelineContext:
    """LLM을 사용해 각 페이지를 분류하고 읽기 방향을 결정한다."""
    logger = get_logger()
    logger.info("Step 0.5: LLM으로 문서 분류 중…")

    try:
        with pdfplumber.open(ctx.pdf_path) as pdf:
            total = len(pdf.pages)
            target_pages = parse_page_range(ctx.options.get("page_range"), total)
            logger.info(f"총 {total}페이지, 대상: {target_pages}")

            page_infos: List[PageInfo] = []

            with tempfile.TemporaryDirectory() as tmpdir:
                for page_num in target_pages:
                    page = pdf.pages[page_num - 1]
                    width = float(page.width)
                    height = float(page.height)

                    # 분류용 저해상도 이미지 렌더링
                    tmp_img = os.path.join(tmpdir, f"cls_page_{page_num}.png")
                    render_pdf_page(ctx.pdf_path, page_num - 1, tmp_img, dpi=_CLASSIFY_DPI)

                    # 텍스트 레이어 글자 수 (LLM 판단 보조 정보)
                    words = page.extract_words() or []
                    char_count = sum(len(w["text"]) for w in words)
                    coverage = _compute_coverage(char_count, width, height)

                    # LLM 판정 (실패 시 규칙 기반 fallback)
                    forced_dir = ctx.options.get("force_direction")
                    info = _classify_with_llm(
                        page_num, tmp_img, width, height, coverage,
                        char_count, forced_dir, logger
                    )
                    page_infos.append(info)

                    logger.info(
                        f"  Page {page_num}: type={info.doc_type}, "
                        f"dir={info.direction}, coverage={info.text_coverage:.2f}"
                    )

        ctx.page_infos = page_infos
        ctx.doc_type = _infer_overall_type(page_infos)
        ctx.status = ProcessingStatus.CLASSIFIED

        _save(ctx)
        logger.info(f"Step 0.5 완료. 전체 유형: {ctx.doc_type}, Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 0.5 실패: {exc}")
        logger.error(f"Step 0.5 실패: {exc}", exc_info=True)

    return ctx


# ── LLM 판정 ─────────────────────────────────────────────────────────────

def _classify_with_llm(
    page_num: int,
    img_path: str,
    width: float,
    height: float,
    coverage: float,
    char_count: int,
    forced_dir: str | None,
    logger,
) -> PageInfo:
    """Claude Vision으로 페이지를 분류한다. 실패 시 규칙 기반 fallback."""

    # LLM fallback 기본값 (규칙 기반)
    fallback_type = _rule_based_type(char_count)
    fallback_dir = forced_dir or ReadingDirection.LEFT_TO_RIGHT.value

    fallback = {
        "doc_type": fallback_type,
        "direction": fallback_dir,
        "reasoning": "LLM 호출 실패 – 규칙 기반 fallback 적용",
    }

    try:
        result = ask_json(_CLASSIFY_PROMPT, image_path=img_path, fallback=None)
    except Exception as exc:
        logger.warning(f"  Page {page_num}: LLM 분류 실패 ({exc}), fallback 적용.")
        result = fallback

    # 값 유효성 검증
    doc_type = _validated(result.get("doc_type"), [t.value for t in DocumentType], fallback_type)
    direction = forced_dir or _validated(
        result.get("direction"),
        [d.value for d in ReadingDirection],
        fallback_dir,
    )

    if result.get("reasoning"):
        logger.debug(f"  Page {page_num} LLM 근거: {result['reasoning']}")

    return PageInfo(
        page_num=page_num,
        doc_type=doc_type,
        direction=direction,
        width=width,
        height=height,
        text_coverage=coverage,
    )


# ── 헬퍼 ─────────────────────────────────────────────────────────────────

def _validated(value, allowed: list, default: str) -> str:
    """LLM 반환값이 허용 범위 내에 있는지 확인하고, 아니면 default 반환."""
    if isinstance(value, str) and value.upper() in [a.upper() for a in allowed]:
        return value.upper()
    return default


def _rule_based_type(char_count: int) -> str:
    """LLM 실패 시 글자 수 기반 간단 분류 (fallback 전용)."""
    if char_count >= _DIGITAL_CHAR_MIN:
        return DocumentType.DIGITAL.value
    if char_count <= _SCANNED_CHAR_MAX:
        return DocumentType.SCANNED.value
    return DocumentType.HYBRID.value


def _compute_coverage(char_count: int, width: float, height: float) -> float:
    area = width * height
    if area <= 0:
        return 0.0
    return min(1.0, char_count / max(1.0, area / 200.0))


def _infer_overall_type(page_infos: List[PageInfo]) -> str:
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
        "classifier": "llm",
        "pages": [pi.to_dict() for pi in ctx.page_infos],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

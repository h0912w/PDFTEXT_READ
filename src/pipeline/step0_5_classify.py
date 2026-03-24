"""
Step 0.5: Document type classification and per-page strategy decision.
[에이전트(Claude Code) 판단 영역]

스크립트 역할:
  1. 각 페이지를 저해상도 이미지로 렌더링 (Claude가 볼 수 있도록)
  2. 텍스트 레이어 글자 수 측정
  3. 판단에 필요한 데이터를 classify_input.json 에 저장
  4. Claude가 작성한 classify_decision.json 을 읽어 PageInfo 구성

Claude Code 담당:
  - classify_input.json 의 이미지 경로와 데이터를 보고
  - 각 페이지의 doc_type / direction 을 판정
  - classify_decision.json 에 결과 작성

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
from src.utils.logger import get_logger

_CLASSIFY_DPI = 72  # 판단용 저해상도 렌더링


def run(ctx: PipelineContext) -> PipelineContext:
    """
    페이지별 분류 데이터를 수집하고, Claude의 판단 결과를 읽어 PageInfo를 구성한다.
    """
    logger = get_logger()
    logger.info("Step 0.5: 문서 분류 데이터 수집 중…")

    try:
        classify_images_dir = os.path.join(ctx.work_dir, "classify_images")
        os.makedirs(classify_images_dir, exist_ok=True)

        input_data = []

        with pdfplumber.open(ctx.pdf_path) as pdf:
            total = len(pdf.pages)
            target_pages = parse_page_range(ctx.options.get("page_range"), total)
            logger.info(f"총 {total}페이지, 대상: {target_pages}")

            for page_num in target_pages:
                page = pdf.pages[page_num - 1]
                width = float(page.width)
                height = float(page.height)
                words = page.extract_words() or []
                char_count = sum(len(w["text"]) for w in words)
                coverage = _compute_coverage(char_count, width, height)

                # 판단용 이미지 렌더링
                img_path = os.path.join(classify_images_dir, f"page_{page_num:03d}.png")
                render_pdf_page(ctx.pdf_path, page_num - 1, img_path, dpi=_CLASSIFY_DPI)

                input_data.append({
                    "page_num": page_num,
                    "width": width,
                    "height": height,
                    "char_count": char_count,
                    "text_coverage": round(coverage, 4),
                    "image_path": img_path,
                    "text_sample": " ".join(w["text"] for w in words[:20]),
                })

        # classify_input.json 저장 (Claude가 읽을 판단 재료)
        input_path = os.path.join(ctx.work_dir, "intermediate", "classify_input.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump({
                "pdf_path": ctx.pdf_path,
                "forced_direction": ctx.options.get("force_direction"),
                "pages": input_data,
            }, f, ensure_ascii=False, indent=2)

        logger.info(f"판단 재료 저장 완료: {input_path}")
        logger.info(">>> Claude Code가 classify_input.json 을 보고 classify_decision.json 을 작성해야 합니다.")

        # classify_decision.json 읽기 (Claude가 작성)
        decision_path = os.path.join(ctx.work_dir, "intermediate", "classify_decision.json")
        decision = _load_decision(decision_path, input_data, ctx.options.get("force_direction"))

        # PageInfo 구성
        page_infos: List[PageInfo] = []
        for page_data in input_data:
            page_num = page_data["page_num"]
            page_decision = decision.get(str(page_num), {})
            doc_type = _validated(page_decision.get("doc_type"), [t.value for t in DocumentType],
                                   _fallback_type(page_data["char_count"]))
            direction = ctx.options.get("force_direction") or _validated(
                page_decision.get("direction"),
                [d.value for d in ReadingDirection],
                ReadingDirection.LEFT_TO_RIGHT.value,
            )
            page_infos.append(PageInfo(
                page_num=page_num,
                doc_type=doc_type,
                direction=direction,
                width=page_data["width"],
                height=page_data["height"],
                text_coverage=page_data["text_coverage"],
            ))
            logger.info(f"  Page {page_num}: type={doc_type}, dir={direction}, "
                        f"coverage={page_data['text_coverage']:.2f}")

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


# ── decision JSON 로드 ────────────────────────────────────────────────────

def _load_decision(decision_path: str, input_data: list, forced_dir: str | None) -> dict:
    """
    Claude가 작성한 classify_decision.json 을 읽는다.
    파일이 없으면 fallback(규칙 기반)으로 자동 생성한다.
    """
    logger = get_logger()
    if os.path.exists(decision_path):
        with open(decision_path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"classify_decision.json 로드 완료.")
        return data.get("pages", data)

    # decision 파일 없음 → 규칙 기반 fallback 자동 적용
    logger.warning("classify_decision.json 없음 → 규칙 기반 fallback 적용.")
    fallback = {}
    for p in input_data:
        fallback[str(p["page_num"])] = {
            "doc_type": _fallback_type(p["char_count"]),
            "direction": forced_dir or ReadingDirection.LEFT_TO_RIGHT.value,
            "reasoning": "fallback: classify_decision.json 없음",
        }
    # fallback 결과를 파일로도 저장 (다음 실행 시 재사용)
    with open(decision_path, "w", encoding="utf-8") as f:
        json.dump({"source": "fallback", "pages": fallback}, f, ensure_ascii=False, indent=2)
    return fallback


# ── 헬퍼 ─────────────────────────────────────────────────────────────────

def _fallback_type(char_count: int) -> str:
    if char_count >= 80:
        return DocumentType.DIGITAL.value
    if char_count <= 10:
        return DocumentType.SCANNED.value
    return DocumentType.HYBRID.value


def _compute_coverage(char_count: int, width: float, height: float) -> float:
    area = width * height
    return min(1.0, char_count / max(1.0, area / 200.0)) if area > 0 else 0.0


def _validated(value, allowed: list, default: str) -> str:
    if isinstance(value, str) and value.upper() in [a.upper() for a in allowed]:
        return value.upper()
    return default


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
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "overall_type": ctx.doc_type,
            "status": ctx.status,
            "pages": [pi.to_dict() for pi in ctx.page_infos],
        }, f, ensure_ascii=False, indent=2)

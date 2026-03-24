"""
Step 5: Quality validation and final routing.
에이전트(LLM) 담당 영역.

Claude가 추출 결과 전체를 보고 품질을 평가하고
최종 상태(VALIDATED / APPROVED_WITH_WARNINGS)를 결정한다.

LLM 실패 시 비율 기반 fallback.

State transition: SKIP_RESOLVED → VALIDATED | APPROVED_WITH_WARNINGS
"""
from __future__ import annotations

import json
import os
from typing import List, Set

from src.models.state import PipelineContext, ProcessingStatus, TextStatus
from src.utils.llm_client import ask_json
from src.utils.logger import get_logger

_SAMPLE_FOR_LLM = 10    # LLM에 전달할 블록 샘플 수
# Fallback 임계값
_SKIP_WARN_RATIO = 0.20
_MIN_AVG_CONFIDENCE = 0.70

_VALIDATE_PROMPT = """\
당신은 PDF 텍스트 추출 품질 검사관입니다.
아래 통계와 샘플을 보고 추출 결과의 품질을 평가하세요.

=== 추출 통계 ===
문서 유형: {doc_type}
총 블록 수: {total}
SKIPPED 블록: {skipped} ({skip_ratio:.1%})
UNKNOWN 블록: {unknown}
평균 신뢰도: {avg_conf:.2f}
검토 필요 페이지: {review_pages}

=== 텍스트 샘플 (최대 {n}개) ===
{samples}

=== 판단 기준 ===
- VALIDATED         : 품질이 충분히 높아 자동 확정 가능
- APPROVED_WITH_WARNINGS : 사용 가능하지만 일부 검토 필요
- NEEDS_REVIEW      : 품질이 낮아 사람이 전체 검토해야 함

아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{
  "decision": "VALIDATED" | "APPROVED_WITH_WARNINGS" | "NEEDS_REVIEW",
  "reason": "판단 근거",
  "concerns": ["구체적 우려사항 1", "우려사항 2"]
}}
"""


def run(ctx: PipelineContext) -> PipelineContext:
    """LLM으로 추출 품질을 평가하고 최종 상태를 결정한다."""
    logger = get_logger()
    logger.info("Step 5: LLM으로 품질 검증 중…")

    try:
        total = len(ctx.text_blocks)
        skipped = sum(1 for tb in ctx.text_blocks if tb.status == TextStatus.SKIPPED.value)
        unknown = sum(1 for tb in ctx.text_blocks if tb.status == TextStatus.UNKNOWN.value)
        review_pages: Set[int] = set()
        confidences: List[float] = []

        for tb in ctx.text_blocks:
            if tb.review_required:
                review_pages.add(tb.page_num)
            if tb.status == TextStatus.OK.value:
                confidences.append(tb.confidence)

        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        skip_ratio = (skipped + unknown) / max(1, total)

        logger.info(
            f"  총 {total}블록, SKIPPED={skipped}, UNKNOWN={unknown}, "
            f"avg_conf={avg_conf:.2f}, 검토 페이지={sorted(review_pages)}"
        )

        # LLM 품질 평가
        decision = _evaluate_with_llm(ctx, total, skipped, unknown, skip_ratio, avg_conf, review_pages, logger)

        # NEEDS_REVIEW는 APPROVED_WITH_WARNINGS로 처리 (파이프라인은 계속 진행)
        if decision == "VALIDATED":
            ctx.status = ProcessingStatus.VALIDATED
        else:
            ctx.status = ProcessingStatus.APPROVED_WITH_WARNINGS
            if decision == "NEEDS_REVIEW":
                ctx.add_warning("LLM 평가: 전체 검토 필요 (NEEDS_REVIEW)")

        _save(ctx, review_pages, avg_conf, skip_ratio)
        logger.info(f"Step 5 완료. Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 5 실패: {exc}")
        logger.error(f"Step 5 실패: {exc}", exc_info=True)

    return ctx


# ── LLM 품질 평가 ─────────────────────────────────────────────────────────

def _evaluate_with_llm(
    ctx, total, skipped, unknown, skip_ratio, avg_conf, review_pages, logger
) -> str:
    """Claude에게 품질 판정을 요청한다. 실패 시 규칙 기반 fallback."""

    # 샘플 블록 생성 (OK, SKIPPED, UNKNOWN 혼합)
    samples = []
    for tb in ctx.text_blocks[:_SAMPLE_FOR_LLM]:
        samples.append(f"  [{tb.status}] p{tb.page_num}: {tb.text[:60]!r}  (conf={tb.confidence:.2f})")
    samples_text = "\n".join(samples) if samples else "(없음)"

    prompt = _VALIDATE_PROMPT.format(
        doc_type=ctx.doc_type or "UNKNOWN",
        total=total,
        skipped=skipped,
        unknown=unknown,
        skip_ratio=skip_ratio,
        avg_conf=avg_conf,
        review_pages=sorted(review_pages) or "없음",
        n=_SAMPLE_FOR_LLM,
        samples=samples_text,
    )

    # Fallback 결정 (규칙 기반)
    if skip_ratio > _SKIP_WARN_RATIO or avg_conf < _MIN_AVG_CONFIDENCE or total == 0:
        fallback_decision = "APPROVED_WITH_WARNINGS"
    else:
        fallback_decision = "VALIDATED"

    fallback = {
        "decision": fallback_decision,
        "reason": "LLM 실패 – 규칙 기반 fallback",
        "concerns": [],
    }

    try:
        result = ask_json(prompt, fallback=None)
        decision = result.get("decision", fallback_decision)
        reason = result.get("reason", "")
        concerns = result.get("concerns", [])

        logger.info(f"  LLM 품질 판정: {decision}")
        if reason:
            logger.info(f"  근거: {reason}")
        for c in concerns:
            ctx.add_warning(f"품질 우려: {c}")
            logger.warning(f"  우려: {c}")

        # 허용 값 검증
        if decision not in ("VALIDATED", "APPROVED_WITH_WARNINGS", "NEEDS_REVIEW"):
            decision = fallback_decision

        return decision

    except Exception as exc:
        logger.warning(f"  LLM 품질 평가 실패 ({exc}), fallback 적용: {fallback_decision}")
        return fallback_decision


def _save(ctx: PipelineContext, review_pages, avg_conf, skip_ratio) -> None:
    summary_path = os.path.join(ctx.work_dir, "intermediate", "summary.json")
    summary = {
        "status": ctx.status,
        "validator": "llm",
        "pdf_path": ctx.pdf_path,
        "doc_type": ctx.doc_type,
        "total_pages": len(ctx.page_infos),
        "total_blocks": len(ctx.text_blocks),
        "skipped_count": ctx.skipped_count,
        "skip_ratio": skip_ratio,
        "avg_confidence": avg_conf,
        "warnings": ctx.warnings,
        "errors": ctx.errors,
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    review_path = os.path.join(ctx.work_dir, "intermediate", "review_required_pages.json")
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump({"pages": sorted(review_pages)}, f, ensure_ascii=False, indent=2)

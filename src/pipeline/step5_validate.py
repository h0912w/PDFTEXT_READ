"""
Step 5: Quality validation and final routing.
[에이전트(Claude Code) 판단 영역]

스크립트 역할:
  1. 추출 통계 + 블록 샘플을 validate_input.json 에 저장
  2. Claude가 작성한 validate_decision.json 을 읽어 최종 상태 결정

Claude Code 담당:
  - validate_input.json 의 통계와 샘플을 보고
  - VALIDATED / APPROVED_WITH_WARNINGS / NEEDS_REVIEW 결정
  - validate_decision.json 에 결과 작성

State transition: SKIP_RESOLVED → VALIDATED | APPROVED_WITH_WARNINGS
"""
from __future__ import annotations

import json
import os
from typing import List, Set

from src.models.state import PipelineContext, ProcessingStatus, TextStatus
from src.utils.logger import get_logger

_SAMPLE_SIZE = 10
_SKIP_WARN_RATIO = 0.20
_MIN_AVG_CONFIDENCE = 0.70


def run(ctx: PipelineContext) -> PipelineContext:
    """추출 통계를 저장하고, Claude의 품질 판정을 읽어 최종 상태를 결정한다."""
    logger = get_logger()
    logger.info("Step 5: 품질 검증 데이터 수집 중…")

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

        # 샘플 블록 (OK + SKIPPED + UNKNOWN 혼합)
        samples = [
            {
                "order_index": tb.order_index,
                "page_num": tb.page_num,
                "text": tb.text[:80],
                "confidence": round(tb.confidence, 3),
                "status": tb.status,
                "source": tb.source,
            }
            for tb in ctx.text_blocks[:_SAMPLE_SIZE]
        ]

        # validate_input.json 저장 (Claude 판단 재료)
        input_path = os.path.join(ctx.work_dir, "intermediate", "validate_input.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump({
                "doc_type": ctx.doc_type,
                "total_blocks": total,
                "skipped": skipped,
                "unknown": unknown,
                "skip_ratio": round(skip_ratio, 4),
                "avg_confidence": round(avg_conf, 4),
                "review_pages": sorted(review_pages),
                "warnings_so_far": ctx.warnings,
                "samples": samples,
            }, f, ensure_ascii=False, indent=2)

        logger.info(f"품질 판단 재료 저장: {input_path}")
        logger.info(f"  통계: total={total}, skip={skipped}, unknown={unknown}, "
                    f"avg_conf={avg_conf:.2f}, skip_ratio={skip_ratio:.1%}")
        logger.info(">>> Claude Code가 validate_input.json 을 보고 validate_decision.json 을 작성해야 합니다.")

        # validate_decision.json 읽기 (Claude 작성)
        decision_path = os.path.join(ctx.work_dir, "intermediate", "validate_decision.json")
        decision = _load_decision(decision_path, skip_ratio, avg_conf, total)

        raw_decision = decision.get("decision", "VALIDATED")
        reason = decision.get("reason", "")
        concerns = decision.get("concerns", [])

        for c in concerns:
            ctx.add_warning(f"품질 우려: {c}")

        # NEEDS_REVIEW → 파이프라인은 계속 진행하되 APPROVED_WITH_WARNINGS 처리
        if raw_decision == "VALIDATED":
            ctx.status = ProcessingStatus.VALIDATED
        else:
            ctx.status = ProcessingStatus.APPROVED_WITH_WARNINGS
            if raw_decision == "NEEDS_REVIEW":
                ctx.add_warning("전체 검토 필요 (NEEDS_REVIEW)")

        logger.info(f"  품질 판정: {raw_decision} → {ctx.status}")
        if reason:
            logger.info(f"  근거: {reason}")

        _save(ctx, review_pages, avg_conf, skip_ratio)
        logger.info(f"Step 5 완료. Status: {ctx.status}")

    except Exception as exc:
        ctx.status = ProcessingStatus.FAILED
        ctx.add_error(f"Step 5 실패: {exc}")
        logger.error(f"Step 5 실패: {exc}", exc_info=True)

    return ctx


def _load_decision(decision_path: str, skip_ratio: float, avg_conf: float, total: int) -> dict:
    logger = get_logger()
    if os.path.exists(decision_path):
        with open(decision_path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("validate_decision.json 로드 완료.")
        return data

    logger.warning("validate_decision.json 없음 → 규칙 기반 fallback 적용.")
    if skip_ratio > _SKIP_WARN_RATIO or avg_conf < _MIN_AVG_CONFIDENCE or total == 0:
        fallback_decision = "APPROVED_WITH_WARNINGS"
        concerns = []
        if skip_ratio > _SKIP_WARN_RATIO:
            concerns.append(f"높은 스킵 비율: {skip_ratio:.1%}")
        if avg_conf < _MIN_AVG_CONFIDENCE:
            concerns.append(f"낮은 평균 신뢰도: {avg_conf:.2f}")
    else:
        fallback_decision = "VALIDATED"
        concerns = []

    result = {
        "decision": fallback_decision,
        "reason": "fallback: validate_decision.json 없음",
        "concerns": concerns,
    }
    with open(decision_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def _save(ctx, review_pages, avg_conf, skip_ratio):
    summary_path = os.path.join(ctx.work_dir, "intermediate", "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "status": ctx.status,
            "pdf_path": ctx.pdf_path,
            "doc_type": ctx.doc_type,
            "total_pages": len(ctx.page_infos),
            "total_blocks": len(ctx.text_blocks),
            "skipped_count": ctx.skipped_count,
            "skip_ratio": skip_ratio,
            "avg_confidence": avg_conf,
            "warnings": ctx.warnings,
            "errors": ctx.errors,
        }, f, ensure_ascii=False, indent=2)

    review_path = os.path.join(ctx.work_dir, "intermediate", "review_required_pages.json")
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump({"pages": sorted(review_pages)}, f, ensure_ascii=False, indent=2)

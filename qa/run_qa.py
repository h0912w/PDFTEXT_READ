"""
QA Regression Test Runner
에이전트(LLM) 담당 영역: 실패 원인 분류 및 수정 피드백.

절차:
  1. qa/samples/ 의 PDF마다 파이프라인 실행
  2. 결과 CSV를 qa/answers/ 정답과 비교
  3. 불일치 발견 시 Claude에게 실패 원인 분류 요청
  4. qa/reports/ 에 리포트 저장

통과 기준 (testing-and-qa.md):
  - 모든 샘플 PASS
  - skipped_count = 0
  - 정답 100% 일치

Usage:
    python qa/run_qa.py
    python qa/run_qa.py --sample my_sample.pdf
    python qa/run_qa.py --verbose

정답 파일 형식 (qa/answers/<stem>.json):
{
  "doc_type": "DIGITAL",
  "blocks": [
    {"page_num": 1, "order_index": 0, "text": "기대 텍스트"},
    ...
  ]
}
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import run_pipeline
from src.utils.llm_client import ask_json
from src.utils.logger import setup_logger

_SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "samples")
_ANSWERS_DIR = os.path.join(os.path.dirname(__file__), "answers")
_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
_QA_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "_qa_output")

_PASS = "PASS"
_FAIL = "FAIL"

_FAILURE_CLASSIFY_PROMPT = """\
당신은 PDF 텍스트 추출 파이프라인의 QA 실패 분석 전문가입니다.
아래 정보를 바탕으로 실패 원인을 분류하고 수정 방향을 제시하세요.

=== 샘플 정보 ===
샘플명: {sample}
문서 유형: {doc_type}
불일치 수: {mismatch_count}

=== 불일치 목록 (최대 10개) ===
{mismatches}

=== 분류 기준 ===
- CODE_ISSUE       : 추출/정합/정렬 알고리즘의 버그
- DOCUMENT_ISSUE   : 이 특정 문서 특성(폰트, 인코딩, 레이아웃)으로 인한 문제
- RULE_ISSUE       : 분류/방향 판정/스킵 규칙이 이 케이스에 맞지 않음
- ANSWER_DATA_ISSUE: 정답 데이터 자체가 잘못되었을 가능성

아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{
  "cause": "CODE_ISSUE" | "DOCUMENT_ISSUE" | "RULE_ISSUE" | "ANSWER_DATA_ISSUE",
  "explanation": "원인 상세 설명",
  "fix_suggestion": "구체적인 수정 방향"
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="QA 회귀 테스트 러너")
    parser.add_argument("--sample", help="특정 샘플만 실행 (예: my_doc.pdf)")
    parser.add_argument("--verbose", action="store_true", help="상세 불일치 출력")
    args = parser.parse_args()

    os.makedirs(_REPORTS_DIR, exist_ok=True)
    os.makedirs(_QA_OUTPUT_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(_REPORTS_DIR, f"qa_{timestamp}.log")
    logger = setup_logger("qa", log_file=log_path)

    logger.info("=" * 60)
    logger.info("QA 회귀 테스트 시작")
    logger.info("=" * 60)

    samples = [os.path.join(_SAMPLES_DIR, args.sample)] if args.sample else _collect_samples()

    if not samples:
        print("[QA] qa/samples/ 에 PDF가 없습니다. PDF와 정답 JSON을 추가하세요.")
        return 1

    results: List[Dict[str, Any]] = []

    for sample_path in samples:
        stem = os.path.splitext(os.path.basename(sample_path))[0]
        answer_path = os.path.join(_ANSWERS_DIR, f"{stem}.json")

        logger.info(f"\n--- 샘플: {stem} ---")

        # 정답 파일 확인
        if not os.path.exists(answer_path):
            result = _make_result(stem, _FAIL, "MISSING_ANSWER",
                                  f"정답 파일 없음: {answer_path}")
            results.append(result)
            logger.error(f"  FAIL: {result['reason']}")
            continue

        # 파이프라인 실행 (QA 모드: confidence_threshold=0 → 스킵 없이 전부 추출)
        sample_out_dir = os.path.join(_QA_OUTPUT_DIR, stem)
        try:
            rc = run_pipeline(
                sample_path,
                output_dir=sample_out_dir,
                options={"confidence_threshold": 0.0},
            )
        except Exception as exc:
            result = _make_result(stem, _FAIL, "PIPELINE_EXCEPTION", str(exc))
            results.append(result)
            logger.error(f"  FAIL: 파이프라인 예외: {exc}")
            continue

        if rc != 0:
            result = _make_result(stem, _FAIL, "PIPELINE_FAILED", "파이프라인 비정상 종료")
            results.append(result)
            logger.error("  FAIL: 파이프라인 비정상 종료")
            continue

        # CSV 탐색
        csv_path = _find_output_csv(sample_out_dir)
        if not csv_path:
            result = _make_result(stem, _FAIL, "CSV_NOT_FOUND", "final_output.csv 생성 안 됨")
            results.append(result)
            logger.error("  FAIL: CSV 없음")
            continue

        actual_blocks = _load_csv(csv_path)
        expected = _load_answer(answer_path)

        if expected is None:
            result = _make_result(stem, _FAIL, "ANSWER_PARSE_ERROR", "정답 JSON 파싱 실패")
            results.append(result)
            continue

        # 스킵 수 검사 (QA 통과 조건: 0이어야 함)
        skipped = sum(1 for b in actual_blocks if b.get("status") in ("SKIPPED", "UNKNOWN"))
        if skipped > 0:
            result = _make_result(
                stem, _FAIL, "SKIPPED_ITEMS",
                f"스킵 {skipped}개 존재 (QA 조건: 0개)",
                skipped_count=skipped,
            )
            results.append(result)
            logger.error(f"  FAIL: 스킵 {skipped}개")
            continue

        # 텍스트 비교
        mismatches = _compare_blocks(actual_blocks, expected["blocks"], args.verbose, logger)

        if mismatches:
            # LLM에게 실패 원인 분류 요청
            failure_analysis = _classify_failure_with_llm(
                stem, expected.get("doc_type", "UNKNOWN"), mismatches, logger
            )
            result = _make_result(
                stem, _FAIL, failure_analysis.get("cause", "UNKNOWN"),
                failure_analysis.get("explanation", f"{len(mismatches)}개 불일치"),
                mismatches=mismatches,
                skipped_count=skipped,
                fix_suggestion=failure_analysis.get("fix_suggestion", ""),
            )
            results.append(result)
            logger.error(
                f"  FAIL: {len(mismatches)}개 불일치 "
                f"[{failure_analysis.get('cause')}] {failure_analysis.get('explanation','')}"
            )
            if failure_analysis.get("fix_suggestion"):
                logger.info(f"  수정 방향: {failure_analysis['fix_suggestion']}")
        else:
            result = _make_result(stem, _PASS, "", "전체 일치")
            results.append(result)
            logger.info(f"  PASS: {stem}")

    # 리포트 작성
    report_path = os.path.join(_REPORTS_DIR, f"qa_report_{timestamp}.json")
    _write_report(results, report_path)

    passed = sum(1 for r in results if r["verdict"] == _PASS)
    failed = len(results) - passed
    overall = "QA_PASSED" if failed == 0 else "QA_FAILED"

    print("\n" + "=" * 60)
    print(f"QA 결과: {overall}")
    print(f"  전체: {len(results)}  PASS: {passed}  FAIL: {failed}")
    print(f"  리포트: {report_path}")
    if failed > 0:
        print("\n실패 목록:")
        for r in results:
            if r["verdict"] == _FAIL:
                print(f"  ✗ {r['sample']}: [{r['reason_code']}] {r['reason']}")
                if r.get("fix_suggestion"):
                    print(f"    → {r['fix_suggestion']}")
    print("=" * 60)

    return 0 if overall == "QA_PASSED" else 1


# ── LLM 실패 원인 분류 ────────────────────────────────────────────────────

def _classify_failure_with_llm(
    sample: str, doc_type: str, mismatches: List[Dict], logger
) -> Dict[str, str]:
    """Claude에게 QA 실패 원인을 분류하도록 요청한다."""

    mm_text = "\n".join(
        f"  [{i}] 기대: {m.get('expected','')!r}  →  실제: {m.get('actual','')!r}"
        for i, m in enumerate(mismatches[:10])
    )
    if not mm_text:
        # COUNT_MISMATCH만 있는 경우
        cnt = next((m for m in mismatches if m.get("type") == "COUNT_MISMATCH"), {})
        mm_text = f"  블록 수 불일치: 기대={cnt.get('expected_count')}, 실제={cnt.get('actual_count')}"

    prompt = _FAILURE_CLASSIFY_PROMPT.format(
        sample=sample,
        doc_type=doc_type,
        mismatch_count=len(mismatches),
        mismatches=mm_text,
    )

    fallback = {
        "cause": "CODE_ISSUE",
        "explanation": "LLM 분류 실패 – 수동 확인 필요",
        "fix_suggestion": "불일치 목록을 직접 검토하세요.",
    }

    try:
        result = ask_json(prompt, fallback=None)
        cause = result.get("cause", "CODE_ISSUE")
        allowed = ("CODE_ISSUE", "DOCUMENT_ISSUE", "RULE_ISSUE", "ANSWER_DATA_ISSUE")
        if cause not in allowed:
            cause = "CODE_ISSUE"
        result["cause"] = cause
        return result
    except Exception as exc:
        logger.warning(f"  LLM 실패 분류 오류 ({exc}), fallback 반환.")
        return fallback


# ── 유틸 ─────────────────────────────────────────────────────────────────

def _collect_samples() -> List[str]:
    if not os.path.isdir(_SAMPLES_DIR):
        return []
    return sorted(
        os.path.join(_SAMPLES_DIR, f)
        for f in os.listdir(_SAMPLES_DIR)
        if f.lower().endswith(".pdf")
    )


def _find_output_csv(output_dir: str) -> Optional[str]:
    for root, _, files in os.walk(output_dir):
        if "final_output.csv" in files:
            return os.path.join(root, "final_output.csv")
    return None


def _load_csv(csv_path: str) -> List[Dict[str, Any]]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _load_answer(answer_path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(answer_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _compare_blocks(
    actual: List[Dict], expected_blocks: List[Dict], verbose: bool, logger
) -> List[Dict[str, Any]]:
    mismatches = []
    if len(actual) != len(expected_blocks):
        mismatches.append({
            "type": "COUNT_MISMATCH",
            "actual_count": len(actual),
            "expected_count": len(expected_blocks),
        })
        logger.warning(f"  블록 수 불일치: actual={len(actual)}, expected={len(expected_blocks)}")

    for i, (act, exp) in enumerate(zip(actual, expected_blocks)):
        act_text = str(act.get("text", "")).strip()
        exp_text = str(exp.get("text", "")).strip()
        if act_text != exp_text:
            mm = {"type": "TEXT_MISMATCH", "index": i, "actual": act_text, "expected": exp_text}
            mismatches.append(mm)
            if verbose:
                logger.warning(f"  [{i}] 기대={exp_text!r}  실제={act_text!r}")
    return mismatches


def _make_result(
    sample: str, verdict: str, reason_code: str, reason: str,
    mismatches: Optional[List] = None, skipped_count: int = 0,
    fix_suggestion: str = "",
) -> Dict[str, Any]:
    return {
        "sample": sample,
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
        "fix_suggestion": fix_suggestion,
        "skipped_count": skipped_count,
        "mismatches": mismatches or [],
        "timestamp": datetime.now().isoformat(),
    }


def _write_report(results: List[Dict], path: str) -> None:
    passed = sum(1 for r in results if r["verdict"] == _PASS)
    report = {
        "overall": "QA_PASSED" if passed == len(results) else "QA_FAILED",
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "generated_at": datetime.now().isoformat(),
        "failure_analyst": "llm",
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())

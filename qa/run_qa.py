"""
QA Regression Test Runner
[에이전트(Claude Code) 판단 영역: 실패 원인 분류]

절차:
  1. qa/samples/ 의 PDF마다 파이프라인 실행
  2. 결과 CSV를 qa/answers/ 정답과 비교
  3. 불일치 발견 시 qa_failure_input.json 저장
     → Claude Code가 읽고 qa_failure_analysis.json 작성
  4. qa/reports/ 에 최종 리포트 저장

통과 기준:
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
from src.utils.logger import setup_logger

_SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "samples")
_ANSWERS_DIR = os.path.join(os.path.dirname(__file__), "answers")
_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
_QA_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "_qa_output")

_PASS = "PASS"
_FAIL = "FAIL"


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
        print("[QA] qa/samples/ 에 PDF가 없습니다.")
        return 1

    results: List[Dict[str, Any]] = []
    failure_inputs: List[Dict] = []  # Claude가 분류해야 할 실패 목록

    for sample_path in samples:
        stem = os.path.splitext(os.path.basename(sample_path))[0]
        answer_path = os.path.join(_ANSWERS_DIR, f"{stem}.json")

        logger.info(f"\n--- 샘플: {stem} ---")

        if not os.path.exists(answer_path):
            result = _make_result(stem, _FAIL, "MISSING_ANSWER", f"정답 파일 없음: {answer_path}")
            results.append(result)
            logger.error(f"  FAIL: {result['reason']}")
            continue

        sample_out_dir = os.path.join(_QA_OUTPUT_DIR, stem)
        try:
            rc = run_pipeline(sample_path, output_dir=sample_out_dir,
                              options={"confidence_threshold": 0.0})
        except Exception as exc:
            result = _make_result(stem, _FAIL, "PIPELINE_EXCEPTION", str(exc))
            results.append(result)
            logger.error(f"  FAIL: 파이프라인 예외: {exc}")
            continue

        if rc != 0:
            result = _make_result(stem, _FAIL, "PIPELINE_FAILED", "파이프라인 비정상 종료")
            results.append(result)
            continue

        csv_path = _find_output_csv(sample_out_dir)
        if not csv_path:
            result = _make_result(stem, _FAIL, "CSV_NOT_FOUND", "final_output.csv 생성 안 됨")
            results.append(result)
            continue

        actual_blocks = _load_csv(csv_path)
        expected = _load_answer(answer_path)
        if expected is None:
            result = _make_result(stem, _FAIL, "ANSWER_PARSE_ERROR", "정답 JSON 파싱 실패")
            results.append(result)
            continue

        skipped = sum(1 for b in actual_blocks if b.get("status") in ("SKIPPED", "UNKNOWN"))
        if skipped > 0:
            result = _make_result(stem, _FAIL, "SKIPPED_ITEMS",
                                  f"스킵 {skipped}개 (QA 조건: 0개)", skipped_count=skipped)
            results.append(result)
            logger.error(f"  FAIL: 스킵 {skipped}개")
            continue

        mismatches = _compare_blocks(actual_blocks, expected["blocks"], args.verbose, logger)

        if mismatches:
            # 실패 입력 저장 (Claude가 원인 분류)
            failure_input = {
                "sample": stem,
                "doc_type": expected.get("doc_type", "UNKNOWN"),
                "mismatch_count": len(mismatches),
                "mismatches": mismatches[:20],
            }
            failure_inputs.append(failure_input)

            result = _make_result(stem, _FAIL, "TEXT_MISMATCH",
                                  f"{len(mismatches)}개 불일치", mismatches=mismatches)
            results.append(result)
            logger.error(f"  FAIL: {len(mismatches)}개 불일치")
        else:
            result = _make_result(stem, _PASS, "", "전체 일치")
            results.append(result)
            logger.info(f"  PASS: {stem}")

    # 실패 원인 분류 (Claude Code가 담당)
    if failure_inputs:
        failure_input_path = os.path.join(_REPORTS_DIR, f"qa_failure_input_{timestamp}.json")
        with open(failure_input_path, "w", encoding="utf-8") as f:
            json.dump({"failures": failure_inputs}, f, ensure_ascii=False, indent=2)
        logger.info(f"\n>>> Claude Code가 {failure_input_path} 를 읽고 실패 원인을 분류해야 합니다.")
        logger.info(f">>> 결과를 qa_failure_analysis_{timestamp}.json 에 작성해 주세요.")

        # 분석 결과 파일이 있으면 결과에 반영
        analysis_path = os.path.join(_REPORTS_DIR, f"qa_failure_analysis_{timestamp}.json")
        if os.path.exists(analysis_path):
            with open(analysis_path, encoding="utf-8") as f:
                analysis = json.load(f)
            _apply_analysis(results, analysis)

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
                if r.get("cause"):
                    print(f"    원인: {r['cause']} – {r.get('fix_suggestion','')}")
    print("=" * 60)

    return 0 if overall == "QA_PASSED" else 1


# ── 헬퍼 ─────────────────────────────────────────────────────────────────

def _apply_analysis(results: List[Dict], analysis: Dict) -> None:
    """Claude가 작성한 실패 분석을 결과 목록에 반영한다."""
    by_sample = {a["sample"]: a for a in analysis.get("analyses", [])}
    for r in results:
        if r["verdict"] == _FAIL and r["sample"] in by_sample:
            a = by_sample[r["sample"]]
            r["cause"] = a.get("cause", "")
            r["fix_suggestion"] = a.get("fix_suggestion", "")


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


def _compare_blocks(actual, expected_blocks, verbose, logger) -> List[Dict]:
    mismatches = []
    if len(actual) != len(expected_blocks):
        mismatches.append({"type": "COUNT_MISMATCH",
                           "actual_count": len(actual),
                           "expected_count": len(expected_blocks)})
        logger.warning(f"  블록 수 불일치: actual={len(actual)}, expected={len(expected_blocks)}")
    for i, (act, exp) in enumerate(zip(actual, expected_blocks)):
        at = str(act.get("text", "")).strip()
        et = str(exp.get("text", "")).strip()
        if at != et:
            mismatches.append({"type": "TEXT_MISMATCH", "index": i, "actual": at, "expected": et})
            if verbose:
                logger.warning(f"  [{i}] 기대={et!r}  실제={at!r}")
    return mismatches


def _make_result(sample, verdict, reason_code, reason,
                 mismatches=None, skipped_count=0) -> Dict[str, Any]:
    return {
        "sample": sample, "verdict": verdict, "reason_code": reason_code,
        "reason": reason, "skipped_count": skipped_count,
        "mismatches": mismatches or [], "timestamp": datetime.now().isoformat(),
        "cause": "", "fix_suggestion": "",
    }


def _write_report(results: List[Dict], path: str) -> None:
    passed = sum(1 for r in results if r["verdict"] == _PASS)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "overall": "QA_PASSED" if passed == len(results) else "QA_FAILED",
            "total": len(results), "passed": passed,
            "failed": len(results) - passed,
            "generated_at": datetime.now().isoformat(),
            "failure_analyst": "claude_code",
            "results": results,
        }, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())

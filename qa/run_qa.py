"""
QA Regression Test Runner

Runs the full extraction pipeline on every sample PDF in qa/samples/,
compares the result against the expected answer in qa/answers/,
and writes a report to qa/reports/.

Usage:
    python qa/run_qa.py
    python qa/run_qa.py --sample my_sample.pdf
    python qa/run_qa.py --verbose

Pass/Fail criteria (from testing-and-qa.md):
  - All registered samples must PASS.
  - skipped_count must be 0 for PASS.
  - Extracted text must match answer 100%.
  - QA report must be generated.

Answer file format (qa/answers/<stem>.json):
{
  "doc_type": "DIGITAL",
  "blocks": [
    {"page_num": 1, "text": "expected text", "order_index": 0},
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

# Make project root importable
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
    parser = argparse.ArgumentParser(description="QA regression test runner.")
    parser.add_argument("--sample", help="Run only this sample filename (e.g., my_doc.pdf).")
    parser.add_argument("--verbose", action="store_true", help="Print detailed diff output.")
    args = parser.parse_args()

    os.makedirs(_REPORTS_DIR, exist_ok=True)
    os.makedirs(_QA_OUTPUT_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(_REPORTS_DIR, f"qa_{timestamp}.log")
    logger = setup_logger("qa", log_file=log_path)

    logger.info("=" * 60)
    logger.info("QA Regression Test Start")
    logger.info("=" * 60)

    # Collect sample PDFs
    if args.sample:
        samples = [os.path.join(_SAMPLES_DIR, args.sample)]
    else:
        samples = _collect_samples()

    if not samples:
        logger.error("No sample PDFs found in qa/samples/. Add PDFs and their answer JSONs.")
        print("[QA] No samples found. Add PDFs to qa/samples/ and answers to qa/answers/.")
        return 1

    results: List[Dict[str, Any]] = []

    for sample_path in samples:
        stem = os.path.splitext(os.path.basename(sample_path))[0]
        answer_path = os.path.join(_ANSWERS_DIR, f"{stem}.json")

        logger.info(f"\n--- Sample: {stem} ---")

        # Check answer file
        if not os.path.exists(answer_path):
            result = _make_result(stem, _FAIL, "MISSING_ANSWER", f"Answer file not found: {answer_path}")
            results.append(result)
            logger.error(f"  FAIL: {result['reason']}")
            continue

        # Run pipeline
        sample_output_dir = os.path.join(_QA_OUTPUT_DIR, stem)
        try:
            rc = run_pipeline(
                sample_path,
                output_dir=sample_output_dir,
                options={"confidence_threshold": 0.0},  # Don't skip in QA
            )
        except Exception as exc:
            result = _make_result(stem, _FAIL, "PIPELINE_EXCEPTION", str(exc))
            results.append(result)
            logger.error(f"  FAIL: Pipeline exception: {exc}")
            continue

        if rc != 0:
            result = _make_result(stem, _FAIL, "PIPELINE_FAILED", "Pipeline returned non-zero exit code.")
            results.append(result)
            logger.error(f"  FAIL: Pipeline non-zero exit.")
            continue

        # Find the CSV output
        csv_path = _find_output_csv(sample_output_dir)
        if not csv_path:
            result = _make_result(stem, _FAIL, "CSV_NOT_FOUND", "final_output.csv not generated.")
            results.append(result)
            logger.error(f"  FAIL: CSV not found.")
            continue

        # Load actual vs expected
        actual_blocks = _load_csv(csv_path)
        expected = _load_answer(answer_path)

        if expected is None:
            result = _make_result(stem, _FAIL, "ANSWER_PARSE_ERROR", "Failed to parse answer JSON.")
            results.append(result)
            logger.error(f"  FAIL: Answer parse error.")
            continue

        # Check skipped count
        skipped = sum(1 for b in actual_blocks if b.get("status") in ("SKIPPED", "UNKNOWN"))
        if skipped > 0:
            result = _make_result(
                stem, _FAIL, "SKIPPED_ITEMS",
                f"{skipped} skipped item(s) remain (QA requires 0).",
                skipped_count=skipped,
            )
            results.append(result)
            logger.error(f"  FAIL: {skipped} skipped item(s).")
            continue

        # Compare texts
        mismatches = _compare_blocks(actual_blocks, expected["blocks"], args.verbose, logger)

        if mismatches:
            result = _make_result(
                stem, _FAIL, "TEXT_MISMATCH",
                f"{len(mismatches)} mismatch(es) found.",
                mismatches=mismatches,
                skipped_count=skipped,
            )
            results.append(result)
            logger.error(f"  FAIL: {len(mismatches)} mismatch(es).")
        else:
            result = _make_result(stem, _PASS, "", "All blocks match.")
            results.append(result)
            logger.info(f"  PASS: {stem}")

    # Write QA report
    report_path = os.path.join(_REPORTS_DIR, f"qa_report_{timestamp}.json")
    _write_report(results, report_path)

    # Print summary
    passed = sum(1 for r in results if r["verdict"] == _PASS)
    failed = len(results) - passed
    overall = "QA_PASSED" if failed == 0 else "QA_FAILED"

    print("\n" + "=" * 60)
    print(f"QA Result: {overall}")
    print(f"  Total: {len(results)}, PASS: {passed}, FAIL: {failed}")
    print(f"  Report: {report_path}")
    print("=" * 60)

    logger.info(f"\nOverall: {overall} | PASS: {passed} | FAIL: {failed}")
    logger.info(f"Report written: {report_path}")

    return 0 if overall == "QA_PASSED" else 1


# ── Helpers ───────────────────────────────────────────────────────────────

def _collect_samples() -> List[str]:
    """Return sorted list of PDF paths in qa/samples/."""
    if not os.path.isdir(_SAMPLES_DIR):
        return []
    return sorted(
        os.path.join(_SAMPLES_DIR, f)
        for f in os.listdir(_SAMPLES_DIR)
        if f.lower().endswith(".pdf")
    )


def _find_output_csv(output_dir: str) -> Optional[str]:
    """Find final_output.csv in any subdirectory of output_dir."""
    for root, _, files in os.walk(output_dir):
        if "final_output.csv" in files:
            return os.path.join(root, "final_output.csv")
    return None


def _load_csv(csv_path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _load_answer(answer_path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(answer_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _compare_blocks(
    actual: List[Dict], expected_blocks: List[Dict], verbose: bool, logger
) -> List[Dict[str, Any]]:
    """Compare actual vs expected text blocks. Return list of mismatches."""
    mismatches = []

    if len(actual) != len(expected_blocks):
        mismatches.append({
            "type": "COUNT_MISMATCH",
            "actual_count": len(actual),
            "expected_count": len(expected_blocks),
        })
        logger.warning(f"  Block count: actual={len(actual)}, expected={len(expected_blocks)}")

    for i, (act, exp) in enumerate(zip(actual, expected_blocks)):
        act_text = str(act.get("text", "")).strip()
        exp_text = str(exp.get("text", "")).strip()
        if act_text != exp_text:
            mm = {
                "type": "TEXT_MISMATCH",
                "index": i,
                "actual": act_text,
                "expected": exp_text,
            }
            mismatches.append(mm)
            if verbose:
                logger.warning(f"  Mismatch [{i}]: expected={exp_text!r}, actual={act_text!r}")

    return mismatches


def _make_result(
    sample: str,
    verdict: str,
    reason_code: str,
    reason: str,
    mismatches: Optional[List] = None,
    skipped_count: int = 0,
) -> Dict[str, Any]:
    return {
        "sample": sample,
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
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
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())

"""
main.py – CLI entry point for the PDF text extraction pipeline.

Usage:
    # 단일 파일
    python main.py document.pdf

    # input/ 폴더 일괄 처리
    python main.py --batch
    python main.py --input-dir /path/to/pdfs

Examples:
    python main.py document.pdf --pages 1-5 --debug
    python main.py document.pdf --ocr-engine tesseract --confidence-threshold 0.6
    python main.py document.pdf --force-direction TOP_TO_BOTTOM
    python main.py --batch --output-dir results/
"""
from __future__ import annotations

import argparse
import os
import sys

from src.models.state import ProcessingStatus
from src.pipeline import (
    step0_init,
    step0_5_classify,
    step1_text_layer,
    step1_5_preprocess,
    step2_vision,
    step3_reconcile,
    step4_skip,
    step5_validate,
    step6_csv,
    step7_xlsx,
)
from src.utils.logger import get_logger, setup_logger

_DEFAULT_INPUT_DIR = "input"
_DEFAULT_OUTPUT_DIR = "output"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdftext",
        description="PDF 텍스트 추출 파이프라인 (디지털/스캔본/하이브리드 지원)",
    )

    # ── 입력 지정 (둘 중 하나) ──────────────────────────────────────────
    input_group = p.add_mutually_exclusive_group()
    input_group.add_argument(
        "pdf_path", nargs="?", default=None,
        help="처리할 PDF 파일 경로 (단일 파일 모드).",
    )
    input_group.add_argument(
        "--batch", action="store_true",
        help=f"input/ 폴더의 모든 PDF를 일괄 처리합니다 (기본 입력 폴더: ./{_DEFAULT_INPUT_DIR}/).",
    )
    p.add_argument(
        "--input-dir", default=_DEFAULT_INPUT_DIR,
        help=f"--batch 시 사용할 입력 폴더 (기본값: ./{_DEFAULT_INPUT_DIR}/).",
    )

    # ── 출력 ────────────────────────────────────────────────────────────
    p.add_argument(
        "--output-dir", default=_DEFAULT_OUTPUT_DIR,
        help=f"결과물 저장 폴더 (기본값: ./{_DEFAULT_OUTPUT_DIR}/).",
    )

    # ── 처리 옵션 ────────────────────────────────────────────────────────
    p.add_argument(
        "--pages",
        help='처리할 페이지 범위. 예: "1-5", "1,3,5", "2-4,7". 기본값: 전체.',
    )
    p.add_argument(
        "--ocr-engine", choices=["tesseract", "easyocr"], default="tesseract",
        help="OCR 엔진 선택 (기본값: tesseract).",
    )
    p.add_argument(
        "--force-direction", choices=["LEFT_TO_RIGHT", "TOP_TO_BOTTOM"],
        help="모든 페이지의 읽기 방향을 강제 지정.",
    )
    p.add_argument(
        "--confidence-threshold", type=float, default=0.5, metavar="0.0-1.0",
        help="이 값 미만 신뢰도의 텍스트를 SKIPPED 처리 (기본값: 0.5).",
    )
    p.add_argument(
        "--render-dpi", type=int, default=150,
        help="PDF 페이지 렌더링 DPI (기본값: 150).",
    )
    p.add_argument(
        "--preprocess", action="store_true", default=True,
        help="스캔 전처리 활성화 (기본값: on).",
    )
    p.add_argument(
        "--no-preprocess", dest="preprocess", action="store_false",
        help="스캔 전처리 비활성화.",
    )
    p.add_argument(
        "--ocr-priority", action="store_true", default=False,
        help="모든 페이지를 OCR 우선으로 처리 (스캔본으로 간주).",
    )
    p.add_argument(
        "--debug", action="store_true", default=False,
        help="디버그 정보 저장.",
    )
    return p


def run_pipeline(pdf_path: str, output_dir: str, options: dict) -> int:
    """
    단일 PDF에 대해 전체 파이프라인 실행.

    Returns:
        0 = EXPORT_COMPLETED, 1 = 실패
    """
    try:
        ctx = step0_init.run(pdf_path, options=options, output_base_dir=output_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    logger = get_logger()

    def _check(label: str) -> bool:
        if ctx.status == ProcessingStatus.FAILED:
            logger.error(f"Pipeline FAILED at {label}. Errors: {ctx.errors}")
            return False
        return True

    steps = [
        ("Step 0.5 Classify",    step0_5_classify.run),
        ("Step 1  Text layer",   step1_text_layer.run),
        ("Step 1.5 Preprocess",  step1_5_preprocess.run),
        ("Step 2  Vision/OCR",   step2_vision.run),
        ("Step 3  Reconcile",    step3_reconcile.run),
        ("Step 4  Skip",         step4_skip.run),
        ("Step 5  Validate",     step5_validate.run),
        ("Step 6  CSV",          step6_csv.run),
        ("Step 7  XLSX",         step7_xlsx.run),
    ]

    for label, step_fn in steps:
        step_fn(ctx)
        if not _check(label):
            return 1

    # ── 완료 리포트 ──────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"완료. Status: {ctx.status}")
    logger.info(f"작업 폴더: {ctx.work_dir}")
    logger.info(f"CSV : {ctx.options.get('_csv_path')}")
    logger.info(f"XLSX: {ctx.options.get('_xlsx_path')}")
    for w in ctx.warnings:
        logger.warning(f"  • {w}")
    if ctx.skipped_count:
        logger.warning(f"  스킵된 블록: {ctx.skipped_count}")
    logger.info("=" * 60)

    print(f"\n완료 → {ctx.work_dir}")
    print(f"  CSV : {ctx.options.get('_csv_path')}")
    print(f"  XLSX: {ctx.options.get('_xlsx_path')}")
    if ctx.warnings:
        for w in ctx.warnings:
            print(f"  ⚠  {w}")

    return 0 if ctx.status == ProcessingStatus.EXPORT_COMPLETED else 1


def run_batch(input_dir: str, output_dir: str, options: dict) -> int:
    """
    input_dir 안의 모든 PDF를 순서대로 처리.

    Returns:
        0 = 전체 성공, 1 = 하나 이상 실패
    """
    setup_logger("pdftext")  # 배치 모드 콘솔 로거
    logger = get_logger()

    if not os.path.isdir(input_dir):
        print(f"[ERROR] 입력 폴더를 찾을 수 없습니다: {input_dir}", file=sys.stderr)
        return 1

    pdfs = sorted(
        os.path.join(input_dir, f)
        for f in os.listdir(input_dir)
        if f.lower().endswith(".pdf")
    )

    if not pdfs:
        print(f"[ERROR] {input_dir} 폴더에 PDF 파일이 없습니다.", file=sys.stderr)
        return 1

    logger.info(f"배치 처리 시작: {len(pdfs)}개 파일")
    failed = []

    for pdf_path in pdfs:
        print(f"\n{'='*60}")
        print(f"처리 중: {os.path.basename(pdf_path)}")
        print(f"{'='*60}")
        rc = run_pipeline(pdf_path, output_dir, dict(options))
        if rc != 0:
            failed.append(os.path.basename(pdf_path))

    print(f"\n{'='*60}")
    print(f"배치 완료: {len(pdfs) - len(failed)}/{len(pdfs)} 성공")
    if failed:
        print(f"실패: {', '.join(failed)}")
    print(f"{'='*60}")

    return 0 if not failed else 1


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    options = {
        "page_range": args.pages,
        "ocr_engine": args.ocr_engine,
        "force_direction": args.force_direction,
        "confidence_threshold": args.confidence_threshold,
        "render_dpi": args.render_dpi,
        "preprocess": args.preprocess,
        "ocr_priority": args.ocr_priority,
        "debug": args.debug,
    }

    if args.batch:
        sys.exit(run_batch(args.input_dir, args.output_dir, options))
    elif args.pdf_path:
        sys.exit(run_pipeline(args.pdf_path, args.output_dir, options))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

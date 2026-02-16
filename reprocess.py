"""placeholder 마크다운을 삭제하고 Gemini API로 재생성

사용법:
    python reprocess.py           # placeholder 파일 전체 재처리
    python reprocess.py --dry-run # 실제 삭제/생성 없이 대상 목록만 출력
    python reprocess.py --limit N # N개만 처리 (테스트용)
"""

import argparse
import json
import os
import re

from config import JSON_PATH, MARKDOWN_DIR
from extractor import load_existing_papers
from markdown_gen import generate_markdown
from summarizer import is_thesis, summarize_paper


def find_placeholder_files() -> list[tuple[str, str | None]]:
    """placeholder 마크다운과 원본 PDF명을 반환.

    Returns:
        list of (md_filename, pdf_filename or None)
    """
    results = []
    for fname in sorted(os.listdir(MARKDOWN_DIR)):
        if not fname.endswith(".md"):
            continue
        fpath = MARKDOWN_DIR / fname
        try:
            content = fpath.read_text(encoding="utf-8")
        except Exception:
            continue

        if "[논문을 읽고 핵심 주장을 정리하세요]" not in content:
            continue

        pdf_match = re.search(r"\[\[(.+?\.pdf)\]\]", content, re.IGNORECASE)
        pdf_name = pdf_match.group(1) if pdf_match else None
        results.append((fname, pdf_name))

    return results


def main():
    parser = argparse.ArgumentParser(description="placeholder 마크다운 재처리")
    parser.add_argument("--dry-run", action="store_true", help="목록만 출력, 변경 없음")
    parser.add_argument("--limit", type=int, default=0, help="처리 건수 제한 (0=전체)")
    args = parser.parse_args()

    placeholders = find_placeholder_files()
    papers = load_existing_papers()
    paper_map = {p["file_name"]: p for p in papers}

    # 제외 항목 분류
    skipped_no_text = sum(
        1 for _, pdf in placeholders
        if not pdf or len(paper_map.get(pdf, {}).get("full_text", "")) <= 200
    )
    skipped_thesis = sum(
        1 for _, pdf in placeholders
        if pdf
        and len(paper_map.get(pdf, {}).get("full_text", "")) > 200
        and is_thesis(paper_map.get(pdf, {}))
    )
    targets = [
        (md, pdf) for md, pdf in placeholders
        if pdf
        and len(paper_map.get(pdf, {}).get("full_text", "")) > 200
        and not is_thesis(paper_map.get(pdf, {}))
    ]

    if args.limit:
        targets = targets[: args.limit]

    print("=" * 60)
    print(f"placeholder 재처리 {'(dry-run)' if args.dry_run else ''}")
    print("=" * 60)
    print(f"전체 placeholder: {len(placeholders)}개")
    print(f"텍스트 없어 제외: {skipped_no_text}개")
    print(f"학위논문 제외:    {skipped_thesis}개")
    print(f"처리 대상:        {len(targets)}개")
    if args.limit:
        print(f"(--limit {args.limit} 적용)")
    print()

    if args.dry_run:
        for md, pdf in targets:
            print(f"  {md[:70]}")
        return

    success = skipped = errors = 0

    for i, (md_fname, pdf_fname) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {pdf_fname[:65] if pdf_fname else '?'}")

        md_path = MARKDOWN_DIR / md_fname

        # 1. 기존 파일 내용 백업 (실패 시 복원용)
        backup_content = md_path.read_text(encoding="utf-8")

        # 2. 기존 placeholder 삭제 (generate_markdown이 동일 파일명 생성 가능하도록)
        try:
            md_path.unlink()
        except Exception as e:
            print(f"  [오류] 삭제 실패: {e}")
            errors += 1
            continue

        # 3. AI 분석 — 실패 시 백업 복원
        paper = paper_map[pdf_fname]
        print("  AI 분석 중...")
        try:
            summary = summarize_paper(paper)
            result = generate_markdown(summary, pdf_fname)
            if result:
                success += 1
            else:
                # generate_markdown이 스킵한 경우 (다른 유사 파일 존재)
                skipped += 1
        except Exception as e:
            print(f"  [오류] {e}")
            errors += 1
            # 예외 발생 시 백업으로 복원
            md_path.write_text(backup_content, encoding="utf-8")
            print(f"  [복구] 기존 파일 복원: {md_fname}")

    print(f"\n{'='*60}")
    print(f"완료: 성공 {success}개 / 스킵 {skipped}개 / 오류 {errors}개")


if __name__ == "__main__":
    main()

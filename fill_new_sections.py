"""기존 마크다운 노트의 '연구 배경' / '분석결과' 섹션을 채운다.

기본 동작:
  - 빈 섹션 또는 placeholder 섹션만 대상으로 함
  - extracted_papers.json과 안전하게 매칭되는 노트만 처리
  - Zotero 키가 있는 노트는 Zotero 스토리지 PDF 추출로 보완
  - Gemini가 실제 내용을 생성한 경우에만 해당 섹션을 치환

사용법:
    python fill_new_sections.py
    python fill_new_sections.py --dry-run
    python fill_new_sections.py --limit 5
    python fill_new_sections.py --overwrite
    python fill_new_sections.py --no-zotero   # Zotero 스토리지 탐색 비활성화
"""

from __future__ import annotations

import argparse
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import MARKDOWN_DIR, ZOTERO_LIBRARY_ID, ZOTERO_API_KEY, ZOTERO_STORAGE
from extractor import load_existing_papers, extract_one
from summarizer import summarize_paper

logger = logging.getLogger(__name__)

RESEARCH_BG_HEADING = "## 연구 배경 (Research Background)"
METHOD_HEADING = "## 연구 방법 (Method)"
ANALYSIS_RESULT_HEADING = "## 분석결과 (Analysis Result)"
FINDINGS_HEADING = "## 주요 발견 (Findings)"

BG_PLACEHOLDER = "[연구 배경을 정리하세요]"
AR_PLACEHOLDER = "[분석 결과를 정리하세요]"


def normalize_text(text: str) -> str:
    """파일명/제목 비교용 정규화."""
    if not text:
        return ""

    text = text.strip()
    text = re.sub(r"\.(pdf|md)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\[편집충돌_[^\]]+\]", "", text)
    text = re.sub(
        r"^(?:.+?(?:\s+등|\s+및\s+.+?)?)\s+-\s+(?:19|20)\d{2}\s+-\s+",
        "",
        text,
    )
    text = text.replace("_", " ")
    text = re.sub(r"[^\w\s가-힣]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def collapse_candidates(candidates: list[dict]) -> list[dict]:
    """편집충돌 복제본처럼 사실상 동일한 PDF 후보를 하나로 축약."""
    grouped: dict[str, dict] = {}
    for paper in candidates:
        key = normalize_text(paper.get("file_name", ""))
        current = grouped.get(key)
        if current is None or len(paper.get("full_text", "")) > len(current.get("full_text", "")):
            grouped[key] = paper
    return list(grouped.values())


def section_body(text: str, heading: str, next_heading: str) -> str | None:
    pattern = re.compile(
        rf"{re.escape(heading)}\n(.*?)\n{re.escape(next_heading)}",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1)


def section_needs_fill(body: str | None, placeholder: str) -> bool:
    if body is None:
        return False
    body = body.strip()
    return not body or placeholder in body


def replace_section(text: str, heading: str, next_heading: str, new_body: str) -> str:
    pattern = re.compile(
        rf"({re.escape(heading)}\n)(.*?)(\n{re.escape(next_heading)})",
        re.DOTALL,
    )
    return pattern.sub(
        lambda m: f"{m.group(1)}{new_body.strip()}\n\n{next_heading}",
        text,
        count=1,
    )


def has_meaningful_content(value: str, placeholder: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    if placeholder in value:
        return False
    if value in {"-", "1.", "1.\n2.\n3."}:
        return False
    return True


def parse_markdown_context(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")

    def extract(pattern: str) -> str:
        match = re.search(pattern, text, re.MULTILINE)
        return match.group(1).strip() if match else ""

    fm_title = extract(r'^title:\s*"(.*)"\s*$')
    heading_title = extract(r"^#\s+(.+)$")
    source_pdf = extract(r"\*\*원본 파일\*\*:\s*\[\[(.+?\.pdf)\]\]")
    zotero_key = extract(r"^zotero_key:\s*(.*)$")

    md_title = md_path.stem
    md_title = re.sub(r"^@(?:\d{4}|)_", "", md_title)
    md_title = md_title.replace("-", " ")

    return {
        "path": md_path,
        "text": text,
        "title": fm_title or heading_title,
        "source_pdf": source_pdf,
        "zotero_key": zotero_key,
        "fallback_title": md_title,
        "biblio": {
            "title": fm_title or heading_title,
            "author": extract(r"^- \*\*저자\*\*:\s*(.*)$"),
            "year": extract(r"^- \*\*연도\*\*:\s*(.*)$"),
            "journal": extract(r"^- \*\*저널/출처\*\*:\s*(.*)$"),
            "publisher": extract(r"^- \*\*출판사\*\*:\s*(.*)$"),
            "volume": extract(r"^- \*\*권\(Vol\)\*\*:\s*(.*)$"),
            "issue": extract(r"^- \*\*호\(Issue\)\*\*:\s*(.*)$"),
            "pages": extract(r"^- \*\*페이지\*\*:\s*(.*)$"),
            "doi": extract(r"^- \*\*DOI\*\*:\s*(.*)$"),
            "issn": extract(r"^- \*\*ISSN\*\*:\s*(.*)$"),
            "url": extract(r"^- \*\*URL\*\*:\s*(.*)$"),
            "language": extract(r"^- \*\*언어\*\*:\s*(.*)$"),
        },
    }


def build_indexes(papers: list[dict]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    exact_map = {paper["file_name"]: paper for paper in papers}
    normalized_map: dict[str, list[dict]] = {}

    for paper in papers:
        file_name = paper.get("file_name", "")
        keys = {
            normalize_text(file_name),
            normalize_text(Path(file_name).stem),
        }
        for key in keys:
            if key:
                normalized_map.setdefault(key, []).append(paper)

    return exact_map, normalized_map


def resolve_paper(note: dict, papers: list[dict], exact_map: dict[str, dict], normalized_map: dict[str, list[dict]]) -> tuple[dict | None, str]:
    source_pdf = note.get("source_pdf", "")
    title = note.get("title", "")
    fallback_title = note.get("fallback_title", "")

    if source_pdf and source_pdf in exact_map:
        return exact_map[source_pdf], "exact_source"

    for candidate_text, label in (
        (source_pdf, "normalized_source"),
        (title, "title_exact"),
        (fallback_title, "filename_exact"),
    ):
        key = normalize_text(candidate_text)
        if not key:
            continue
        candidates = collapse_candidates(normalized_map.get(key, []))
        if len(candidates) == 1:
            return candidates[0], label
        if len(candidates) > 1:
            return None, "ambiguous"

    base_key = normalize_text(title or source_pdf or fallback_title)
    if not base_key:
        return None, "unmatched"

    candidates = []
    for paper in papers:
        norm_name = normalize_text(paper.get("file_name", ""))
        if base_key in norm_name or norm_name in base_key:
            candidates.append(paper)

    candidates = collapse_candidates(candidates)
    if len(candidates) == 1:
        return candidates[0], "contains_match"
    if len(candidates) > 1:
        return None, "ambiguous"
    return None, "unmatched"


def resolve_paper_from_zotero(note: dict, sync) -> dict | None:
    """Zotero 키가 있는 노트에 대해 Zotero 스토리지 PDF를 찾아 paper dict 반환."""
    zotero_key = note.get("zotero_key", "")
    if not zotero_key:
        return None
    # doi:, url: 등 유효하지 않은 키 무시 (Zotero 키는 8자 대문자+숫자)
    if not re.match(r'^[A-Z0-9]{8}$', zotero_key):
        return None
    try:
        pdf_path = sync.get_pdf_path(zotero_key)
    except Exception as e:
        logger.debug(f"Zotero PDF 경로 탐색 실패 ({zotero_key}): {e}")
        return None
    if pdf_path is None:
        return None
    if not pdf_path.exists():
        logger.debug(f"Zotero 스토리지 PDF 없음: {pdf_path}")
        return None
    try:
        paper = extract_one(pdf_path)
        if len(paper.get("full_text", "").strip()) > 200:
            return paper
        logger.debug(f"PDF 텍스트 부족 ({pdf_path.name}): {len(paper.get('full_text','').strip())}자")
    except Exception as e:
        logger.debug(f"PDF 텍스트 추출 실패 ({pdf_path.name}): {e}")
    return None


def collect_targets(papers: list[dict], overwrite: bool, sync=None) -> tuple[list[dict], dict[str, int], list[tuple[str, str]]]:
    exact_map, normalized_map = build_indexes(papers)

    stats = {
        "notes": 0,
        "already_filled": 0,
        "needs_fill": 0,
        "matched": 0,
        "zotero_storage": 0,
        "unmatched": 0,
        "ambiguous": 0,
        "short_text": 0,
    }
    skipped_examples: list[tuple[str, str]] = []
    targets: list[dict] = []

    for md_path in sorted(MARKDOWN_DIR.glob("@*.md")):
        note = parse_markdown_context(md_path)
        text = note["text"]
        stats["notes"] += 1

        bg_body = section_body(text, RESEARCH_BG_HEADING, METHOD_HEADING)
        ar_body = section_body(text, ANALYSIS_RESULT_HEADING, FINDINGS_HEADING)
        needs_bg = overwrite or section_needs_fill(bg_body, BG_PLACEHOLDER)
        needs_ar = overwrite or section_needs_fill(ar_body, AR_PLACEHOLDER)

        if not (needs_bg or needs_ar):
            stats["already_filled"] += 1
            continue

        stats["needs_fill"] += 1
        paper, match_mode = resolve_paper(note, papers, exact_map, normalized_map)

        # extracted_papers.json 매칭 실패 or 본문 부족 → Zotero 스토리지 보완
        if paper is None or len(paper.get("full_text", "").strip()) <= 200:
            if sync is not None:
                zotero_paper = resolve_paper_from_zotero(note, sync)
                if zotero_paper is not None:
                    paper = zotero_paper
                    match_mode = "zotero_storage"

        if paper is None:
            stats[match_mode if match_mode in stats else "unmatched"] += 1
            if len(skipped_examples) < 20:
                skipped_examples.append((md_path.name, match_mode))
            continue

        if len(paper.get("full_text", "").strip()) <= 200:
            stats["short_text"] += 1
            if len(skipped_examples) < 20:
                skipped_examples.append((md_path.name, "short_text"))
            continue

        note["paper"] = paper
        note["match_mode"] = match_mode
        note["needs_bg"] = needs_bg
        note["needs_ar"] = needs_ar
        targets.append(note)
        if match_mode == "zotero_storage":
            stats["zotero_storage"] += 1
        else:
            stats["matched"] += 1

    return targets, stats, skipped_examples


def update_note(text: str, summary: dict, needs_bg: bool, needs_ar: bool) -> tuple[str, bool]:
    changed = False

    if needs_bg:
        bg_content = str(summary.get("research_background", "")).strip()
        if has_meaningful_content(bg_content, BG_PLACEHOLDER):
            text = replace_section(text, RESEARCH_BG_HEADING, METHOD_HEADING, bg_content)
            changed = True

    if needs_ar:
        ar_content = str(summary.get("analysis_result", "")).strip()
        if has_meaningful_content(ar_content, AR_PLACEHOLDER):
            text = replace_section(text, ANALYSIS_RESULT_HEADING, FINDINGS_HEADING, ar_content)
            changed = True

    return text, changed


def main():
    parser = argparse.ArgumentParser(description="기존 노트의 연구 배경/분석결과 채우기")
    parser.add_argument("--dry-run", action="store_true", help="실제 수정 없이 대상만 집계")
    parser.add_argument("--limit", type=int, default=0, help="처리 건수 제한 (0=전체)")
    parser.add_argument("--overwrite", action="store_true", help="이미 채워진 섹션도 강제로 재생성")
    parser.add_argument("--no-zotero", action="store_true", help="Zotero 스토리지 탐색 비활성화")
    parser.add_argument("--workers", type=int, default=3, help="병렬 처리 worker 수 (기본 3)")
    args = parser.parse_args()

    # Zotero 연결 초기화 (--no-zotero 미사용 시)
    sync = None
    if not args.no_zotero:
        try:
            from zotero_sync import ZoteroSync
            sync = ZoteroSync(ZOTERO_LIBRARY_ID, ZOTERO_API_KEY, ZOTERO_STORAGE)
            print("Zotero 스토리지 연동 활성화")
        except Exception as e:
            print(f"[경고] Zotero 연결 실패, 스토리지 탐색 비활성화: {e}")

    papers = load_existing_papers()
    targets, stats, skipped_examples = collect_targets(papers, overwrite=args.overwrite, sync=sync)

    if args.limit:
        targets = targets[: args.limit]

    print("=" * 60)
    print(f"연구 배경/분석결과 채우기 {'(dry-run)' if args.dry_run else ''}")
    print("=" * 60)
    print(f"전체 노트:          {stats['notes']}개")
    print(f"이미 채워진 노트:   {stats['already_filled']}개")
    print(f"채움 필요 노트:     {stats['needs_fill']}개")
    print(f"JSON 매칭 성공:     {stats['matched']}개")
    print(f"Zotero 스토리지:    {stats['zotero_storage']}개")
    print(f"매칭 실패:          {stats['unmatched']}개")
    print(f"모호한 매칭:        {stats['ambiguous']}개")
    print(f"본문 부족 제외:     {stats['short_text']}개")
    if args.limit:
        print(f"실제 처리 제한:     {len(targets)}개 (--limit {args.limit})")
    print()

    if skipped_examples:
        print("[참고] 스킵 예시")
        for name, reason in skipped_examples[:10]:
            print(f"  - {name}: {reason}")
        print()

    if args.dry_run:
        for note in targets[:20]:
            print(
                f"  - {note['path'].name} <- {note['paper']['file_name']} "
                f"({note['match_mode']})"
            )
        if len(targets) > 20:
            print(f"  ... 외 {len(targets) - 20}개")
        return

    summary_cache: dict[str, dict | None] = {}
    cache_lock = threading.Lock()
    print_lock = threading.Lock()
    counters_lock = threading.Lock()
    updated = 0
    skipped_generation = 0
    errors = 0

    def process_one(idx_note: tuple[int, dict]) -> None:
        nonlocal updated, skipped_generation, errors
        idx, note = idx_note
        md_path = note["path"]
        paper = note["paper"]

        if note.get("match_mode") == "zotero_storage" and note.get("zotero_key"):
            cache_key = f"{note['zotero_key']}::{paper['file_name']}"
        else:
            cache_key = paper["file_name"]

        with print_lock:
            print(f"[{idx}/{len(targets)}] {md_path.name}")

        # 캐시 확인 (lock 보호)
        with cache_lock:
            in_cache = cache_key in summary_cache
            summary = summary_cache.get(cache_key)

        if not in_cache:
            with print_lock:
                print(f"  AI 분석 중... ({paper['file_name']})")
            try:
                summary = summarize_paper(paper, biblio=note["biblio"])
            except Exception as exc:
                with print_lock:
                    print(f"  [오류] 요약 실패: {exc}")
                with cache_lock:
                    summary_cache[cache_key] = None
                with counters_lock:
                    errors += 1
                return
            with cache_lock:
                summary_cache[cache_key] = summary

        if not summary:
            with print_lock:
                print("  [스킵] 사용 가능한 요약이 없음")
            with counters_lock:
                skipped_generation += 1
            return

        try:
            original_text = md_path.read_text(encoding="utf-8")
            new_text, changed = update_note(
                original_text,
                summary,
                needs_bg=note["needs_bg"],
                needs_ar=note["needs_ar"],
            )
            if not changed:
                with print_lock:
                    print("  [스킵] 생성 결과가 placeholder 또는 공란")
                with counters_lock:
                    skipped_generation += 1
                return

            md_path.write_text(new_text, encoding="utf-8")
            with counters_lock:
                updated += 1
            with print_lock:
                print("  [완료] 섹션 업데이트")
        except Exception as exc:
            with print_lock:
                print(f"  [오류] 파일 갱신 실패: {exc}")
            with counters_lock:
                errors += 1

    workers = max(1, args.workers)
    with print_lock:
        print(f"병렬 처리: {workers} workers")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        executor.map(process_one, enumerate(targets, 1))

    print()
    print("=" * 60)
    print(f"완료: 업데이트 {updated}개 / 생성 스킵 {skipped_generation}개 / 오류 {errors}개")


if __name__ == "__main__":
    main()

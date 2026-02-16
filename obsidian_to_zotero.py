"""Obsidian 02-Literature 노트 → Zotero 일괄 임포트

기능:
  1. 마크다운 frontmatter + 서지정보 섹션 파싱 (title/year/author/journal/tags)
  2. Zotero API로 아이템 생성 (최대 50개씩 배치)
  3. 생성된 zotero_key를 마크다운 frontmatter에 역기록
  4. 이미 zotero_key 있는 파일은 건너뜀

사용법:
  python3 obsidian_to_zotero.py             # 전체 임포트
  python3 obsidian_to_zotero.py --dry-run   # 실제 전송 없이 파싱만 확인
  python3 obsidian_to_zotero.py --limit 10  # 처음 10개만 테스트
"""

import argparse
import re
import sys
import time
from pathlib import Path

try:
    from pyzotero import zotero
except ImportError:
    print("[오류] pip install pyzotero")
    sys.exit(1)

from config import (
    MARKDOWN_DIR,
    ZOTERO_API_KEY,
    ZOTERO_LIBRARY_ID,
)

BATCH_SIZE = 50       # Zotero API 배치 최대치
BATCH_DELAY = 2       # 배치 간 딜레이(초)


# ── 마크다운 파싱 ──────────────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> dict:
    """YAML frontmatter 파싱 → {title, year, tags, zotero_key}"""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    fm = m.group(1)

    def get(key):
        r = re.search(rf'^{key}:\s*(.+)$', fm, re.MULTILINE)
        return r.group(1).strip() if r else ""

    title = get("title").strip('"')
    year_raw = get("year")
    year = year_raw if re.match(r"^\d{4}$", year_raw) else ""

    tags_m = re.search(r'^tags:\s*\[(.+?)\]', fm, re.MULTILINE)
    tags = []
    if tags_m:
        tags = [t.strip().strip('"') for t in tags_m.group(1).split(",") if t.strip()]
        tags = [t for t in tags if t not in ("literature", "paper")]

    zotero_key = get("zotero_key")

    return {"title": title, "year": year, "tags": tags, "zotero_key": zotero_key}


def _parse_field(text: str, label: str) -> str:
    """서지정보 섹션에서 특정 필드 값 추출."""
    m = re.search(rf'\*\*{re.escape(label)}\*\*:[ \t]*(.+)', text)
    if m:
        val = m.group(1).strip().rstrip("\r")
        if val and not val.startswith(">") and val != "-":
            return val
    return ""


def parse_body(text: str) -> dict:
    """본문 서지정보 섹션 파싱 → {author, journal, doi, volume, issue, pages, issn, url, language, publisher}"""
    return {
        "author":    _parse_field(text, "저자"),
        "journal":   _parse_field(text, "저널/출처"),
        "publisher": _parse_field(text, "출판사"),
        "volume":    _parse_field(text, "권(Vol)"),
        "issue":     _parse_field(text, "호(Issue)"),
        "pages":     _parse_field(text, "페이지"),
        "doi":       _parse_field(text, "DOI"),
        "issn":      _parse_field(text, "ISSN"),
        "url":       _parse_field(text, "URL"),
        "language":  _parse_field(text, "언어"),
    }


def parse_note_file(md_path: Path) -> dict | None:
    """마크다운 파일 전체 파싱. zotero_key 이미 있으면 None 반환."""
    text = md_path.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    if not fm:
        return None
    if fm.get("zotero_key"):  # 이미 연동됨
        return None

    body = parse_body(text)

    # frontmatter에서 doi 추출 (있는 경우)
    doi_fm = re.search(r'^doi:\s*(.+)$', text[:500], re.MULTILINE)
    doi = doi_fm.group(1).strip() if doi_fm else ""
    if not doi:
        doi = body.get("doi", "")

    return {
        "path": md_path,
        "title": fm["title"],
        "year": fm["year"],
        "tags": fm["tags"],
        "author": body["author"],
        "journal": body["journal"],
        "doi": doi,
        "volume": body.get("volume", ""),
        "issue": body.get("issue", ""),
        "pages": body.get("pages", ""),
        "issn": body.get("issn", ""),
        "url": body.get("url", ""),
        "language": body.get("language", ""),
        "publisher": body.get("publisher", ""),
    }


# ── Zotero 아이템 빌드 ─────────────────────────────────────────────────────────

def build_zotero_item(info: dict) -> dict:
    """파싱 결과 → Zotero API 아이템 dict 생성."""
    creators = []
    if info["author"]:
        author_str = info["author"]
        # 세미콜론 구분: "성, 이름; 성, 이름" or "이름 성; 이름 성"
        if ";" in author_str:
            segments = [s.strip().rstrip(",") for s in author_str.split(";") if s.strip()]
        else:
            # 쉼표 구분: "이름 성, 이름 성" (대문자/한글 앞 쉼표로 저자 구분)
            segments = re.split(r",\s*(?=[A-Z가-힣])", author_str)
        for raw in segments:
            raw = raw.strip().rstrip(",;")
            if not raw:
                continue
            # 세미콜론 방식에서 "성, 이름" 형식인지 확인
            # (쉼표가 있고 세미콜론이 원본에 있었던 경우)
            if ";" in author_str and "," in raw:
                parts = raw.split(",", 1)
                creators.append({
                    "creatorType": "author",
                    "lastName": parts[0].strip(),
                    "firstName": parts[1].strip(),
                })
            else:
                creators.append({
                    "creatorType": "author",
                    "name": raw,
                })

    tags = [{"tag": t} for t in info["tags"] if t]

    item: dict = {
        "itemType": "journalArticle",
        "title": info["title"],
        "creators": creators,
        "date": info["year"],
        "publicationTitle": info["journal"],
        "tags": tags,
    }
    # 확장 서지정보 필드 (값이 있는 경우만 추가)
    for md_key, zotero_key in (
        ("doi",       "DOI"),
        ("volume",    "volume"),
        ("issue",     "issue"),
        ("pages",     "pages"),
        ("issn",      "ISSN"),
        ("url",       "url"),
        ("language",  "language"),
        ("publisher", "publisher"),
    ):
        val = info.get(md_key, "")
        if val:
            item[zotero_key] = val
    return item


# ── frontmatter에 zotero_key 기록 ─────────────────────────────────────────────

def write_zotero_key(md_path: Path, key: str):
    """마크다운 frontmatter에 zotero_key 줄 추가."""
    text = md_path.read_text(encoding="utf-8")

    if "zotero_key:" in text:
        # 이미 있으면 값만 교체
        text = re.sub(r'^zotero_key:.*$', f'zotero_key: {key}', text, flags=re.MULTILINE)
    else:
        # created: 줄 뒤에 삽입
        text = re.sub(
            r'^(created:.*)',
            rf'\1\nzotero_key: {key}',
            text,
            count=1,
            flags=re.MULTILINE,
        )

    md_path.write_text(text, encoding="utf-8")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Obsidian 노트 → Zotero 임포트")
    parser.add_argument("--dry-run", action="store_true", help="파싱만, API 전송 없음")
    parser.add_argument("--limit", type=int, default=0, help="처리 개수 제한 (0=전체)")
    args = parser.parse_args()

    if not ZOTERO_LIBRARY_ID or not ZOTERO_API_KEY:
        print("[오류] config.py에 ZOTERO_LIBRARY_ID / ZOTERO_API_KEY 설정 필요")
        sys.exit(1)

    # 마크다운 파일 파싱
    md_files = sorted(MARKDOWN_DIR.glob("*.md"))
    print(f"마크다운 파일 총 {len(md_files)}개 스캔 중...")

    items_to_create = []
    skipped_linked = 0

    for md_path in md_files:
        info = parse_note_file(md_path)
        if info is None:
            skipped_linked += 1
            continue
        if not info["title"]:
            continue
        items_to_create.append(info)

    print(f"  → 이미 연동됨: {skipped_linked}개 (건너뜀)")
    print(f"  → 신규 임포트 대상: {len(items_to_create)}개")

    if args.limit:
        items_to_create = items_to_create[:args.limit]
        print(f"  → --limit {args.limit} 적용: {len(items_to_create)}개만 처리")

    if args.dry_run:
        print("\n[dry-run] 실제 전송 없음. 샘플 파싱 결과:")
        for info in items_to_create[:5]:
            print(f"  {info['path'].name[:50]}")
            print(f"    제목: {info['title'][:50]}")
            print(f"    저자: {info['author'][:40] or '(없음)'}")
            print(f"    연도: {info['year'] or '(없음)'}")
            print(f"    저널: {info['journal'][:40] or '(없음)'}")
            print(f"    DOI:  {info.get('doi') or '(없음)'}")
            print(f"    권/호: {info.get('volume') or '-'}/{info.get('issue') or '-'}")
            print(f"    태그: {info['tags'][:3]}")
        return

    # Zotero 연결
    zot = zotero.Zotero(ZOTERO_LIBRARY_ID, "user", ZOTERO_API_KEY)

    # 배치 처리
    total = len(items_to_create)
    created = 0
    errors = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch = items_to_create[batch_start: batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        batch_total = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n[배치 {batch_num}/{batch_total}] {len(batch)}개 전송 중...")

        zotero_items = [build_zotero_item(info) for info in batch]

        try:
            result = zot.create_items(zotero_items)
        except Exception as e:
            print(f"  [오류] 배치 전송 실패: {e}")
            errors += len(batch)
            continue

        # 결과 처리: result는 {'success': {idx: key}, 'unchanged': {}, 'failed': {}}
        success = result.get("success", {})
        failed = result.get("failed", {})

        for idx_str, key in success.items():
            idx = int(idx_str)
            info = batch[idx]
            write_zotero_key(info["path"], key)
            print(f"  ✓ [{key}] {info['title'][:50]}")
            created += 1

        for idx_str, err in failed.items():
            idx = int(idx_str)
            info = batch[idx]
            print(f"  ✗ 실패: {info['title'][:40]} — {err}")
            errors += 1

        if batch_start + BATCH_SIZE < total:
            time.sleep(BATCH_DELAY)

    print(f"\n완료! 생성: {created}개 / 실패: {errors}개 / 전체: {total}개")
    if created:
        print(f"마크다운 파일에 zotero_key가 기록되었습니다.")


if __name__ == "__main__":
    main()

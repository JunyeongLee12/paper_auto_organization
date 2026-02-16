"""CrossRef API로 Zotero 빈 서지정보 필드 자동 채우기

워크플로우:
  1. Zotero 전체 아이템 조회
  2. DOI 있는 아이템 → CrossRef /works/{doi} 직접 조회
  3. DOI 없는 아이템 → 제목+저자로 CrossRef 검색
  4. CrossRef 응답에서 volume/issue/pages/ISSN/publisher/URL/language 추출
  5. Zotero API update_item()으로 빈 필드만 업데이트 (기존 값 덮어쓰지 않음)
  6. 연동된 Obsidian 마크다운 서지정보 섹션도 업데이트

사용법:
  python3 crossref_enrich.py             # 전체 처리
  python3 crossref_enrich.py --dry-run   # 실제 변경 없이 결과만 출력
  python3 crossref_enrich.py --limit 10  # 처음 10개만 처리
"""

import argparse
import re
import sys
import time
from pathlib import Path

import requests

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

# CrossRef Etiquette: mailto 헤더 포함
CROSSREF_MAILTO = "paper-automation@example.com"
CROSSREF_HEADERS = {
    "User-Agent": f"PaperOrganizer/1.0 (mailto:{CROSSREF_MAILTO})",
}
CROSSREF_DELAY = 1.0   # 초당 1회 제한
CROSSREF_TIMEOUT = 15  # seconds


# ── CrossRef API 호출 ──────────────────────────────────────────────────────────

def _fetch_crossref_by_doi(doi: str) -> dict | None:
    """DOI로 CrossRef 직접 조회."""
    url = f"https://api.crossref.org/works/{doi}"
    try:
        resp = requests.get(url, headers=CROSSREF_HEADERS, timeout=CROSSREF_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("message", {})
    except requests.RequestException as e:
        print(f"  [CrossRef] DOI 조회 오류 ({doi}): {e}")
        return None


def _fetch_crossref_by_query(title: str, author: str) -> dict | None:
    """제목+저자로 CrossRef 검색. 첫 번째 결과 반환."""
    params: dict = {"rows": 1}
    if title:
        params["query.title"] = title
    if author:
        # 첫 번째 저자 성만 사용
        first_author = re.split(r"[;,]", author)[0].strip()
        params["query.author"] = first_author

    url = "https://api.crossref.org/works"
    try:
        resp = requests.get(url, params=params, headers=CROSSREF_HEADERS, timeout=CROSSREF_TIMEOUT)
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
        if not items:
            return None
        # 제목 유사도 기본 체크 (첫 단어 일치 확인)
        result = items[0]
        cr_titles = result.get("title", [])
        if cr_titles and title:
            cr_title = cr_titles[0].lower()
            query_words = title.lower().split()[:3]
            if not any(w in cr_title for w in query_words):
                return None
        return result
    except requests.RequestException as e:
        print(f"  [CrossRef] 검색 오류 ({title[:40]}): {e}")
        return None


# ── CrossRef 응답 → 필드 추출 ─────────────────────────────────────────────────

def _extract_fields(cr: dict) -> dict:
    """CrossRef 응답에서 서지정보 필드 추출."""
    result: dict = {}

    # volume / issue / page
    if cr.get("volume"):
        result["volume"] = str(cr["volume"])
    if cr.get("issue"):
        result["issue"] = str(cr["issue"])
    if cr.get("page"):
        result["pages"] = str(cr["page"])

    # ISSN (print 우선, 없으면 electronic)
    issn_list = cr.get("ISSN", [])
    if issn_list:
        result["ISSN"] = issn_list[0]

    # publisher
    if cr.get("publisher"):
        result["publisher"] = cr["publisher"]

    # URL
    if cr.get("URL"):
        result["url"] = cr["URL"]
    elif cr.get("resource", {}).get("primary", {}).get("URL"):
        result["url"] = cr["resource"]["primary"]["URL"]

    # language
    if cr.get("language"):
        result["language"] = cr["language"]

    # DOI (검색으로 찾은 경우 DOI도 추출)
    if cr.get("DOI"):
        result["DOI"] = cr["DOI"]

    return result


# ── Zotero 아이템 업데이트 ─────────────────────────────────────────────────────

def _update_zotero_item(zot, item: dict, new_fields: dict) -> bool:
    """빈 필드만 Zotero에 업데이트. True=변경, False=변경없음."""
    data = item.get("data", {})
    updates: dict = {}

    for zotero_field, new_val in new_fields.items():
        if not new_val:
            continue
        current = data.get(zotero_field, "")
        if not current:   # 빈 필드만 채움
            updates[zotero_field] = new_val

    if not updates:
        return False

    try:
        updated_item = dict(data)
        updated_item.update(updates)
        zot.update_item({"key": item["key"], "version": data.get("version", 0), "data": updated_item})
        return True
    except Exception as e:
        print(f"  [오류] Zotero 업데이트 실패 ({item['key']}): {e}")
        return False


# ── Obsidian 마크다운 서지정보 업데이트 ───────────────────────────────────────

def _find_markdown_by_key(zotero_key: str) -> Path | None:
    """zotero_key로 마크다운 파일 탐색."""
    for md_path in MARKDOWN_DIR.glob("@*.md"):
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            continue
        if f"zotero_key: {zotero_key}" in text:
            return md_path
    return None


def _update_markdown(md_path: Path, new_fields: dict, zotero_to_md: dict) -> bool:
    """마크다운 서지정보 섹션의 빈 필드만 업데이트."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return False

    changed = False

    for zotero_field, new_val in new_fields.items():
        if not new_val:
            continue
        md_label = zotero_to_md.get(zotero_field)
        if not md_label:
            continue

        # 빈 줄만 업데이트 (예: "- **DOI**:" 뒤에 값 없는 줄)
        escaped = re.escape(md_label)
        pattern = rf'(\*\*{escaped}\*\*:[ \t]*)$'
        new_text, n = re.subn(pattern, rf'\g<1>{new_val}', text, flags=re.MULTILINE)
        if n:
            text = new_text
            changed = True

    # frontmatter doi 업데이트
    doi_val = new_fields.get("DOI", "")
    if doi_val:
        new_text, n = re.subn(
            r'^(doi:[ \t]*)$', rf'\g<1>{doi_val}', text, flags=re.MULTILINE
        )
        if n:
            text = new_text
            changed = True

    if changed:
        md_path.write_text(text, encoding="utf-8")

    return changed


# ── 메인 ──────────────────────────────────────────────────────────────────────

# Zotero 필드 → 마크다운 라벨 매핑
ZOTERO_TO_MD = {
    "volume":    "권(Vol)",
    "issue":     "호(Issue)",
    "pages":     "페이지",
    "ISSN":      "ISSN",
    "publisher": "출판사",
    "url":       "URL",
    "language":  "언어",
    "DOI":       "DOI",
}


def main():
    parser = argparse.ArgumentParser(description="CrossRef API로 빈 서지정보 채우기")
    parser.add_argument("--dry-run", action="store_true", help="실제 변경 없이 결과만 출력")
    parser.add_argument("--limit", type=int, default=0, help="처리 아이템 수 제한 (0=전체)")
    args = parser.parse_args()

    if not ZOTERO_LIBRARY_ID or not ZOTERO_API_KEY:
        print("[오류] config.py에 ZOTERO_LIBRARY_ID / ZOTERO_API_KEY 설정 필요")
        sys.exit(1)

    zot = zotero.Zotero(ZOTERO_LIBRARY_ID, "user", ZOTERO_API_KEY)

    print("=" * 60)
    print(f"CrossRef 서지정보 보강 {'(dry-run)' if args.dry_run else ''}")
    print("=" * 60)

    # 전체 아이템 조회
    print("Zotero 아이템 조회 중...")
    try:
        all_items = zot.everything(zot.items(itemType="-attachment || note"))
    except Exception as e:
        print(f"[오류] Zotero API 호출 실패: {e}")
        sys.exit(1)

    print(f"  → 총 {len(all_items)}개 아이템")

    # 빈 필드가 있는 아이템만 대상
    targets = []
    for item in all_items:
        data = item.get("data", {})
        item_type = data.get("itemType", "")
        if item_type in ("attachment", "note"):
            continue
        # 최소 1개 이상 빈 서지정보 필드가 있는 아이템
        empty_fields = [
            f for f in ("DOI", "volume", "issue", "pages", "ISSN", "publisher", "url", "language")
            if not data.get(f)
        ]
        if empty_fields:
            targets.append(item)

    print(f"  → 빈 필드 있는 아이템: {len(targets)}개")

    if args.limit:
        targets = targets[:args.limit]
        print(f"  → --limit {args.limit} 적용: {len(targets)}개만 처리")

    print()

    zotero_updated = 0
    md_updated = 0
    not_found = 0

    for i, item in enumerate(targets, 1):
        data = item.get("data", {})
        key = item.get("key", "")
        title = data.get("title", "")
        author_raw = ""
        for c in data.get("creators", []):
            if c.get("creatorType") == "author":
                author_raw = c.get("lastName", "") or c.get("name", "")
                break
        doi = data.get("DOI", "")

        print(f"[{i}/{len(targets)}] {title[:55]}")

        # CrossRef 조회
        cr = None
        if doi:
            print(f"  DOI: {doi}")
            cr = _fetch_crossref_by_doi(doi)
        if cr is None:
            cr = _fetch_crossref_by_query(title, author_raw)
            if cr:
                print(f"  [검색] CrossRef 결과 발견")

        time.sleep(CROSSREF_DELAY)

        if cr is None:
            print(f"  [스킵] CrossRef 결과 없음")
            not_found += 1
            continue

        new_fields = _extract_fields(cr)
        if not new_fields:
            print(f"  [스킵] 추출 가능한 새 필드 없음")
            continue

        # 실제로 채울 수 있는 빈 필드만 필터
        fillable = {
            f: v for f, v in new_fields.items()
            if not data.get(f) and v
        }
        if not fillable:
            print(f"  [스킵] 모든 필드 이미 채워져 있음")
            continue

        print(f"  → 채울 필드: {list(fillable.keys())}")

        if args.dry_run:
            continue

        # Zotero 업데이트
        if _update_zotero_item(zot, item, fillable):
            print(f"  ✓ Zotero 업데이트")
            zotero_updated += 1

        # 마크다운 업데이트
        md_path = _find_markdown_by_key(key)
        if md_path:
            if _update_markdown(md_path, fillable, ZOTERO_TO_MD):
                print(f"  ✓ 마크다운 업데이트: {md_path.name}")
                md_updated += 1

    print(f"\n{'=' * 60}")
    if args.dry_run:
        print(f"[dry-run] 실제 변경 없음")
    else:
        print(f"완료!")
        print(f"  Zotero 업데이트: {zotero_updated}개")
        print(f"  마크다운 업데이트: {md_updated}개")
        print(f"  CrossRef 미발견: {not_found}개")


if __name__ == "__main__":
    main()

"""기존 마크다운에 새 서지정보 필드 추가

대상: 표준 템플릿 파일 (## 서지정보 섹션 + **저자** 필드 있는 파일)
작업:
  1. 서지정보 섹션에 신규 필드 줄 삽입 (저널/출처 뒤, 태그 앞)
     - 출판사, 권(Vol), 호(Issue), 페이지, DOI, ISSN, URL, 언어
  2. frontmatter에 `doi:` 줄 추가 (zotero_key: 뒤)
  3. 이미 추가된 파일은 건너뜀

사용법:
  python3 migrate_biblio_fields.py            # dry-run (변경 없이 미리보기)
  python3 migrate_biblio_fields.py --apply    # 실제 적용
"""

import argparse
import re
from pathlib import Path

from config import MARKDOWN_DIR

# 새로 삽입할 필드 줄 (태그 바로 앞에 삽입)
NEW_FIELDS_BLOCK = (
    "- **출판사**: \n"
    "- **권(Vol)**: \n"
    "- **호(Issue)**: \n"
    "- **페이지**: \n"
    "- **DOI**: \n"
    "- **ISSN**: \n"
    "- **URL**: \n"
    "- **언어**: "
)


def _has_new_fields(text: str) -> bool:
    """이미 새 필드가 있는지 확인."""
    return "**DOI**:" in text and "**권(Vol)**:" in text


def _has_standard_biblio(text: str) -> bool:
    """표준 서지정보 섹션이 있는지 확인."""
    return "**저자**:" in text and "## 서지정보" in text


def migrate_file(md_path: Path, apply: bool) -> str:
    """파일 1개 마이그레이션. 결과 상태 문자열 반환."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"읽기 실패: {e}"

    if _has_new_fields(text):
        return "skip_already"

    if not _has_standard_biblio(text):
        return "skip_nonstandard"

    original = text

    # ── 1. frontmatter에 doi: 추가 (zotero_key: 줄 뒤) ──────────────────────
    if "doi:" not in text:
        text, n = re.subn(
            r'^(zotero_key:.*)$',
            r'\1\ndoi:',
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n == 0:
            # zotero_key 없으면 created: 뒤에 삽입
            text, _ = re.subn(
                r'^(created:.*)$',
                r'\1\ndoi:',
                text,
                count=1,
                flags=re.MULTILINE,
            )

    # ── 2. 서지정보 섹션 필드 삽입 ──────────────────────────────────────────
    # **태그**: 줄 바로 앞에 새 필드 블록 삽입
    # 우선 **저널/출처**: 뒤 + **태그**: 앞 위치에 삽입 시도
    new_text, n = re.subn(
        r'(\*\*저널/출처\*\*:.*?)(\n- \*\*태그\*\*:)',
        rf'\1\n{NEW_FIELDS_BLOCK}\2',
        text,
        count=1,
        flags=re.DOTALL,
    )
    if n:
        text = new_text
    else:
        # 태그 줄이 없는 경우 — **저널/출처**: 줄 바로 다음에 삽입
        new_text, n = re.subn(
            r'(\*\*저널/출처\*\*:[^\n]*\n)',
            rf'\1{NEW_FIELDS_BLOCK}\n',
            text,
            count=1,
        )
        if n:
            text = new_text
        else:
            # 저널 없는 경우 — **연도**: 줄 바로 다음에 삽입
            new_text, n = re.subn(
                r'(\*\*연도\*\*:[^\n]*\n)',
                rf'\1{NEW_FIELDS_BLOCK}\n',
                text,
                count=1,
            )
            if n:
                text = new_text
            else:
                return "skip_pattern_not_found"

    if text == original:
        return "skip_no_change"

    if apply:
        try:
            md_path.write_text(text, encoding="utf-8")
        except Exception as e:
            return f"쓰기 실패: {e}"
        return "updated"
    else:
        return "dry_updated"


def main():
    parser = argparse.ArgumentParser(description="기존 마크다운에 서지정보 필드 추가")
    parser.add_argument("--apply", action="store_true", help="실제 파일 수정 (없으면 dry-run)")
    args = parser.parse_args()

    files = sorted(MARKDOWN_DIR.glob("@*.md"))
    print("=" * 60)
    print(f"서지정보 필드 마이그레이션 {'[APPLY]' if args.apply else '[DRY-RUN]'}")
    print(f"대상 폴더: {MARKDOWN_DIR}")
    print(f"전체 파일: {len(files)}개")
    print("=" * 60)

    stats = {
        "updated": 0,
        "dry_updated": 0,
        "skip_already": 0,
        "skip_nonstandard": 0,
        "skip_pattern_not_found": 0,
        "skip_no_change": 0,
        "error": 0,
    }

    for md_path in files:
        result = migrate_file(md_path, apply=args.apply)
        if result.startswith("읽기") or result.startswith("쓰기"):
            stats["error"] += 1
            print(f"  [오류] {md_path.name[:60]}: {result}")
        elif result == "updated":
            stats["updated"] += 1
        elif result == "dry_updated":
            stats["dry_updated"] += 1
        elif result == "skip_pattern_not_found":
            stats["skip_pattern_not_found"] += 1
            print(f"  [패턴없음] {md_path.name[:70]}")
        else:
            stats[result] = stats.get(result, 0) + 1

    print()
    print("=" * 60)
    if args.apply:
        print(f"완료!")
        print(f"  업데이트: {stats['updated']}개")
    else:
        print(f"[DRY-RUN] 실제 변경 없음")
        print(f"  업데이트 예정: {stats['dry_updated']}개")
    print(f"  이미 적용됨 (스킵): {stats['skip_already']}개")
    print(f"  비표준 형식 (스킵): {stats['skip_nonstandard']}개")
    print(f"  패턴 미발견 (스킵): {stats['skip_pattern_not_found']}개")
    if stats["error"]:
        print(f"  오류: {stats['error']}개")

    if not args.apply and (stats["dry_updated"] > 0):
        print()
        print("→ 적용하려면: python3 migrate_biblio_fields.py --apply")


if __name__ == "__main__":
    main()

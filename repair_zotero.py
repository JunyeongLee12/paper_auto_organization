"""Zotero 데이터 품질 수정 스크립트

처리 순서:
  1. 중복 아이템 삭제 (대응 마크다운 없는 아이템)
  2. 저자 없는 아이템 → PDF 텍스트에서 재추출 후 Zotero 업데이트
  3. 이니셜 오파싱 아이템 → 마크다운 재파싱 후 업데이트
  4. obsidian_to_zotero.py parse_body 버그 수정 확인

사용법:
  python3 repair_zotero.py --dry-run   # 변경 없이 분석만
  python3 repair_zotero.py             # 실제 수정
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from pyzotero import zotero

from config import JSON_PATH, MARKDOWN_DIR, ZOTERO_API_KEY, ZOTERO_LIBRARY_ID

VAULT = MARKDOWN_DIR
API_DELAY = 0.5  # API 호출 간 딜레이(초)


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def load_key_to_md() -> dict[str, Path]:
    """zotero_key → 마크다운 파일 경로 매핑."""
    mapping = {}
    for md_path in VAULT.glob("*.md"):
        txt = md_path.read_text(encoding="utf-8")
        m = re.search(r"^zotero_key:\s*(\S+)", txt, re.MULTILINE)
        if m:
            mapping[m.group(1)] = md_path
    return mapping


def load_json_papers() -> dict[str, dict]:
    """JSON → {file_name: paper} 매핑."""
    papers = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    return {p["file_name"]: p for p in papers}


def get_all_items(zot) -> list[dict]:
    """Zotero 전체 아이템 조회 (attachment/note 제외)."""
    items = []
    start = 0
    while True:
        batch = zot.items(limit=100, start=start, itemType="-attachment || note")
        if not batch:
            break
        items.extend(batch)
        start += 100
        if len(batch) < 100:
            break
    return items


# ── 저자 파싱 (개선된 버전) ────────────────────────────────────────────────────

def parse_author_string(author_str: str) -> list[dict]:
    """저자 문자열 → Zotero creators 리스트.

    지원 형식:
      - "First Last, First Last" (쉼표 구분, 이름+성 순서)
      - "Last, First; Last, First" (세미콜론 구분, 성+이름 순서)
      - "성이름, 성이름" (한국어, 쉼표 구분)
    """
    if not author_str or author_str.strip() in ("-", ""):
        return []

    creators = []

    # 세미콜론이 있으면 → "성, 이름; 성, 이름" 형식
    if ";" in author_str:
        segments = [s.strip() for s in author_str.split(";") if s.strip()]
        for seg in segments:
            seg = seg.strip().rstrip(",")
            if "," in seg:
                parts = seg.split(",", 1)
                last = parts[0].strip()
                first = parts[1].strip()
                if last:
                    creators.append({"creatorType": "author",
                                     "lastName": last, "firstName": first})
            elif seg:
                creators.append({"creatorType": "author", "name": seg})
        return creators

    # 세미콜론 없으면 → "First Last, First Last" 형식으로 시도
    # 쉼표 다음에 대문자(영문) 또는 한글이 오면 저자 구분자
    segments = re.split(r",\s*(?=[A-Z가-힣])", author_str)

    # 단일 "성, 이름" 형식 감지: 세그먼트 2개이고 첫 번째가 한 단어, 두 번째도 한 단어
    if (len(segments) == 2
            and len(segments[0].split()) == 1
            and len(segments[1].split()) <= 2
            and not re.search(r"[가-힣]", author_str)):
        # "Kahneman, Daniel" 형식
        return [{"creatorType": "author",
                 "lastName": segments[0].strip(),
                 "firstName": segments[1].strip()}]

    for seg in segments:
        seg = seg.strip().rstrip(",;")
        if seg:
            creators.append({"creatorType": "author", "name": seg})

    return creators


def extract_author_from_text(full_text: str, title: str = "") -> str:
    """PDF 전문 텍스트에서 저자명 추출 시도."""
    if not full_text:
        return ""

    text = full_text[:3000]

    # 영문 논문: 저자 패턴
    patterns = [
        # "Author(s): Name" 형식
        r"(?:Authors?|By)[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]*\.?)+(?:\s*,\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]*\.?)+)*)",
        # 제목 다음 줄의 이름 패턴 (영문)
        r"\n([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s*,\s*[A-Z][a-z]+\s+[A-Z][a-z]+)*)\n",
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            candidate = m.group(1).strip()
            # 너무 길거나 제목과 겹치면 스킵
            if len(candidate) < 100 and candidate.lower() not in title.lower():
                return candidate

    # 한국어 논문: "저자:" 또는 "연구자:" 패턴
    kor_m = re.search(r"(?:저자|연구자|글쓴이)[:\s：]+([가-힣\s,]+)", text)
    if kor_m:
        candidate = kor_m.group(1).strip()[:100]
        if candidate:
            return candidate

    return ""


# ── 수정 함수들 ────────────────────────────────────────────────────────────────

def delete_duplicates(zot, all_items, key_to_md, dry_run):
    """대응 마크다운 없는 중복 아이템 삭제."""
    to_delete = [item for item in all_items if item["key"] not in key_to_md]

    print(f"\n[1단계] 중복 아이템 삭제: {len(to_delete)}개")
    if dry_run:
        for item in to_delete[:5]:
            print(f"  삭제 예정: [{item['key']}] {item['data'].get('title','')[:50]}")
        if len(to_delete) > 5:
            print(f"  ... 외 {len(to_delete)-5}개")
        return 0

    deleted = 0
    # Zotero API는 삭제 시 version 필요
    for item in to_delete:
        try:
            zot.delete_item(item)
            deleted += 1
            time.sleep(API_DELAY)
        except Exception as e:
            print(f"  [오류] 삭제 실패 [{item['key']}]: {e}")

    print(f"  → 삭제 완료: {deleted}개")
    return deleted


# 가비지 저자명 필터
_GARBAGE_WORDS = {
    "primary adviser", "adviser", "advisor", "committee",
    "graduate division", "graduate school", "department",
    "business culture", "manufacturing companies",
    "bibliographic services", "social networks",
    "strategical orientation", "entrepreneurship",
    "the puzzle", "doctor", "professor", "university",
    "institute", "college", "school", "faculty",
}


def is_valid_author(creator: dict) -> bool:
    """저자 딕셔너리가 유효한 사람 이름인지 검증."""
    name = (creator.get("name", "") or
            f"{creator.get('lastName', '')} {creator.get('firstName', '')}").strip()
    # 개행 포함 → 무효
    if "\n" in name or "\r" in name:
        return False
    # 너무 짧음 (1글자 이름)
    if len(name.strip()) <= 1:
        return False
    # 알려진 가비지 패턴
    name_lower = name.lower()
    if any(g in name_lower for g in _GARBAGE_WORDS):
        return False
    return True


def fix_authors(zot, all_items, key_to_md, json_papers, dry_run):
    """저자 없거나 오파싱된 아이템 수정."""

    def has_bad_authors(item):
        creators = item["data"].get("creators", [])
        authors = [c for c in creators if c.get("creatorType") == "author"]
        if not authors:
            return True
        # 이니셜만 있는 경우 (1~2글자)
        for c in authors:
            name = c.get("name", "") or c.get("lastName", "")
            if len(name.strip()) <= 2:
                return True
        return False

    to_fix = [item for item in all_items
              if item["key"] in key_to_md and has_bad_authors(item)]

    print(f"\n[2단계] 저자 수정 대상: {len(to_fix)}개")

    fixed = 0
    skipped = 0

    for item in to_fix:
        key = item["key"]
        md_path = key_to_md[key]
        txt = md_path.read_text(encoding="utf-8")
        title = item["data"].get("title", "")

        # 마크다운에서 재파싱 (개선된 [ \t]* 패턴)
        m = re.search(r"\*\*저자\*\*:[ \t]*(.+)", txt)
        md_author = m.group(1).strip().rstrip("\r") if m else ""

        # 마크다운에도 없으면 PDF 텍스트에서 추출 시도
        if not md_author or md_author in ("-", ""):
            pdf_m = re.search(r"\[\[(.+?\.pdf)\]\]", txt, re.IGNORECASE)
            pdf_name = pdf_m.group(1) if pdf_m else ""
            paper = json_papers.get(pdf_name, {})
            md_author = extract_author_from_text(
                paper.get("full_text", ""), title
            )

        if not md_author:
            skipped += 1
            continue

        new_creators = parse_author_string(md_author)
        if not new_creators:
            skipped += 1
            continue

        # 가비지 필터링
        valid_creators = [c for c in new_creators if is_valid_author(c)]
        if not valid_creators:
            print(f"  스킵(가비지): [{key}] {title[:40]} → {[c.get('name') or c.get('lastName') for c in new_creators]}")
            skipped += 1
            continue
        if len(valid_creators) < len(new_creators):
            removed = [c.get('name') or c.get('lastName') for c in new_creators if c not in valid_creators]
            print(f"  가비지 제거: [{key}] {removed}")
        new_creators = valid_creators

        # 기존 비저자(editor 등) 유지
        other_creators = [c for c in item["data"].get("creators", [])
                          if c.get("creatorType") != "author"]
        item["data"]["creators"] = new_creators + other_creators

        if dry_run:
            print(f"  수정 예정: [{key}] {title[:40]}")
            for c in new_creators[:2]:
                print(f"    → {c}")
            fixed += 1
            continue

        try:
            zot.update_item(item)
            print(f"  ✓ [{key}] {title[:45]}")
            fixed += 1
            time.sleep(API_DELAY)
        except Exception as e:
            print(f"  ✗ [{key}] 업데이트 실패: {e}")
            skipped += 1

    print(f"  → {'수정 예정' if dry_run else '수정 완료'}: {fixed}개 / 스킵: {skipped}개")
    return fixed


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Zotero 데이터 품질 수정")
    parser.add_argument("--dry-run", action="store_true", help="분석만, 실제 변경 없음")
    args = parser.parse_args()

    if not ZOTERO_LIBRARY_ID or not ZOTERO_API_KEY:
        print("[오류] Zotero 설정 없음")
        sys.exit(1)

    print("=" * 60)
    print(f"Zotero 데이터 수정 {'[DRY-RUN]' if args.dry_run else ''}")
    print("=" * 60)

    zot = zotero.Zotero(ZOTERO_LIBRARY_ID, "user", ZOTERO_API_KEY)

    print("데이터 로드 중...")
    key_to_md = load_key_to_md()
    json_papers = load_json_papers()
    all_items = get_all_items(zot)

    print(f"  Zotero 아이템: {len(all_items)}개")
    print(f"  마크다운 파일: {len(key_to_md)}개")

    # 1단계: 중복 삭제
    delete_duplicates(zot, all_items, key_to_md, args.dry_run)

    # 2단계: 저자 수정
    # 삭제 후 남은 아이템만 대상
    remaining = [item for item in all_items if item["key"] in key_to_md]
    fix_authors(zot, remaining, key_to_md, json_papers, args.dry_run)

    print("\n완료!")


if __name__ == "__main__":
    main()

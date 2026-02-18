"""JSON + AI 요약 결과를 Obsidian 마크다운 파일로 생성"""

import os
import re
from datetime import date
from pathlib import Path

from config import MARKDOWN_DIR, MARKDOWN_TEMPLATE
from moc_manager import format_moc_links, create_new_moc, MOC_DIR


def _sanitize_filename(name: str) -> str:
    """파일명에 사용할 수 없는 문자를 하이픈으로 대체."""
    # Windows 금지 문자 + 추가 특수문자 제거
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "-", name)
    # 연속 하이픈 정리
    name = re.sub(r"-{2,}", "-", name)
    # 앞뒤 공백/하이픈 제거
    name = name.strip(" -")
    # 공백을 하이픈으로
    name = name.replace(" ", "-")
    return name


def _build_filename(summary: dict) -> str:
    """@연도_제목.md 형식의 파일명 생성."""
    year = summary.get("year", "")
    title = summary.get("title", "Untitled")

    # 제목 길이 제한 (파일명이 너무 길지 않도록)
    safe_title = _sanitize_filename(title)
    if len(safe_title) > 80:
        safe_title = safe_title[:80].rsplit("-", 1)[0]

    if year:
        return f"@{year}_{safe_title}.md"
    return f"@_{safe_title}.md"


def _format_tags_yaml(tags: list[str]) -> str:
    """YAML frontmatter용 태그 문자열: literature, paper, tag1, tag2"""
    base = ["literature", "paper"]
    extra = [t.lower().replace(" ", "-") for t in tags if t]
    all_tags = base + [t for t in extra if t not in base]
    return ", ".join(all_tags)


def _format_hashtags(tags: list[str]) -> str:
    """본문용 해시태그: #tag1 #tag2"""
    if not tags:
        return ""
    return " ".join(f"#{t.lower().replace(' ', '-')}" for t in tags if t)


def _to_bullets(value) -> str:
    """리스트 또는 문자열을 마크다운 불릿 리스트로 변환."""
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value if item)
    return str(value) if value else ""


def _to_numbered(value) -> str:
    """리스트 또는 문자열을 마크다운 번호 리스트로 변환."""
    if isinstance(value, list):
        lines = []
        for i, item in enumerate(value, 1):
            if not item:
                continue
            # 이미 번호가 붙어 있으면 그대로, 아니면 번호 추가
            text = str(item)
            if re.match(r"^\d+[\.\)]\s", text):
                lines.append(text)
            else:
                lines.append(f"{i}. {text}")
        return "\n".join(lines)
    return str(value) if value else ""


def _to_quotes(value) -> str:
    """리스트 또는 문자열을 마크다운 인용 형식으로 변환."""
    if isinstance(value, list):
        return "\n\n".join(f'> "{item}"' for item in value if item)
    return str(value) if value else ""


def _to_excerpts(value) -> str:
    """내용 발췌 필드 렌더링.

    - 신규 형식: 구조화된 마크다운 문자열은 그대로 출력
    - 구형 형식: 리스트면 불릿으로 변환 (하위호환)
    """
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value if item)
    return str(value).strip() if value else ""


def generate_markdown(summary: dict, pdf_filename: str, zotero_key: str = "") -> Path | None:
    """요약 결과로 마크다운 파일을 생성.

    이미 같은 파일이 존재하면 스킵.

    Args:
        summary: summarizer.summarize_paper()의 반환값
        pdf_filename: 원본 PDF 파일명
        zotero_key: Zotero 아이템 키 (Zotero 연동 시 전달)

    Returns:
        생성된 파일 경로. 스킵된 경우 None.
    """
    md_name = _build_filename(summary)
    md_path = MARKDOWN_DIR / md_name

    if md_path.exists():
        print(f"  [스킵] 이미 존재: {md_name}")
        return None

    # 동일 연도+제목으로 시작하는 기존 파일이 있는지도 확인
    year = summary.get("year", "")
    if year:
        prefix = f"@{year}_"
        existing = list(MARKDOWN_DIR.glob(f"{prefix}*.md"))
        title_part = _sanitize_filename(summary.get("title", ""))[:30]
        for existing_file in existing:
            if title_part and title_part.lower() in existing_file.name.lower():
                print(f"  [스킵] 유사 파일 존재: {existing_file.name}")
                return None

    tags = summary.get("tags", [])

    # YAML frontmatter용 제목: 따옴표 이스케이프
    title = summary.get("title", "")
    yaml_title = title.replace('"', '\\"')

    # 확장자 대소문자 무관하게 제거 후 .pdf 추가
    base, ext = os.path.splitext(pdf_filename)
    safe_pdf_name = base + ".pdf"

    # MOC 처리: AI가 제안한 moc_assignments를 파싱하여 링크 생성
    moc_assignments = summary.get("moc_assignments", [])
    moc_names = []
    for moc in moc_assignments:
        name = moc.get("name", "")
        if not name:
            continue
        if not name.startswith("MOC_"):
            name = f"MOC_{name}"
        if moc.get("is_new"):
            create_new_moc(name, moc.get("description", ""))
        else:
            # 기존 MOC 존재 여부 검증 — 없으면 자동 생성
            if not (MOC_DIR / f"{name}.md").exists():
                create_new_moc(name, moc.get("description", ""))
        moc_names.append(name)
    moc_links = format_moc_links(moc_names)

    content = MARKDOWN_TEMPLATE.format(
        title=yaml_title,
        year=year or "미상",
        tags=_format_tags_yaml(tags),
        created=date.today().isoformat(),
        zotero_key=zotero_key or "",
        doi=summary.get("doi", ""),
        author=summary.get("author", ""),
        journal=summary.get("journal", ""),
        publisher=summary.get("publisher", ""),
        volume=summary.get("volume", ""),
        issue=summary.get("issue", ""),
        pages=summary.get("pages", ""),
        issn=summary.get("issn", ""),
        url=summary.get("url", ""),
        language=summary.get("language", ""),
        hashtags=_format_hashtags(tags),
        abstract=summary.get("abstract", ""),
        key_claims=_to_bullets(summary.get("key_claims", "")),
        method=summary.get("method", ""),
        findings=_to_numbered(summary.get("findings", "")),
        excerpts=_to_excerpts(summary.get("excerpts", "")),
        pdf_filename=safe_pdf_name,
        moc_links=moc_links,
    )

    md_path.write_text(content, encoding="utf-8")
    print(f"  → 생성: {md_name}")
    return md_path


if __name__ == "__main__":
    # 테스트용
    test_summary = {
        "title": "Test Paper Title",
        "author": "Test Author",
        "year": "2024",
        "journal": "Test Journal",
        "abstract": "This is a test abstract.",
        "key_claims": "- Claim 1\n- Claim 2",
        "method": "Survey method",
        "findings": "1. Finding 1\n2. Finding 2",
        "tags": ["test", "example"],
    }
    result = generate_markdown(test_summary, "test_paper.pdf")
    if result:
        print(f"Created: {result}")

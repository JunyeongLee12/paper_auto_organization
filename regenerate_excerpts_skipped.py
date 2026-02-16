"""내용 발췺 재생성 — 비표준 포맷 및 PDF 데이터 활용 버전.

1차 regenerate_excerpts.py에서 skip_no_content 처리된 파일 대상:
  - 비표준 헤더 파일: 마크다운 본문 전체를 소스로 사용
  - PDF 데이터 있는 파일: extracted_papers.json의 full_text 활용
  - 진짜 빈 파일: 스킵

사용법:
  python3 regenerate_excerpts_skipped.py --limit 3   # 테스트
  python3 regenerate_excerpts_skipped.py             # 전체 실행
"""

import argparse
import json
import re
from pathlib import Path

from config import MARKDOWN_DIR, GEMINI_MODEL, JSON_PATH
from summarizer import _call_gemini_model


PROMPT = """\
학술 논문 분석 전문가로서 아래 논문 내용을 바탕으로 '내용 발췺' 섹션을 작성하세요.

JSON 없이, 아래 마크다운 형식만 출력하세요.

### **논문 핵심 분석: [논문 주제 한 줄]**

#### **1. [핵심 소주제 1]**
[서술형 분석 2-3문장]

* **[세부 개념]:** [설명]
* **[세부 개념]:** [설명]

#### **2. [핵심 소주제 2]**
[서술형 분석]

* [포인트]

### **요약 결론 (Executive Summary)**
[실무·학문적 시사점 중심 3~5문장]

---
논문 정보:
제목: {title}
저자: {author}
연도: {year}
저널: {journal}

[논문 내용]
{content}
"""

PLACEHOLDER_MARKERS = (
    "논문을 읽고 핵심 주장을 정리하세요",
    "원문을 확인",
    "초록을 추출할 수 없습니다",
    "주요 연구 결과를 정리하세요",
    "연구 방법론을 정리하세요",
    "직접 인용할 만한 구절",
    "AI 심층 분석 실행 시",
)


def _needs_update(text: str) -> bool:
    m = re.search(r"\n## [^\n]*\(Excerpts\)\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    if not m:
        return False
    return "### **논문 핵심 분석" not in m.group(1)


def _has_standard_content(text: str) -> bool:
    """표준 헤더 섹션에 충분한 내용이 있는지."""
    for sec in ("## 초록/요약 (Abstract)", "## 핵심 주장 (Key Claims)", "## 주요 발견 (Findings)"):
        m = re.search(rf"\n{re.escape(sec)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if m:
            s = m.group(1).strip()
            if s and len(s) > 80 and not any(mk in s for mk in PLACEHOLDER_MARKERS):
                return True
    return False


def _extract_body_content(text: str) -> str:
    """frontmatter 이후 ~ 발췺/나의생각/연결 섹션 이전의 본문 추출."""
    # frontmatter 제거
    body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
    # # 제목 줄 제거
    body = re.sub(r"^# [^\n]+\n", "", body)
    # 발췺, 나의 생각, 연결, 원본 파일 섹션 이후 제거
    stop_pattern = r"\n## [^\n]*(Excerpts|나의 생각|My Thoughts|연결|Links)[^\n]*\n"
    m = re.search(stop_pattern, body, re.IGNORECASE)
    if m:
        body = body[:m.start()]
    # 플레이스홀더 줄 제거
    lines = [l for l in body.splitlines() if not any(mk in l for mk in PLACEHOLDER_MARKERS)]
    body = "\n".join(lines).strip()
    return body


def _replace_excerpts(text: str, new_body: str) -> str:
    def replacer(mo):
        return mo.group(1) + new_body + "\n" + mo.group(3)
    return re.sub(
        r"(\n## [^\n]*\(Excerpts\)\n)(.*?)(\n## [^\n]+|\Z)",
        replacer, text, count=1, flags=re.DOTALL,
    )


def process_file(md_path: Path, paper_map: dict) -> str:
    text = md_path.read_text(encoding="utf-8", errors="ignore")

    if not _needs_update(text):
        return "skip_structured"

    if _has_standard_content(text):
        return "skip_standard_ok"  # 표준 스크립트가 처리해야 함

    # YAML 메타 추출
    title_m  = re.search(r'^title:\s*"?(.+?)"?\s*$', text, re.MULTILINE)
    year_m   = re.search(r'^year:\s*(\S+)', text, re.MULTILINE)
    author_m = re.search(r'(?:\*\*저자\*\*|authors?):\s*(.+)', text)
    journal_m = re.search(r'(?:\*\*저널/출처\*\*|source|journal):\s*(.+)', text)
    title   = title_m.group(1).strip() if title_m else md_path.stem
    year    = year_m.group(1).strip() if year_m else ""
    author  = (author_m.group(1).strip() if author_m else "미상") or "미상"
    journal = (journal_m.group(1).strip() if journal_m else "미상") or "미상"

    # 소스 콘텐츠 결정 (우선순위: PDF full_text > 마크다운 본문)
    pdf_m = re.search(r"\[\[(.+?\.pdf)\]\]", text, re.IGNORECASE)
    pdf_name = pdf_m.group(1) if pdf_m else None
    paper = paper_map.get(pdf_name, {}) if pdf_name else {}
    full_text = paper.get("full_text", "")

    if len(full_text) > 500:
        content = full_text[:25000]
        source = "PDF"
    else:
        body = _extract_body_content(text)
        if len(body) < 300:
            return "skip_no_content"
        content = body[:8000]
        source = "MD"

    prompt = PROMPT.format(
        title=title[:200], author=author[:100],
        year=year, journal=journal[:100],
        content=content,
    )

    raw = _call_gemini_model(prompt, GEMINI_MODEL)
    if not raw:
        return "api_fail"

    raw = re.sub(r"^```(?:markdown)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw.strip(), flags=re.MULTILINE)
    raw = raw.strip()

    if "### **논문 핵심 분석" not in raw:
        return "api_bad_format"

    new_text = _replace_excerpts(text, raw)
    if new_text == text:
        return "skip_no_change"

    try:
        md_path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return f"write_error"

    return f"updated({source})"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    # PDF 데이터 로드
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            papers = json.load(f)
        paper_map = {p["file_name"]: p for p in papers}
        print(f"PDF 데이터 로드: {len(paper_map)}개")
    except Exception as e:
        print(f"[경고] JSON 로드 실패: {e}")
        paper_map = {}

    # 대상 파일 수집
    targets = []
    for f in sorted(MARKDOWN_DIR.glob("@*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            if _needs_update(text) and not _has_standard_content(text):
                targets.append(f)
        except Exception:
            pass

    total = len(targets)
    if args.limit:
        targets = targets[: args.limit]

    print("=" * 60)
    print("내용 발췺 AI 재생성 (비표준/PDF 버전)")
    print(f"대상: {total}개")
    if args.limit:
        print(f"  → --limit {args.limit} 적용")
    print("=" * 60)

    stats: dict[str, int] = {}
    for i, md_path in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {md_path.name[:60]}", end=" ... ", flush=True)
        result = process_file(md_path, paper_map)
        stats[result] = stats.get(result, 0) + 1
        print(result)

    print()
    print("=" * 60)
    updated = sum(v for k, v in stats.items() if k.startswith("updated"))
    print(f"완료!")
    print(f"  업데이트: {updated}개  (PDF:{stats.get('updated(PDF)',0)} / MD:{stats.get('updated(MD)',0)})")
    print(f"  내용 없어 스킵: {stats.get('skip_no_content', 0)}개")
    print(f"  API 실패: {stats.get('api_fail', 0)}개")
    print(f"  형식 오류: {stats.get('api_bad_format', 0)}개")


if __name__ == "__main__":
    main()

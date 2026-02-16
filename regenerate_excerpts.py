"""기존 마크다운 '내용 발췌 (Excerpts)' 섹션을 Gemini AI로 구조화 형식 재생성.

참고 형식 (발췌 예시.md):
  ### **논문 핵심 분석: [논문 주제 한 줄 요약]**

  #### **1. [첫 번째 핵심 소주제]**
  [원문 근거를 반영한 서술형 분석 2-3문장]

  * **[소항목]:** [설명]
  * **[소항목]:** [설명]

  #### **2. [두 번째 핵심 소주제]**
  ...

  ### **요약 결론 (Executive Summary)**
  [실무적 시사점 중심 3~5문장 요약]

대상: ### **논문 핵심 분석** 헤더가 없는 @*.md 파일
소스: 기존 마크다운의 초록/핵심주장/방법/발견 섹션

사용법:
  python3 regenerate_excerpts.py --limit 3  # 3개 테스트 후 확인
  python3 regenerate_excerpts.py            # 전체 실행
"""

import argparse
import re

from pathlib import Path

from config import MARKDOWN_DIR, GEMINI_MODEL
from summarizer import _call_gemini_model


# ── 프롬프트 ──────────────────────────────────────────────────────────────────

PROMPT = """\
학술 논문 분석 전문가로서 아래 논문 정보를 바탕으로 '내용 발췌' 섹션을 작성하세요.

**출력 규칙:**
- JSON 형식 금지. 마크다운 텍스트만 출력.
- 앞뒤 설명 없이 아래 형식만 출력.
- 소주제는 2~4개. 각 소주제에 서술형 분석(2~3문장) + 불릿 포인트.
- 서술형 문장은 논문 내용에 근거한 심층 분석 (단순 나열 금지).
- 요약 결론은 실무·학문적 시사점 중심 3~5문장.

**출력 형식:**
### **논문 핵심 분석: [논문의 핵심 주제 한 줄 요약]**

#### **1. [첫 번째 핵심 소주제]**
[원문 내용에 근거한 서술형 분석 2~3문장]

* **[세부 개념]:** [설명]
* **[세부 개념]:** [설명]

#### **2. [두 번째 핵심 소주제]**
[서술형 분석]

* [포인트]
* [포인트]

(내용이 풍부하면 3~4번째 소주제 추가)

### **요약 결론 (Executive Summary)**
[실무·학문적 시사점 중심 3~5문장]

---
논문 정보:
제목: {title}
저자: {author}
연도: {year}
저널: {journal}

[초록]
{abstract}

[핵심 주장]
{key_claims}

[연구 방법]
{method}

[주요 발견]
{findings}
"""


# ── 헬퍼 함수 ─────────────────────────────────────────────────────────────────

def _get_section(text: str, header: str) -> str:
    """## 헤더 섹션의 내용 추출."""
    m = re.search(rf"\n{re.escape(header)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _needs_update(text: str) -> bool:
    """구조화 형식이 없는 파일인지 확인."""
    # Korean 철자 무관하게 (Excerpts) 앵커로 매칭
    m = re.search(r"\n## [^\n]*\(Excerpts\)\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    if not m:
        return False
    return "### **논문 핵심 분석" not in m.group(1)


def _replace_excerpts(text: str, new_body: str) -> str:
    """내용 발췺 (Excerpts) 섹션 내용만 new_body로 교체."""

    def replacer(mo):
        return mo.group(1) + new_body + "\n" + mo.group(3)

    return re.sub(
        r"(\n## [^\n]*\(Excerpts\)\n)(.*?)(\n## [^\n]+|\Z)",
        replacer,
        text,
        count=1,
        flags=re.DOTALL,
    )


# ── 파일 처리 ─────────────────────────────────────────────────────────────────

PLACEHOLDER_MARKERS = (
    "논문을 읽고 핵심 주장을 정리하세요",
    "원문을 확인",
    "초록을 추출할 수 없습니다",
    "주요 연구 결과를 정리하세요",
    "연구 방법론을 정리하세요",
)


def process_file(md_path: Path) -> str:
    """파일 1개 처리. 결과 상태 문자열 반환."""
    try:
        text = md_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"read_error: {e}"

    if not _needs_update(text):
        return "skip_structured"

    # YAML frontmatter 필드 추출
    title_m = re.search(r'^title:\s*"?(.+?)"?\s*$', text, re.MULTILINE)
    year_m  = re.search(r'^year:\s*(\S+)', text, re.MULTILINE)
    title   = title_m.group(1).strip() if title_m else md_path.stem
    year    = year_m.group(1).strip() if year_m else ""

    author_m  = re.search(r'\*\*저자\*\*:\s*(.+)', text)
    journal_m = re.search(r'\*\*저널/출처\*\*:\s*(.+)', text)
    author  = (author_m.group(1).strip() if author_m else "미상") or "미상"
    journal = (journal_m.group(1).strip() if journal_m else "미상") or "미상"

    abstract   = _get_section(text, "## 초록/요약 (Abstract)")
    key_claims = _get_section(text, "## 핵심 주장 (Key Claims)")
    method     = _get_section(text, "## 연구 방법 (Method)")
    findings   = _get_section(text, "## 주요 발견 (Findings)")

    # 내용 충분성 확인
    usable = [
        s for s in (abstract, key_claims, findings)
        if s and len(s) > 80 and not any(m in s for m in PLACEHOLDER_MARKERS)
    ]
    if not usable:
        return "skip_no_content"

    prompt = PROMPT.format(
        title=title[:200],
        author=author[:100],
        year=year,
        journal=journal[:100],
        abstract=abstract[:2000],
        key_claims=key_claims[:1500],
        method=method[:1000],
        findings=findings[:1500],
    )

    raw = _call_gemini_model(prompt, GEMINI_MODEL)
    if not raw:
        return "api_fail"

    # 코드블록 래퍼 제거
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
        return f"write_error: {e}"

    return "updated"


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="내용 발췺 Gemini AI 재생성")
    parser.add_argument("--limit", type=int, default=0, help="처리 파일 수 제한 (0=전체)")
    args = parser.parse_args()

    all_files = sorted(MARKDOWN_DIR.glob("@*.md"))

    # 업데이트 필요 파일만 추려서 targets 구성
    targets = []
    for f in all_files:
        try:
            if _needs_update(f.read_text(encoding="utf-8", errors="ignore")):
                targets.append(f)
        except Exception:
            pass

    total_targets = len(targets)
    if args.limit:
        targets = targets[: args.limit]

    print("=" * 60)
    print("내용 발췺 AI 재생성")
    print(f"전체 @*.md: {len(all_files)}개")
    print(f"업데이트 대상: {total_targets}개")
    if args.limit:
        print(f"  → --limit {args.limit} 적용: {len(targets)}개만 처리")
    print("=" * 60)

    stats: dict[str, int] = {}

    for i, md_path in enumerate(targets, 1):
        label = md_path.name[:65]
        print(f"[{i}/{len(targets)}] {label}", end=" ... ", flush=True)
        result = process_file(md_path)
        stats[result] = stats.get(result, 0) + 1
        print(result)

    print()
    print("=" * 60)
    print("완료!")
    print(f"  업데이트: {stats.get('updated', 0)}개")
    print(f"  내용 없어 스킵: {stats.get('skip_no_content', 0)}개")
    print(f"  이미 구조화: {stats.get('skip_structured', 0)}개")
    print(f"  API 실패: {stats.get('api_fail', 0)}개")
    print(f"  형식 오류: {stats.get('api_bad_format', 0)}개")
    if any(k.endswith("_error") for k in stats):
        errs = sum(v for k, v in stats.items() if k.endswith("_error"))
        print(f"  읽기/쓰기 오류: {errs}개")


if __name__ == "__main__":
    main()

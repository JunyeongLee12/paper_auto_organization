"""기존 노트의 '내용 발췌 (Excerpts)'를 구조화 형식으로 마이그레이션.

사용법:
    python migrate_excerpts_format.py          # dry-run
    python migrate_excerpts_format.py --apply  # 실제 반영
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from config import MARKDOWN_DIR


def section(text: str, header: str) -> str:
    m = re.search(rf"\n{re.escape(header)}\n(.*?)(?=\n## [^\n]+\n|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def first_nonempty_lines(s: str, n: int = 3) -> list[str]:
    out = []
    for line in s.splitlines():
        t = line.strip()
        if not t or t in {"-", "—"}:
            continue
        out.append(t)
        if len(out) >= n:
            break
    return out


def clean_line(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^[-*]\s*", "", s)
    s = re.sub(r"^\d+[\.)]\s*", "", s)
    return s.strip().strip('"')


def build_structured_excerpt(text: str, old_body: str) -> str:
    claims = section(text, "## 핵심 주장 (Key Claims)")
    method = section(text, "## 연구 방법 (Method)")
    findings = section(text, "## 주요 발견 (Findings)")
    abstract = section(text, "## 초록/요약 (Abstract)")

    claim_lines = [clean_line(x) for x in first_nonempty_lines(claims, 3)]
    claim_lines = [x for x in claim_lines if x and "논문을 읽고 핵심 주장을 정리하세요" not in x]

    finding_lines = [clean_line(x) for x in first_nonempty_lines(findings, 3)]
    finding_lines = [x for x in finding_lines if x and "주요 연구 결과를 정리하세요" not in x]

    method_lines = [clean_line(x) for x in first_nonempty_lines(method, 2)]
    method_lines = [x for x in method_lines if x and "연구 방법론을 정리하세요" not in x]

    old_lines = [clean_line(x) for x in first_nonempty_lines(old_body, 3)]
    old_lines = [x for x in old_lines if x and "핵심 내용을 보여주는 발췌문" not in x]

    part1 = claim_lines or old_lines or ["핵심 주장 섹션을 기반으로 추가 발췌가 필요합니다."]

    part2 = []
    if method_lines:
        part2.append(f"연구 방법: {method_lines[0]}")
    if finding_lines:
        part2.extend(finding_lines[:2])
    if not part2:
        part2.append("연구 방법/주요 발견 섹션을 기반으로 보완이 필요합니다.")

    abs_lines = [clean_line(x) for x in first_nonempty_lines(abstract, 3)]
    abs_lines = [x for x in abs_lines if x and "초록을 추출할 수 없습니다" not in x]
    conclusion = " ".join(abs_lines[:2]).strip()
    if not conclusion:
        conclusion = "핵심 주장과 주요 발견을 종합해 후속 검토 시 결론 문장을 보완하세요."

    body = []
    body.append("### **논문 핵심 분석**")
    body.append("")
    body.append("#### **1. 핵심 주장 기반 발췌**")
    for x in part1:
        body.append(f"- {x}")
    body.append("")
    body.append("#### **2. 방법 및 주요 발견 발췌**")
    for x in part2:
        body.append(f"- {x}")
    body.append("")
    body.append("### **요약 결론 (Executive Summary)**")
    body.append(conclusion)
    return "\n".join(body).strip()


def migrate(apply: bool) -> tuple[int, int, int, int]:
    files = sorted(MARKDOWN_DIR.glob("@*.md"))
    changed = 0
    structured = 0
    skipped = 0
    errors = 0

    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            errors += 1
            continue

        m = re.search(r"\n## 내용 발췌 \(Excerpts\)\n(.*?)(?=\n## [^\n]+\n|\Z)", text, re.DOTALL)
        if not m:
            skipped += 1
            continue

        old_body = m.group(1).strip()
        if "### **논문 핵심 분석" in old_body and "### **요약 결론 (Executive Summary)**" in old_body:
            structured += 1
            continue

        new_body = build_structured_excerpt(text, old_body)
        new_text = text[:m.start(1)] + new_body + text[m.end(1):]
        new_text = re.sub(r"\n{3,}", "\n\n", new_text).rstrip() + "\n"
        if new_text == text:
            skipped += 1
            continue

        if apply:
            try:
                p.write_text(new_text, encoding="utf-8")
            except Exception:
                errors += 1
                continue
        changed += 1

    return len(files), changed, structured, skipped + errors


def main():
    parser = argparse.ArgumentParser(description="내용 발췌(Excerpts) 형식 마이그레이션")
    parser.add_argument("--apply", action="store_true", help="실제 파일 반영")
    args = parser.parse_args()

    total, changed, structured, skipped = migrate(apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] total={total}, changed={changed}, already_structured={structured}, skipped_or_error={skipped}")


if __name__ == "__main__":
    main()

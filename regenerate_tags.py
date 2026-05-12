#!/usr/bin/env python3
"""
각 논문 노트의 내용(초록·핵심주장·방법·발견)을 기반으로 태그를 재생성합니다.
기존 PDF 재처리 없이 마크다운 노트의 분석 섹션을 활용합니다.

사용법:
  python3 regenerate_tags.py --limit 5        # 소량 테스트
  python3 regenerate_tags.py                  # 전체 실행
  python3 regenerate_tags.py --workers 3      # 병렬 3개
  python3 regenerate_tags.py --dry-run        # 변경 없이 확인만
  python3 regenerate_tags.py --empty-only     # type/paper만 있는 노트만
"""

import re
import os
import sys
import json
import time
import argparse
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import MARKDOWN_DIR, GEMINI_API_KEY, GEMINI_MODEL_LITE, GEMINI_TIMEOUT, GEMINI_REQUEST_DELAY
from normalize_tags import normalize_tag, HIERARCHY_MAP, _sort_tags

TAG_PROMPT = """\
논문의 내용을 분석하여 적절한 태그를 JSON 형식으로만 생성하세요. 다른 설명 없이 JSON만 출력하세요.

논문 정보:
제목: {title}
초록: {abstract}
핵심 주장: {key_claims}
연구 방법: {method}
주요 발견: {findings}

태그 생성 규칙:
1. 모든 태그는 영문 소문자, 단어 구분은 하이픈(-) 사용
2. 계층 태그 (분류용, 1~3개):
   - topic/ 예시: topic/entrepreneurship, topic/entrepreneurship/digital, topic/entrepreneurship/social,
     topic/knowledge-management, topic/knowledge-management/sharing, topic/knowledge-management/creation,
     topic/knowledge-management/tacit, topic/innovation, topic/innovation/open,
     topic/technology-management, topic/technology-management/ai-ml, topic/technology-management/digital-transformation,
     topic/strategy, topic/sustainability, topic/education, topic/economics, topic/finance
   - method/ 예시: method/sem, method/ml, method/topic-modeling, method/bibliometric,
     method/network-analysis, method/systematic-review, method/meta-analysis,
     method/case-study, method/qualitative, method/survey, method/quantitative, method/conceptual
3. 평탄 키워드 (이 논문에 특화된 핵심 개념·이론·구성변수, 5~10개):
   - 이 논문만의 고유한 내용을 담는 구체적인 키워드 사용
   - 예시: seci-model, frame-theory, entrepreneurial-orientation, dynamic-capabilities,
     knowledge-sharing-behavior, mediating-effect, institutional-theory, field-evolution
4. 총 8~15개 태그

응답 (JSON만):
{{"tags": ["topic/...", "method/...", "keyword1", "keyword2", ...]}}"""


# ── 섹션 추출 ──────────────────────────────────────────────────────────────────

def _extract(content: str, heading: str, max_len: int = 500) -> str:
    """마크다운 ## 섹션 텍스트 추출."""
    pattern = rf'## {re.escape(heading)}\s*\n(.*?)(?=\n## |\Z)'
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return ""
    text = m.group(1).strip()
    text = re.sub(r'^[\d]+\.\s*', '', text, flags=re.MULTILINE)  # 번호 제거
    text = re.sub(r'^[-*]\s*', '', text, flags=re.MULTILINE)     # 불릿 제거
    return text[:max_len]


def extract_note_content(content: str) -> dict:
    """노트에서 태그 생성에 필요한 섹션을 추출."""
    title_m = re.search(r'^title:\s*"?(.+?)"?\s*$', content, re.MULTILINE)
    return {
        "title":      (title_m.group(1) if title_m else "")[:200],
        "abstract":   _extract(content, "초록/요약 (Abstract)", 600),
        "key_claims": _extract(content, "핵심 주장 (Key Claims)", 400),
        "method":     _extract(content, "연구 방법 (Method)", 300),
        "findings":   _extract(content, "주요 발견 (Findings)", 400),
    }


def has_sufficient_content(info: dict) -> bool:
    """분석 내용이 충분한지 확인."""
    total = len(info["abstract"]) + len(info["key_claims"]) + len(info["findings"])
    return total >= 80


# ── Gemini 호출 ────────────────────────────────────────────────────────────────

_api_lock = threading.Lock()


def call_gemini(prompt: str) -> list[str] | None:
    """Gemini 호출 → 태그 리스트 반환. API 키 없으면 CLI 구독 모드로 자동 전환."""
    from summarizer import _call_gemini_model
    with _api_lock:
        text = _call_gemini_model(prompt, GEMINI_MODEL_LITE)
    if not text:
        return None
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            tags = data.get("tags", [])
            if isinstance(tags, list):
                return [str(t) for t in tags if t]
    except Exception as e:
        print(f"    응답 파싱 오류: {e}")
    return None


# ── 파일 처리 ──────────────────────────────────────────────────────────────────

def process_file(path: Path, dry_run: bool = False) -> str:
    """단일 파일 처리. 결과: 'ok' | 'skip' | 'error'."""
    try:
        content = path.read_text(encoding="utf-8")
        info = extract_note_content(content)

        if not has_sufficient_content(info):
            return "skip"

        prompt = TAG_PROMPT.format(**info)
        raw_tags = call_gemini(prompt)
        if not raw_tags:
            return "error"

        # 정규화 + 계층 변환 + 정렬
        normalized = []
        for t in raw_tags:
            t = t.strip().lower().replace(" ", "-")
            # 이미 계층 형식이면 그대로
            if t.startswith("topic/") or t.startswith("method/") or t.startswith("type/"):
                normalized.append(t)
            else:
                normalized.append(normalize_tag(t))

        final_tags = _sort_tags(["type/paper"] + [t for t in normalized if t and t != "type/paper"])
        tags_str = ", ".join(final_tags)
        keyword_tags = [t for t in final_tags if not t.startswith("type/")]
        hashtags_str = " ".join(f"#{t}" for t in keyword_tags)

        # frontmatter 업데이트
        new_content = re.sub(
            r'^(tags:\s*\[)[^\]]*(\])',
            lambda m: f"{m.group(1)}{tags_str}{m.group(2)}",
            content, flags=re.MULTILINE
        )
        # 본문 해시태그 업데이트
        new_content = re.sub(
            r'(\*\*태그\*\*: ).*$',
            lambda m: f"{m.group(1)}{hashtags_str}",
            new_content, flags=re.MULTILINE
        )

        if new_content == content:
            return "skip"

        if not dry_run:
            path.write_text(new_content, encoding="utf-8")
        return "ok"

    except Exception as e:
        print(f"    파일 오류 {path.name}: {e}")
        return "error"


# ── 메인 ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="논문 내용 기반 태그 재생성")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 확인만")
    parser.add_argument("--limit", type=int, default=0, help="처리할 최대 파일 수")
    parser.add_argument("--workers", type=int, default=1, help="병렬 워커 수 (기본 1)")
    parser.add_argument("--empty-only", action="store_true",
                        help="type/paper만 있는 노트만 처리")
    args = parser.parse_args()

    files = sorted(MARKDOWN_DIR.glob("*.md"))

    # --empty-only: 태그가 거의 없는 파일만
    if args.empty_only:
        filtered = []
        for f in files:
            content = f.read_text(encoding="utf-8")
            m = re.search(r'^tags:\s*\[([^\]]*)\]', content, re.MULTILINE)
            if m:
                tags = [t.strip() for t in m.group(1).split(",") if t.strip()]
                non_type = [t for t in tags if not t.startswith("type/")]
                if len(non_type) <= 1:
                    filtered.append(f)
        files = filtered
        print(f"태그 빈약 노트: {len(files)}개")

    if args.limit:
        files = files[:args.limit]

    total = len(files)
    counts = {"ok": 0, "skip": 0, "error": 0}
    lock = threading.Lock()
    done = [0]

    def worker(path):
        result = process_file(path, dry_run=args.dry_run)
        with lock:
            counts[result] += 1
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == total:
                print(f"  [{done[0]}/{total}] 완료:{counts['ok']} 스킵:{counts['skip']} 오류:{counts['error']}")
        return result

    print(f"{'[dry-run] ' if args.dry_run else ''}태그 재생성 시작: {total}개 파일, {args.workers} workers")

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(worker, files))
    else:
        for f in files:
            worker(f)

    mode = "[dry-run] " if args.dry_run else ""
    print(f"\n{mode}완료:{counts['ok']} / 스킵(내용부족):{counts['skip']} / 오류:{counts['error']} / 전체:{total}")


if __name__ == "__main__":
    main()

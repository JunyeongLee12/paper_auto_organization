"""논문 텍스트 분석/요약 — Gemini 2단계 파이프라인

처리 순서:
  1. 학위논문 감지 → 즉시 메타데이터 fallback (AI 호출 없음)
  2. Stage 1: gemini-2.5-flash-lite  → 서지정보(제목/저자/연도/저널) 추출
  3. Stage 2: gemini-3-flash-preview → Stage 1 결과 참조하여 심층 분석
  4. 메타데이터 기반 fallback
"""

import json
import re
import time

import requests

from config import (
    GEMINI_API_KEY, GEMINI_MODEL, GEMINI_MODEL_LITE, GEMINI_TIMEOUT,
    GEMINI_REQUEST_DELAY,
)
from moc_manager import scan_mocs, build_moc_catalog_text

# ── 프롬프트 ──────────────────────────────────────────────────────────────────

METADATA_PROMPT = """\
논문 텍스트에서 서지정보만 추출하세요. **반드시 아래 JSON 형식으로만** 응답하세요.
다른 설명 없이 JSON만 출력하세요.

{{
  "title": "논문 제목",
  "author": "저자 (여러 명이면 쉼표로 구분)",
  "year": "출판 연도 (4자리 숫자, 알 수 없으면 빈 문자열)",
  "journal": "저널/학회/출처명"
}}

논문 텍스트:
{text}
"""

ANALYSIS_PROMPT = """\
당신은 학술 논문 분석 전문가입니다. 아래 논문을 분석하여 **반드시 아래 JSON 형식으로만** 응답하세요.
다른 설명 없이 JSON만 출력하세요.

서지정보 (참고용):
- 제목: {title}
- 저자: {author}
- 연도: {year}
- 저널: {journal}

{{
  "abstract": "논문의 초록(Abstract) 전체를 원문 그대로 추출",
  "key_claims": ["핵심 주장1 (한국어)", "핵심 주장2", "핵심 주장3"],
  "method": "연구 방법론 요약 (한국어)",
  "findings": ["1. 주요 발견1 (한국어)", "2. 주요 발견2", "3. 주요 발견3"],
  "excerpts": "아래 형식을 따르는 마크다운 문자열(한국어)\n### **논문 핵심 분석**\n#### **1. [핵심 주제 1]**\n- [원문 근거를 반영한 발췌/해설]\n#### **2. [핵심 주제 2]**\n- [원문 근거를 반영한 발췌/해설]\n### **요약 결론 (Executive Summary)**\n[실무적 시사점 중심 3~5문장 요약]",
  "tags": ["태그1", "태그2", "태그3"],
  "moc_assignments": [
    {{"name": "MOC_주제명", "is_new": false}},
    {{"name": "MOC_새주제", "is_new": true, "description": "새주제 관련 연구 허브"}}
  ]
}}

MOC 분류 규칙:
- 아래는 사용 가능한 기존 MOC 목록입니다:
{moc_catalog}
- 기존 MOC 중 이 논문에 적합한 것이 있으면 is_new: false로 선택 (1~3개)
- 적합한 기존 MOC가 없으면 is_new: true로 새 MOC를 제안 (이름은 MOC_ 접두사 + 한국어 주제명)
- 기존 MOC 목록이 비어있으면 자유롭게 새 MOC를 생성하세요

논문 텍스트:
{text}
"""

MAX_TEXT_LENGTH = 30000   # Gemini 입력 한도
STAGE1_TEXT_LIMIT = 8000  # 서지정보 추출엔 앞부분으로 충분

THESIS_KEYWORDS = ("학위", "석사", "박사", "thesis", "dissertation")

# 모델별 가용 상태 캐시: None=미확인, True=정상, False=불가
_gemini_status: dict[str, bool | None] = {}


# ── 학위논문 감지 ─────────────────────────────────────────────────────────────

def is_thesis(paper: dict) -> bool:
    """학위논문 여부 판별.

    기준: 페이지 수 ≥ 50  OR  메타데이터에 학위/석사/박사/thesis/dissertation 포함
    """
    if paper.get("page_count", 0) >= 50:
        return True
    meta = paper.get("metadata", {})
    check_text = " ".join([
        meta.get("title", ""),
        meta.get("subject", ""),
        paper.get("file_name", ""),
    ]).lower()
    return any(kw in check_text for kw in THESIS_KEYWORDS)


# ── Gemini API 호출 ───────────────────────────────────────────────────────────


def _call_gemini_model(prompt: str, model: str) -> str | None:
    """특정 Gemini 모델로 REST API 호출. 영구 오류 또는 Rate Limit 시 None 반환."""
    global _gemini_status

    if _gemini_status.get(model) is False:
        return None
    if not GEMINI_API_KEY or GEMINI_API_KEY == "여기에_API_키_입력":
        _gemini_status[model] = False
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
    }

    try:
        resp = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT)

        if resp.status_code == 429:
            print(f"  [Rate Limit] {model} — fallback으로 전환")
            return None

        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        _gemini_status[model] = True
        time.sleep(GEMINI_REQUEST_DELAY)  # Rate Limit 방지
        return text

    except requests.ConnectionError:
        if _gemini_status.get(model) is None:
            print(f"  [경고] Gemini({model}) 연결 실패.")
        _gemini_status[model] = False
        return None
    except KeyError:
        print(f"  [경고] Gemini({model}) 응답 파싱 실패.")
        return None
    except Exception as e:
        err = str(e)
        if any(k in err for k in ("API_KEY_INVALID", "404")):
            # 영구적 오류 (잘못된 키, 존재하지 않는 모델)
            if _gemini_status.get(model) is None:
                print(f"  [경고] Gemini({model}) 오류: {e}")
            _gemini_status[model] = False
        else:
            print(f"  [오류] Gemini({model}) 호출 실패: {e}")
        return None


# ── 응답 파싱 ─────────────────────────────────────────────────────────────────

def _parse_json_response(text: str) -> dict | None:
    """LLM 응답에서 JSON 객체를 추출."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    else:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ── Fallback 요약 ─────────────────────────────────────────────────────────────

def _fallback_summary(paper: dict) -> dict:
    """AI 없이 메타데이터만으로 기본 요약 생성."""
    meta = paper.get("metadata", {})
    full_text = paper.get("full_text", "")

    abstract = ""
    abstract_match = re.search(
        r"(?:Abstract|ABSTRACT|초록)[:\s]*(.+?)(?:\n\n|\nKeyword|\nIntroduction|\n1[.\s])",
        full_text,
        re.DOTALL | re.IGNORECASE,
    )
    if abstract_match:
        abstract = abstract_match.group(1).strip()[:1000]

    year = ""
    date_str = meta.get("creation_date", "")
    year_match = re.search(r"((?:19|20)\d{2})", date_str)
    if year_match:
        year = year_match.group(1)
    if not year:
        year_match = re.search(r"((?:19|20)\d{2})", paper.get("file_name", ""))
        if year_match:
            year = year_match.group(1)

    # 내용 발췌 fallback: 본문 단락으로 구조화된 발췌문 구성
    excerpt_points = []
    paragraphs = [
        p.strip()
        for p in re.split(r"\n\s*\n", full_text)
        if p and len(p.strip()) >= 120
    ]
    for p in paragraphs[:3]:
        excerpt_points.append(re.sub(r"\s+", " ", p)[:500].strip())

    if excerpt_points:
        bullets = "\n".join(f"- {pt}" for pt in excerpt_points)
        excerpts = (
            "### **논문 핵심 분석**\n\n"
            "#### **1. 본문 기반 핵심 발췌**\n"
            f"{bullets}\n\n"
            "### **요약 결론 (Executive Summary)**\n"
            "원문 기반 핵심 내용을 정리했습니다. AI 심층 분석 실행 시 더 정교한 구조화 발췌가 생성됩니다."
        )
    else:
        excerpts = (
            "### **논문 핵심 분석**\n\n"
            "#### **1. 본문 기반 핵심 발췌**\n"
            "- [원문 텍스트가 부족하여 자동 발췌를 생성하지 못했습니다.]\n\n"
            "### **요약 결론 (Executive Summary)**\n"
            "PDF 원문을 확인해 발췌를 보완하세요."
        )

    return {
        "title": meta.get("title", "") or paper.get("file_name", "").replace(".pdf", ""),
        "author": meta.get("author", ""),
        "year": year,
        "journal": meta.get("subject", ""),
        "abstract": abstract or "[초록을 추출할 수 없습니다. 원본 PDF를 확인하세요.]",
        "key_claims": "> [논문을 읽고 핵심 주장을 정리하세요]\n\n-",
        "method": "> [연구 방법론을 정리하세요]\n\n-",
        "findings": "> [주요 연구 결과를 정리하세요]\n\n1.\n2.\n3.",
        "excerpts": excerpts,
        "tags": [],
        # 확장 서지정보 필드 기본값 (KeyError 방지)
        "doi": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "issn": "",
        "url": "",
        "language": "",
        "publisher": "",
        "moc_assignments": [],
    }


# ── 메인 요약 함수 ────────────────────────────────────────────────────────────

def summarize_paper(paper: dict, biblio: dict | None = None) -> dict:
    """논문 데이터를 분석하여 요약 딕셔너리를 반환.

    Args:
        paper: extractor.extract_one() 반환값 (full_text, metadata 포함)
        biblio: Zotero 서지정보 dict. 제공 시 Stage 1 건너뛰고 biblio 값 사용.
                {'title', 'author', 'year', 'journal', 'abstract', 'tags', ...}

    Returns:
        {title, author, year, journal, abstract, key_claims, method,
         findings, excerpts, tags}
    """
    fallback = _fallback_summary(paper)

    # 1. Zotero 서지정보가 제공된 경우 Stage 1 건너뜀
    if biblio:
        print("  [Zotero] 서지정보 사용 — Stage 1 건너뜀")
        title   = biblio.get("title")   or fallback["title"]
        author  = biblio.get("author")  or fallback["author"]
        year    = biblio.get("year")    or fallback["year"]
        journal = biblio.get("journal") or fallback["journal"]
        # Zotero abstract가 있으면 fallback에 반영
        if biblio.get("abstract"):
            fallback["abstract"] = biblio["abstract"]
        if biblio.get("tags"):
            fallback["tags"] = biblio["tags"]
        # 확장 서지정보 필드 반영
        for ext_field in ("doi", "volume", "issue", "pages", "issn", "url", "language", "publisher"):
            if biblio.get(ext_field):
                fallback[ext_field] = biblio[ext_field]
    else:
        # 1-a. 학위논문 제외
        if is_thesis(paper):
            print("  [스킵] 학위논문 감지 — AI 분석 없이 메타데이터만 사용")
            return fallback

        full_text = paper.get("full_text", "")
        truncated = full_text[:MAX_TEXT_LENGTH]

        # 2. Stage 1: lite 모델로 서지정보 추출
        stage1: dict = {}
        raw1 = _call_gemini_model(
            METADATA_PROMPT.format(text=truncated[:STAGE1_TEXT_LIMIT]),
            GEMINI_MODEL_LITE,
        )
        if raw1:
            parsed1 = _parse_json_response(raw1)
            if parsed1:
                stage1 = parsed1
                print("  [Stage 1] 서지정보 추출 완료 (lite)")

        title   = stage1.get("title")   or fallback["title"]
        author  = stage1.get("author")  or fallback["author"]
        year    = stage1.get("year")    or fallback["year"]
        journal = stage1.get("journal") or fallback["journal"]

    full_text = paper.get("full_text", "")
    truncated = full_text[:MAX_TEXT_LENGTH]

    # 3. Stage 2: preview 모델로 심층 분석 (MOC 분류 포함)
    # scan_mocs()는 디스크 I/O — 배치 처리 시에도 최신 상태 반영 위해 매번 호출
    moc_catalog = build_moc_catalog_text(scan_mocs())
    raw2 = _call_gemini_model(
        ANALYSIS_PROMPT.format(
            title=title, author=author, year=year, journal=journal,
            moc_catalog=moc_catalog,
            text=truncated,
        ),
        GEMINI_MODEL,
    )
    if raw2:
        parsed2 = _parse_json_response(raw2)
        if parsed2:
            print("  [Stage 2] 심층 분석 완료 (preview)")
            result = {"title": title, "author": author, "year": year, "journal": journal}
            result.update(parsed2)
            for key in fallback:
                if key not in result or not result[key]:
                    result[key] = fallback[key]
            return result

    # 4. 메타데이터 fallback
    for key in ("title", "author", "year", "journal"):
        val = (biblio or {}).get(key) or locals().get(key)
        if val:
            fallback[key] = val
    return fallback


if __name__ == "__main__":
    from config import JSON_PATH
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        papers = json.load(f)
    if papers:
        result = summarize_paper(papers[0])
        print(json.dumps(result, ensure_ascii=False, indent=2))

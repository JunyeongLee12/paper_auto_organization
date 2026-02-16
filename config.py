"""경로, Gemini API 설정, 마크다운 템플릿 등 전체 설정

사용법:
  1. 이 파일과 같은 폴더에 .env 파일 생성 (또는 시스템 환경변수 설정)
  2. .env.example 참고하여 필수 값 입력
  3. python main.py 실행
"""

import os
import sys
from pathlib import Path

# ── .env 파일 로드 ────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _val = _line.split("=", 1)
            _key, _val = _key.strip(), _val.strip().strip('"').strip("'")
            if _key and _val:
                os.environ.setdefault(_key, _val)


def _require_env(key: str, desc: str) -> str:
    """필수 환경변수를 가져오고, 없으면 안내 메시지와 함께 종료"""
    val = os.getenv(key, "").strip()
    if not val:
        print(f"[오류] 환경변수 '{key}' 미설정 — {desc}")
        print(f"       .env 파일 또는 시스템 환경변수에 {key}=값 을 추가하세요.")
        sys.exit(1)
    return val


def _require_path(key: str, desc: str) -> Path:
    """필수 경로 환경변수를 가져와 Path로 반환"""
    return Path(_require_env(key, desc))


# ── 경로 설정 ──────────────────────────────────────────────
PDF_DIR      = _require_path("PDF_DIR", "논문 PDF가 저장된 폴더 경로")
MARKDOWN_DIR = _require_path("MARKDOWN_DIR", "Obsidian 마크다운 출력 폴더 경로")
SCRIPT_DIR   = Path(os.getenv("SCRIPT_DIR", str(Path(__file__).parent)))
JSON_PATH    = PDF_DIR / "extracted_papers.json"

# ── Zotero 설정 ────────────────────────────────────────────
ZOTERO_LIBRARY_ID    = _require_env("ZOTERO_LIBRARY_ID", "zotero.org/settings → Your user ID")
ZOTERO_API_KEY       = _require_env("ZOTERO_API_KEY", "zotero.org/settings/keys 에서 생성")
ZOTERO_STORAGE       = _require_path("ZOTERO_STORAGE", "Zotero storage 폴더 경로")
ZOTERO_POLL_INTERVAL = int(os.getenv("ZOTERO_POLL_INTERVAL", "60"))   # 폴링 간격(초)
ZOTERO_NOTE_SYNC     = os.getenv("ZOTERO_NOTE_SYNC", "true").lower() == "true"

# ── Gemini API 설정 ────────────────────────────────────────
GEMINI_API_KEY       = _require_env("GEMINI_API_KEY", "Google AI Studio에서 발급")
GEMINI_MODEL         = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")           # Stage 2: 심층 분석
GEMINI_MODEL_LITE    = os.getenv("GEMINI_MODEL_LITE", "gemini-2.5-flash-lite")       # Stage 1: 서지정보 추출
GEMINI_TIMEOUT       = int(os.getenv("GEMINI_TIMEOUT", "120"))         # seconds
GEMINI_REQUEST_DELAY = int(os.getenv("GEMINI_REQUEST_DELAY", "4"))     # 요청 간 딜레이(초)

# ── 마크다운 템플릿 ───────────────────────────────────────
MARKDOWN_TEMPLATE = """\
---
title: "{title}"
year: {year}
tags: [{tags}]
created: {created}
zotero_key: {zotero_key}
doi: {doi}
---

# {title}

## 서지정보 (Citation)
- **저자**: {author}
- **연도**: {year}
- **저널/출처**: {journal}
- **출판사**: {publisher}
- **권(Vol)**: {volume}
- **호(Issue)**: {issue}
- **페이지**: {pages}
- **DOI**: {doi}
- **ISSN**: {issn}
- **URL**: {url}
- **언어**: {language}
- **태그**: {hashtags}

## 초록/요약 (Abstract)
{abstract}

## 핵심 주장 (Key Claims)
{key_claims}

## 연구 방법 (Method)
{method}

## 주요 발견 (Findings)
{findings}

## 내용 발췌 (Excerpts)
{excerpts}

## 나의 생각 (My Thoughts)
> [이 논문에 대한 개인적인 생각, 비평, 질문 등]

-

## 연결 (Links)
- 관련 노트:
- MOC: [[MOC_생성형AI]] | [[MOC_기업가정신]] | [[MOC_지식경영]]

---
**원본 파일**: [[{pdf_filename}]]
"""

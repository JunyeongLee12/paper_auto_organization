"""PDF에서 텍스트/메타데이터를 추출하여 extracted_papers.json에 저장"""

import json
import os
from pathlib import Path

import fitz  # PyMuPDF

from config import PDF_DIR, JSON_PATH


def load_existing_papers() -> list[dict]:
    """기존 JSON 파일 로드. 없으면 빈 리스트 반환."""
    if JSON_PATH.exists():
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_papers(papers: list[dict]):
    """논문 리스트를 JSON으로 저장."""
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)


def extract_one(pdf_path: Path) -> dict:
    """단일 PDF에서 텍스트와 메타데이터를 추출."""
    doc = fitz.open(pdf_path)
    meta = doc.metadata or {}

    full_text = ""
    for page in doc:
        full_text += page.get_text()

    result = {
        "file_name": pdf_path.name,
        "file_size_kb": round(os.path.getsize(pdf_path) / 1024, 1),
        "metadata": {
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
            "subject": meta.get("subject", ""),
            "creator": meta.get("creator", ""),
            "producer": meta.get("producer", ""),
            "creation_date": meta.get("creationDate", ""),
            "mod_date": meta.get("modDate", ""),
        },
        "page_count": doc.page_count,
        "full_text": full_text,
    }
    doc.close()
    return result


def extract_new_pdfs() -> list[dict]:
    """PDF 폴더에서 아직 추출되지 않은 PDF만 추출하여 JSON에 추가.

    Returns:
        새로 추출된 논문 리스트
    """
    papers = load_existing_papers()
    existing_names = {p["file_name"] for p in papers}

    pdf_files = sorted(PDF_DIR.glob("*.pdf"))
    new_papers = []

    for pdf_path in pdf_files:
        if pdf_path.name in existing_names:
            continue
        print(f"  추출 중: {pdf_path.name}")
        try:
            paper = extract_one(pdf_path)
            papers.append(paper)
            new_papers.append(paper)
        except Exception as e:
            print(f"  [오류] {pdf_path.name}: {e}")

    if new_papers:
        save_papers(papers)
        print(f"  → {len(new_papers)}개 논문 추출 완료 (전체 {len(papers)}개)")
    else:
        print("  → 새로 추출할 PDF가 없습니다.")

    return new_papers


if __name__ == "__main__":
    extract_new_pdfs()

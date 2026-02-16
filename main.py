"""메인 스크립트: 수동 모드 + watchdog 감시 모드 + Zotero 연동 모드

사용법:
    python main.py          # 수동 모드: 미처리 PDF 일괄 처리
    python main.py --watch  # 감시 모드: 폴더 감시하며 새 PDF 실시간 처리
    python main.py --all    # 기존 JSON의 모든 논문에 대해 마크다운 재생성 (없는 것만)
    python main.py --zotero # Zotero 연동 모드: Zotero 라이브러리 폴링하며 새 논문 처리
"""

import argparse
import sys
import time

from config import PDF_DIR
from extractor import extract_new_pdfs, load_existing_papers
from markdown_gen import generate_markdown
from summarizer import summarize_paper


def process_papers(papers: list[dict]):
    """논문 리스트를 요약하고 마크다운 생성."""
    for i, paper in enumerate(papers, 1):
        name = paper["file_name"]
        print(f"\n[{i}/{len(papers)}] {name}")

        print("  AI 분석 중...")
        summary = summarize_paper(paper)

        generate_markdown(summary, name)


def run_manual():
    """수동 모드: 미추출 PDF를 추출 후 마크다운 생성."""
    print("=" * 60)
    print("논문 자동 추출 & 마크다운 생성 (수동 모드)")
    print("=" * 60)

    print("\n[1단계] PDF 텍스트 추출")
    new_papers = extract_new_pdfs()

    if not new_papers:
        print("\n처리할 새 논문이 없습니다.")
        return

    print(f"\n[2단계] {len(new_papers)}개 논문 분석 및 마크다운 생성")
    process_papers(new_papers)

    print("\n완료!")


def run_all_markdown():
    """JSON에 있는 모든 논문에 대해 마크다운 생성 (없는 것만)."""
    print("=" * 60)
    print("기존 논문 마크다운 일괄 생성")
    print("=" * 60)

    papers = load_existing_papers()
    print(f"\nJSON에 {len(papers)}개 논문이 있습니다.")
    print("마크다운이 없는 논문만 처리합니다.\n")

    process_papers(papers)
    print("\n완료!")


def run_watch():
    """watchdog 모드: 폴더 감시하며 새 PDF 실시간 처리."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers.polling import PollingObserver as Observer  # WSL2 /mnt/c 경로는 inotify 미지원
    except ImportError:
        print("watchdog 패키지가 필요합니다: pip install watchdog")
        sys.exit(1)

    class PDFHandler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            if not event.src_path.lower().endswith(".pdf"):
                return

            print(f"\n새 PDF 감지: {event.src_path}")
            # 파일 쓰기 완료 대기
            time.sleep(2)

            print("[1단계] 텍스트 추출")
            new_papers = extract_new_pdfs()
            if new_papers:
                print(f"[2단계] {len(new_papers)}개 논문 분석 및 마크다운 생성")
                process_papers(new_papers)
                print("처리 완료!")

    observer = Observer()
    observer.schedule(PDFHandler(), str(PDF_DIR), recursive=False)
    observer.start()

    print("=" * 60)
    print("논문 폴더 감시 모드 (Ctrl+C로 종료)")
    print(f"감시 폴더: {PDF_DIR}")
    print("=" * 60)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\n감시 종료.")
    observer.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="논문 자동 추출 & 마크다운 생성")
    parser.add_argument("--watch", action="store_true", help="폴더 감시 모드")
    parser.add_argument("--all", action="store_true", help="기존 JSON 전체 마크다운 생성")
    parser.add_argument("--zotero", action="store_true", help="Zotero 연동 모드 (60초 폴링)")
    args = parser.parse_args()

    if args.zotero:
        from zotero_sync import watch_zotero
        watch_zotero()
    elif args.watch:
        run_watch()
    elif args.all:
        run_all_markdown()
    else:
        run_manual()

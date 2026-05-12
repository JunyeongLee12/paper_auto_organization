"""종합 배치 재처리 스크립트

기능:
  1. Placeholder MD 재처리 (AI 분석 없이 생성된 노트 → AI 재분석)
  2. 기존 완료 노트 최신 버전 업데이트 (--update-all)
  3. Zotero 중복 노트 정리 (--cleanup-notes)

사용법:
    python batch_reprocess.py --placeholder          # placeholder만 재처리 (권장)
    python batch_reprocess.py --update-all           # 전체 재처리 (시간 오래 걸림)
    python batch_reprocess.py --cleanup-notes        # Zotero 중복 노트만 정리
    python batch_reprocess.py --placeholder --limit 10  # 10개만 (테스트)
    python batch_reprocess.py --placeholder --dry-run   # 대상 목록만 출력

진행 상태:
    SCRIPT_DIR/batch_progress.json 에 저장 → 중단 후 재시작해도 이어서 처리
"""

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from pyzotero import zotero as pyzot
except ImportError:
    print("[오류] pyzotero 필요: pip install pyzotero")
    sys.exit(1)

from config import (
    MARKDOWN_DIR, SCRIPT_DIR,
    ZOTERO_API_KEY, ZOTERO_LIBRARY_ID, ZOTERO_STORAGE,
    ZOTERO_NOTE_SYNC,
)
from extractor import load_existing_papers
from markdown_gen import generate_markdown
from summarizer import summarize_paper
from zotero_sync import ZoteroSync, _normalize_title

PROGRESS_FILE = SCRIPT_DIR / "batch_progress.json"
_HTML_TAG_RE = re.compile(r"<[^>]+>")


# ── 진행 상태 관리 ─────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"done_keys": [], "done_md": [], "errors": []}


def save_progress(progress: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# ── MD 파일 분류 ───────────────────────────────────────────────────────────────

def find_target_mds(placeholder_only: bool) -> list[dict]:
    """처리 대상 MD 파일 목록 반환.

    Returns:
        [{"md_path": Path, "zotero_key": str|None, "title": str}]
    """
    targets = []
    for md_path in sorted(MARKDOWN_DIR.glob("@*.md")):
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            continue

        is_placeholder = "[논문을 읽고 핵심 주장을 정리하세요]" in text

        if placeholder_only and not is_placeholder:
            continue

        km = re.search(r'^zotero_key:\s*([A-Z0-9]{8})\s*$', text, re.MULTILINE)
        zkey = km.group(1) if km else None

        title_m = re.search(r'^title:\s*"?(.+?)"?\s*$', text, re.MULTILINE)
        title = title_m.group(1)[:80] if title_m else md_path.stem

        targets.append({
            "md_path": md_path,
            "zotero_key": zkey,
            "title": title,
            "is_placeholder": is_placeholder,
        })

    return targets


# ── Zotero 중복 노트 정리 ──────────────────────────────────────────────────────

def cleanup_duplicate_notes(zot, dry_run: bool = False) -> int:
    """동일 부모 아이템에 중복된 auto-analyzed 노트 정리.

    최신 노트 1개만 남기고 나머지 삭제.
    Returns: 삭제된 노트 수
    """
    print("\n[중복 노트 정리] Zotero 노트 조회 중...")
    try:
        notes = zot.everything(zot.items(itemType="note", tag="auto-analyzed"))
    except Exception as e:
        print(f"  [오류] Zotero 조회 실패: {e}")
        return 0

    # 부모별 노트 그룹화 (최신순 정렬)
    from collections import defaultdict
    parent_notes = defaultdict(list)
    for note in notes:
        pk = note.get("data", {}).get("parentItem", "")
        if pk:
            parent_notes[pk].append(note)

    # 버전 기준으로 정렬 (높은 버전 = 최신)
    deleted = 0
    duplicates = {pk: nlist for pk, nlist in parent_notes.items() if len(nlist) > 1}
    print(f"  중복 아이템: {len(duplicates)}개")

    for pk, nlist in duplicates.items():
        # version 기준 내림차순 정렬 → 첫 번째가 최신
        nlist_sorted = sorted(nlist, key=lambda n: n.get("version", 0), reverse=True)
        to_delete = nlist_sorted[1:]  # 최신 1개 제외 나머지

        for note in to_delete:
            note_key = note.get("key", "")
            if dry_run:
                print(f"  [DRY-RUN] 삭제 예정: {pk} → note {note_key}")
            else:
                try:
                    zot.delete_item(note)
                    deleted += 1
                    time.sleep(0.3)
                except Exception as e:
                    print(f"  [경고] 노트 삭제 실패 ({note_key}): {e}")

    if not dry_run:
        print(f"  → {deleted}개 중복 노트 삭제 완료")
    return deleted


# ── Zotero 기존 노트 삭제 (재처리 전) ─────────────────────────────────────────

def delete_existing_notes(zot, item_key: str):
    """아이템의 기존 auto-analyzed 노트 모두 삭제."""
    try:
        children = zot.children(item_key)
        for child in children:
            data = child.get("data", {})
            if data.get("itemType") != "note":
                continue
            tags = [t.get("tag", "") for t in data.get("tags", [])]
            if "auto-analyzed" in tags:
                try:
                    zot.delete_item(child)
                    time.sleep(0.2)
                except Exception as e:
                    print(f"  [경고] 기존 노트 삭제 실패: {e}")
    except Exception as e:
        print(f"  [경고] children 조회 실패: {e}")


# ── Zotero 키 기반 재처리 ──────────────────────────────────────────────────────

def reprocess_with_zotero_key(sync: ZoteroSync, md_path: Path, zotero_key: str,
                               update_mode: bool) -> bool:
    """Zotero 키로 아이템 fetch → AI 재분석 → MD + 노트 갱신."""
    try:
        item = sync.zot.item(zotero_key)
    except Exception as e:
        print(f"  [오류] Zotero 조회 실패 ({zotero_key}): {e}")
        return False

    backup_path = md_path.with_suffix(md_path.suffix + ".bak")
    backup_created = False

    if md_path.exists():
        if backup_path.exists():
            print(f"  [오류] 기존 백업 파일이 이미 존재합니다: {backup_path.name}")
            return False
        try:
            md_path.rename(backup_path)
            backup_created = True
        except Exception as e:
            print(f"  [오류] MD 백업 실패 ({md_path.name}): {e}")
            return False

    try:
        # 기존 Zotero 노트 삭제 (업데이트 모드 또는 중복 방지)
        if ZOTERO_NOTE_SYNC:
            delete_existing_notes(sync.zot, zotero_key)

        # AI 재분석 + MD 생성 + Zotero 노트
        md_result = sync.process_item(item)
    except Exception as e:
        print(f"  [오류] AI 재처리 실패 ({zotero_key}): {e}")
        md_result = None

    if md_result:
        if backup_created and backup_path.exists():
            try:
                backup_path.unlink()
                print("  백업 정리 완료")
            except Exception as e:
                print(f"  [경고] 백업 정리 실패 ({backup_path.name}): {e}")
        return True

    sync.invalidate_md_key_index()
    generated_md = sync.find_markdown_by_key(zotero_key)
    if generated_md and generated_md.exists():
        try:
            generated_md.unlink()
        except Exception as e:
            print(f"  [경고] 실패한 MD 정리 실패 ({generated_md.name}): {e}")

    if backup_created and backup_path.exists():
        try:
            backup_path.rename(md_path)
            print("  [복원] AI 실패로 원본 MD 복원")
        except Exception as e:
            print(f"  [오류] 원본 MD 복원 실패 ({md_path.name}): {e}")

    return False


# ── PDF 기반 재처리 (Zotero 키 없음) ──────────────────────────────────────────

def reprocess_with_pdf(md_path: Path, paper_map: dict) -> bool:
    """PDF full_text 기반으로 AI 재분석 → MD 갱신 (Zotero 노트 없음)."""
    # MD에서 원본 PDF 파일명 추출
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return False

    pdf_m = re.search(r'\[\[(.+?\.pdf)\]\]', text, re.IGNORECASE)
    pdf_name = pdf_m.group(1) if pdf_m else None

    paper = paper_map.get(pdf_name) if pdf_name else None
    if not paper or len(paper.get("full_text", "")) < 100:
        print(f"  [스킵] PDF 텍스트 없음: {pdf_name}")
        return False

    backup = text
    md_path.unlink()

    try:
        summary = summarize_paper(paper)
        result = generate_markdown(summary, pdf_name or md_path.stem + ".pdf")
        return result is not None
    except Exception as e:
        print(f"  [오류] 재분석 실패: {e}")
        md_path.write_text(backup, encoding="utf-8")
        return False


# ── 메인 ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="배치 재처리 & 업데이트")
    parser.add_argument("--placeholder", action="store_true",
                        help="AI 미분석 placeholder 파일만 재처리")
    parser.add_argument("--update-all", action="store_true",
                        help="완료된 노트도 최신 버전으로 전체 업데이트")
    parser.add_argument("--cleanup-notes", action="store_true",
                        help="Zotero 중복 auto-analyzed 노트만 정리")
    parser.add_argument("--limit", type=int, default=0,
                        help="처리 건수 제한 (0=전체, 테스트용)")
    parser.add_argument("--dry-run", action="store_true",
                        help="변경 없이 대상 목록만 출력")
    parser.add_argument("--workers", type=int, default=3, help="병렬 처리 worker 수 (기본 3)")
    parser.add_argument("--reset-progress", action="store_true",
                        help="진행 상태 초기화 후 처음부터 재시작")
    args = parser.parse_args()

    if not args.placeholder and not args.update_all and not args.cleanup_notes:
        parser.print_help()
        sys.exit(0)

    sync = ZoteroSync(ZOTERO_LIBRARY_ID, ZOTERO_API_KEY, ZOTERO_STORAGE)

    # ── 중복 노트 정리 ──
    if args.cleanup_notes:
        cleanup_duplicate_notes(sync.zot, dry_run=args.dry_run)
        if not args.placeholder and not args.update_all:
            return

    # ── 진행 상태 로드 ──
    if args.reset_progress and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("[진행 상태 초기화]")

    progress = load_progress()
    done_keys = set(progress.get("done_keys", []))
    done_md = set(progress.get("done_md", []))
    errors = progress.get("errors", [])

    # ── 대상 수집 ──
    placeholder_only = args.placeholder and not args.update_all
    targets = find_target_mds(placeholder_only=placeholder_only)

    # 이미 완료된 항목 제외
    targets = [
        t for t in targets
        if (t["zotero_key"] not in done_keys if t["zotero_key"] else str(t["md_path"]) not in done_md)
    ]

    if args.limit:
        targets = targets[:args.limit]

    print("=" * 60)
    mode_label = "전체 업데이트" if args.update_all else "Placeholder 재처리"
    print(f"배치 재처리 {'[DRY-RUN] ' if args.dry_run else ''}— {mode_label}")
    print("=" * 60)
    print(f"처리 대상: {len(targets)}개")
    if done_keys or done_md:
        print(f"이미 완료 (스킵): {len(done_keys) + len(done_md)}개")
    print()

    if args.dry_run:
        for t in targets:
            key_label = t["zotero_key"] or "(키 없음)"
            flag = "★placeholder" if t["is_placeholder"] else ""
            print(f"  [{key_label}] {t['title'][:55]} {flag}")
        return

    # ── PDF 기반 처리를 위한 paper map 로드 ──
    print("PDF 데이터 로드 중...")
    try:
        papers = load_existing_papers()
        paper_map = {p["file_name"]: p for p in papers}
    except Exception:
        paper_map = {}
    print(f"  → {len(paper_map)}개 PDF 로드됨\n")

    # ── 병렬 처리 루프 ──
    success = skip = error = 0
    processed = 0
    lock = threading.Lock()
    print_lock = threading.Lock()

    def process_one(args_tuple: tuple) -> None:
        nonlocal success, skip, error, processed
        i, t = args_tuple
        md_path = t["md_path"]
        zkey = t["zotero_key"]
        title = t["title"]

        with print_lock:
            print(f"[{i}/{len(targets)}] {title[:55]}")

        # 스레드별 ZoteroSync 인스턴스 (pyzotero 스레드 안전성)
        thread_sync = ZoteroSync(ZOTERO_LIBRARY_ID, ZOTERO_API_KEY, ZOTERO_STORAGE)

        if zkey:
            ok = reprocess_with_zotero_key(thread_sync, md_path, zkey, update_mode=args.update_all)
        else:
            ok = reprocess_with_pdf(md_path, paper_map)

        with lock:
            if ok:
                success += 1
                if zkey:
                    progress["done_keys"].append(zkey)
                else:
                    progress["done_md"].append(str(md_path))
            else:
                error += 1
                progress["errors"].append({"key": zkey, "md": str(md_path), "title": title})

            processed += 1
            if processed % 10 == 0:
                save_progress(progress)
                with print_lock:
                    print(f"  [진행 저장] {processed}/{len(targets)} 완료")

    workers = max(1, args.workers)
    print(f"병렬 처리: {workers} workers\n")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        executor.map(process_one, enumerate(targets, 1))

    save_progress(progress)

    print()
    print("=" * 60)
    print(f"완료: 성공 {success}개 / 스킵 {skip}개 / 오류 {error}개")
    if error:
        print(f"오류 목록: {PROGRESS_FILE} 참고")


if __name__ == "__main__":
    main()

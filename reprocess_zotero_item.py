"""특정 Zotero 아이템을 강제 재분석 — MD + Zotero 노트 모두 갱신

사용법:
    python reprocess_zotero_item.py PRE75VMG
    python reprocess_zotero_item.py PRE75VMG --dry-run   # 변경 없이 상태만 확인
"""

import argparse
import copy
import sys
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
from zotero_sync import ZoteroSync, STATE_FILE
import json


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_version": 0, "processed_keys": [], "processed_titles": []}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def find_md_by_key(zotero_key: str) -> Path | None:
    for md_path in MARKDOWN_DIR.glob("@*.md"):
        try:
            if f"zotero_key: {zotero_key}" in md_path.read_text(encoding="utf-8"):
                return md_path
        except Exception:
            continue
    return None


def main():
    parser = argparse.ArgumentParser(description="특정 Zotero 아이템 강제 재분석")
    parser.add_argument("key", help="Zotero 아이템 키 (예: PRE75VMG)")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 상태만 확인")
    args = parser.parse_args()

    zotero_key = args.key.strip()
    dry_run = args.dry_run

    print(f"{'[DRY-RUN] ' if dry_run else ''}대상 키: {zotero_key}")

    # 1. Zotero에서 아이템 조회
    zot_client = pyzot.Zotero(ZOTERO_LIBRARY_ID, "user", ZOTERO_API_KEY)
    try:
        item = zot_client.item(zotero_key)
    except Exception as e:
        print(f"[오류] Zotero 아이템 조회 실패: {e}")
        sys.exit(1)

    title = item.get("data", {}).get("title", zotero_key)
    print(f"제목: {title[:80]}")

    # 2. 기존 MD 파일 확인
    existing_md = find_md_by_key(zotero_key)
    if existing_md:
        print(f"기존 MD: {existing_md.name}")
    else:
        print("기존 MD: 없음")

    # 3. dry-run이면 여기서 종료
    if dry_run:
        state = load_state()
        in_keys = zotero_key in state.get("processed_keys", [])
        print(f"processed_keys 등록 여부: {in_keys}")
        print("[DRY-RUN] 실제 변경 없이 종료.")
        return

    backup_path = None
    backup_created = False

    # 4. 기존 MD 백업
    if existing_md:
        backup_path = existing_md.with_suffix(existing_md.suffix + ".bak")
        if backup_path.exists():
            print(f"[오류] 기존 백업 파일이 이미 존재합니다: {backup_path.name}")
            return
        try:
            existing_md.rename(backup_path)
            backup_created = True
            print(f"기존 MD 백업: {backup_path.name}")
        except Exception as e:
            print(f"[오류] 기존 MD 백업 실패: {e}")
            return

    # 5. state에서 해당 키/제목 제거
    original_state = load_state()
    state = copy.deepcopy(original_state)
    try:
        if zotero_key in state.get("processed_keys", []):
            state["processed_keys"].remove(zotero_key)

        # 정규화된 제목도 제거
        import re as _re

        def _norm(t):
            t = _re.sub(r"<[^>]+>", "", t)
            return _re.sub(r"\s+", " ", t).strip().lower()

        norm_title = _norm(title)
        state["processed_titles"] = [
            t for t in state.get("processed_titles", []) if t != norm_title
        ]
        save_state(state)
        print("state에서 해당 항목 제거 완료")

        # 6. ZoteroSync로 재처리 (MD 생성 + Zotero 노트 갱신)
        sync = ZoteroSync(ZOTERO_LIBRARY_ID, ZOTERO_API_KEY, ZOTERO_STORAGE)
        print("\nAI 재분석 시작...")
        md_path = sync.process_item(item)
    except Exception as e:
        print(f"\n[오류] 재처리 중 실패: {e}")
        md_path = None

    if md_path:
        if backup_created and backup_path and backup_path.exists():
            try:
                backup_path.unlink()
                print("백업 정리 완료")
            except Exception as e:
                print(f"[경고] 백업 정리 실패: {e}")
        print(f"\n완료: {md_path.name}")
    else:
        failed_md = find_md_by_key(zotero_key)
        if failed_md and failed_md.exists():
            try:
                failed_md.unlink()
            except Exception as e:
                print(f"[경고] 실패한 MD 정리 실패: {e}")
        if backup_created and backup_path and backup_path.exists() and existing_md:
            try:
                backup_path.rename(existing_md)
                print("[복원] AI 실패로 원본 MD 복원")
            except Exception as e:
                print(f"[오류] 원본 MD 복원 실패: {e}")
        try:
            save_state(original_state)
        except Exception as e:
            print(f"[경고] state 복원 실패: {e}")
        print("\n[경고] MD 생성 실패 또는 스킵됨")


if __name__ == "__main__":
    main()
